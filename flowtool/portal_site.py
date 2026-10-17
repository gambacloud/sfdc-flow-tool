"""
Portal builder, stage 2: an LWR Experience Cloud site with automatic
self-registration, published and validated.

A site cannot be created by deploying metadata (Network, CustomSite and the
bundle each reference the others - the org answers "no Network named X found"),
so the pipeline is, in order:

    preflight -> create shell (Connect API) -> default Account ->
    retrieve+patch Network and deploy it with the registration handler (Metadata API) ->
    NetworkSelfRegistration record (data, not metadata) -> publish -> validate

`build_site(..., apply=False)` stops after preflight and returns the steps it
would run, so the checklist can be reviewed before anything is written.
"""

from __future__ import annotations

import asyncio
import io
import json
import re
import zipfile
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional
from xml.etree import ElementTree as ET

import httpx

from flowtool.portal_preflight import (
    BLOCKING, ERROR, FAIL, PASS, SKIP, WARNING, Check, OrgClient, OrgQueryError,
    PortalSpec, PreflightReport, _q, run_preflight,
)
from flowtool import portal_miaw as miaw
from flowtool import portal_pages as pages
from flowtool.sfdc import build_deploy_package

METADATA_NS = "http://soap.sforce.com/2006/04/metadata"
LWR_TEMPLATE = "Build Your Own (LWR)"
HANDLER_CLASS = "ForgeSelfRegHandler"
DEFAULT_MEMBER_PROFILE = "Customer Community User"

ET.register_namespace("", METADATA_NS)


@dataclass
class SiteSpec(PortalSpec):
    template: str = LWR_TEMPLATE
    member_profile: str = DEFAULT_MEMBER_PROFILE
    account_name: str = "Self Registered Customers"
    handler_class: str = HANDLER_CLASS
    content: Optional[pages.PortalContent] = None  # pages to generate; None = skip the pages step
    sharing_set_name: str = "Portal_Users"
    messaging: Optional[miaw.MiawSpec] = None  # None = no chat
    api_version: str = "64.0"
    wait_seconds: float = 300.0
    poll_seconds: float = 5.0


@dataclass
class Step:
    name: str
    status: str  # "done" | "skipped" | "failed" | "planned"
    detail: str = ""


@dataclass
class SiteResult:
    preflight: PreflightReport
    steps: List[Step] = field(default_factory=list)
    validation: List[Check] = field(default_factory=list)
    site_url: Optional[str] = None
    register_url: Optional[str] = None
    manual_steps: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return (
            self.preflight.can_proceed
            and not any(s.status == "failed" for s in self.steps)
            and not any(c.blocks for c in self.validation)
        )


# --- pure helpers -----------------------------------------------------------

def handler_source(class_name: str = HANDLER_CLASS) -> str:
    """Apex for Auth.ConfigurableSelfRegHandler: build a User from the form and
    let Site.createExternalUser create the Contact under the default Account."""
    return f"""global class {class_name} implements Auth.ConfigurableSelfRegHandler {{
    global Id createUser(Id accountId, Id profileId, Map<SObjectField, String> registrationAttributes, String password) {{
        User u = new User();
        u.ProfileId = profileId;
        for (SObjectField f : registrationAttributes.keySet()) {{
            u.put(f, registrationAttributes.get(f));
        }}
        String email = u.Email;
        u.Username = email;
        u.Alias = (u.FirstName != null ? u.FirstName.left(1) : '') + (u.LastName != null ? u.LastName.left(4) : 'user');
        u.CommunityNickname = email.left(40) + String.valueOf(Crypto.getRandomInteger()).left(5);
        u.EmailEncodingKey = 'UTF-8';
        u.LanguageLocaleKey = UserInfo.getLanguage();
        u.LocaleSidKey = UserInfo.getLocale();
        u.TimeZoneSidKey = UserInfo.getTimeZone().getID();
        u.IsActive = true;
        return Site.createExternalUser(u, accountId, password);
    }}
}}
"""


def handler_meta(api_version: str) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<ApexClass xmlns="{METADATA_NS}"><apiVersion>{api_version}</apiVersion>'
        "<status>Active</status></ApexClass>\n"
    )


def _child(parent: ET.Element, tag: str, create: bool = True) -> Optional[ET.Element]:
    node = parent.find(f"{{{METADATA_NS}}}{tag}")
    if node is None and create:
        node = ET.SubElement(parent, f"{{{METADATA_NS}}}{tag}")
    return node


def patch_network_xml(xml: str, member_profile: str, go_live: bool = True) -> str:
    """
    Turn on self-registration in a retrieved Network file: the flag, the member
    profile, and the Configurable page override the registration record needs
    (without it the org refuses NetworkSelfRegistration: "OverrideSetting must
    be Configurable"). Everything else in the file is left as the org wrote it.
    """
    root = ET.fromstring(xml)
    _child(root, "selfRegistration").text = "true"
    if go_live:
        _child(root, "status").text = "Live"

    groups = _child(root, "networkMemberGroups")
    wanted = member_profile.lower()
    if not any((p.text or "").lower() == wanted for p in groups.findall(f"{{{METADATA_NS}}}profile")):
        ET.SubElement(groups, f"{{{METADATA_NS}}}profile").text = member_profile

    overrides = _child(root, "networkPageOverrides")
    _child(overrides, "selfRegProfilePageOverrideSetting").text = "Configurable"

    ET.indent(root)
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding="unicode") + "\n"


def network_zip_member(zip_bytes: bytes, site_name: str) -> str:
    """Path of the Network file inside a retrieve zip."""
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as archive:
        for name in archive.namelist():
            if name.startswith("networks/") and name.endswith(".network"):
                if name[len("networks/"):-len(".network")].lower() == site_name.lower():
                    return name
    raise RuntimeError(f"Network {site_name!r} not in the retrieved package")


# --- org access (adds the write paths OrgClient deliberately lacks) ---------

class OrgWriter(OrgClient):
    """OrgClient plus the three writes this pipeline needs: Connect API
    POST, and creating a record. Kept separate so preflight stays read-only."""

    async def get_json(self, path: str) -> Dict[str, Any]:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.get(self._url(path), headers=self._headers)
        if resp.status_code != 200:
            self._raise(resp)
        return resp.json()

    async def post_json(self, path: str, body: Dict[str, Any]) -> Dict[str, Any]:
        async with httpx.AsyncClient(timeout=120.0) as client:
            resp = await client.post(self._url(path), json=body, headers=self._headers)
        if resp.status_code not in (200, 201, 202):
            self._raise(resp)
        return resp.json() if resp.content else {}

    async def create(self, sobject: str, fields: Dict[str, Any]) -> str:
        return (await self.post_json(f"sobjects/{sobject}", fields))["id"]

    async def http_probe(self, url: str) -> tuple:
        """(status, Location) WITHOUT following redirects: a page that bounces
        every visitor to login must not read as a 200."""
        async with httpx.AsyncClient(timeout=60.0, follow_redirects=False) as client:
            resp = await client.get(url)
        return resp.status_code, resp.headers.get("location", "")


async def _wait_for(probe: Callable[[], Awaitable[Any]], seconds: float, poll: float, what: str) -> Any:
    waited = 0.0
    while True:
        value = await probe()
        if value:
            return value
        if waited >= seconds:
            raise TimeoutError(f"{what} did not appear within {seconds:.0f}s")
        await asyncio.sleep(poll)
        waited += poll


async def _find_site(org, spec: SiteSpec) -> Optional[Dict[str, Any]]:
    data = await org.get_json("connect/communities")
    for c in data.get("communities", []):
        if c.get("name") == spec.site_name:
            return c
    return None


# --- the pipeline -----------------------------------------------------------

PLANNED_STEPS = [
    ("Create site shell", "Connect API: create the LWR site from the template"),
    ("Default Account", "create the Account that self-registered Contacts belong to"),
    ("Network + handler", "retrieve the Network, enable self-registration and go Live, deploy it with the Apex handler"),
    ("Self-registration record", "create the NetworkSelfRegistration record (Account, handler, Execute-As user, fields)"),
    ("Pages + access", "public landing page, secure My Account page (LWC), guest access on, Sharing Set, member profile permissions"),
    ("Publish", "Connect API: publish the site"),
    ("Validate", "site Live, registration, landing public, My Account requires login, guest has no Contact/Account read, sharing set"),
]
MESSAGING_STEP = ("Messaging", "queue routing, Messaging queue, embedded MessagingChannel and Embedded Service Deployment for the site")


def planned_steps(spec: "SiteSpec") -> List[tuple]:
    steps = list(PLANNED_STEPS)
    if spec.messaging is not None:
        steps.insert(steps.index(next(s for s in steps if s[0] == "Publish")), MESSAGING_STEP)
    return steps


async def build_site(org, meta, spec: SiteSpec, apply: bool = False) -> SiteResult:
    """`org` is an OrgWriter, `meta` a MetadataClient. Nothing is written unless apply=True."""
    preflight = await run_preflight(org, spec)
    result = SiteResult(preflight=preflight)
    if not preflight.can_proceed:
        return result
    if not apply:
        result.steps = [Step(n, "planned", d) for n, d in planned_steps(spec)]
        return result

    def add(name: str, status: str, detail: str = "") -> None:
        result.steps.append(Step(name, status, detail))

    try:
        # 1. shell
        site = await _find_site(org, spec)
        if site:
            add("Create site shell", "skipped", f"'{spec.site_name}' already exists")
        else:
            await org.post_json("connect/communities", {
                "name": spec.site_name,
                "urlPathPrefix": spec.prefix(),
                "templateName": spec.template,
            })
            site = await _wait_for(lambda: _find_site(org, spec), spec.wait_seconds, spec.poll_seconds, "the new site")
            add("Create site shell", "done", site["siteUrl"])
        site_id = site["id"]

        # 2. default Account
        acc = await org.query(f"SELECT Id FROM Account WHERE Name = '{_q(spec.account_name)}' LIMIT 1")
        account_id = acc[0]["Id"] if acc else await org.create("Account", {"Name": spec.account_name})
        add("Default Account", "skipped" if acc else "done", spec.account_name)

        # 3. Network + handler
        types = {"Network": [spec.site_name]}
        zip_bytes = await meta.wait_for_retrieve(await meta.start_retrieve_types(types))
        member = network_zip_member(zip_bytes, spec.site_name)
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as archive:
            network_xml = archive.read(member).decode("utf-8")
        files = {
            member: patch_network_xml(network_xml, spec.member_profile),
            f"classes/{spec.handler_class}.cls": handler_source(spec.handler_class),
            f"classes/{spec.handler_class}.cls-meta.xml": handler_meta(spec.api_version),
        }
        package = build_deploy_package(
            files, {"Network": [spec.site_name], "ApexClass": [spec.handler_class]}, spec.api_version
        )
        deployed = await meta.deploy_and_wait(package, check_only=False)
        if not deployed.success:
            detail = "; ".join(str(f) for f in deployed.failures) or deployed.error_message or deployed.status
            add("Network + handler", "failed", detail)
            return result
        add("Network + handler", "done")

        # 4. registration record
        network_id = (await org.query(f"SELECT Id FROM Network WHERE Name = '{_q(spec.site_name)}'"))[0]["Id"]
        handler_id = (await org.query(f"SELECT Id FROM ApexClass WHERE Name = '{_q(spec.handler_class)}'"))[0]["Id"]
        user_id = (await org.query(f"SELECT Id FROM User WHERE Username = '{_q(spec.admin_username or '')}'"))[0]["Id"] \
            if spec.admin_username else await org.current_user_id()
        existing = await org.query(f"SELECT Id FROM NetworkSelfRegistration WHERE NetworkId = '{network_id}'")
        if existing:
            add("Self-registration record", "skipped", "already exists - not modified")
        else:
            await org.create("NetworkSelfRegistration", {
                "NetworkId": network_id, "AccountId": account_id,
                "ApexHandlerId": handler_id, "ExecuteApexHandlerAsId": user_id,
                "OptionsShowFirstName": True, "OptionsShowLastName": True, "OptionsShowEmail": True,
                "OptionsIncludePassword": True, "OptionsShowUsername": False,
                "OptionsShowNickname": False, "OptionsShowMobilePhone": False,
                "OptionsDisableStandardRgstrComponent": False,
            })
            add("Self-registration record", "done")

        # 4b. pages, guest access, sharing set, profile permissions
        content = spec.content
        if content is not None:
            detail = await _deploy_pages(meta, spec, content)
            add("Pages + access", "done", detail)
        else:
            add("Pages + access", "skipped", "no page content requested")

        # 4c. messaging (MIAW) chain
        if spec.messaging is not None:
            bundle = await _bundle_name(meta, spec)
            files, types = miaw.miaw_files(spec.messaging, bundle.split("/")[-1])
            deployed = await meta.deploy_and_wait(build_deploy_package(files, types, spec.api_version), check_only=False)
            if not deployed.success:
                detail = "; ".join(str(f) for f in deployed.failures) or deployed.error_message or deployed.status
                add("Messaging", "failed", detail)
                return result
            add("Messaging", "done", f"channel {spec.messaging.channel}, deployment {spec.messaging.deployment}")

        # 5. publish
        await org.post_json(f"connect/communities/{site_id}/publish", {})
        add("Publish", "done", "publishing runs in the background; validation waits for the page")

        # 6. validate
        site = await _find_site(org, spec) or site
        result.site_url = site.get("siteUrl")
        result.register_url = f"{result.site_url}/SelfRegister" if result.site_url else None
        result.validation = await validate_site(org, spec, site, network_id, handler_id, meta)
        add("Validate", "done" if not any(c.blocks for c in result.validation) else "failed")
    except (OrgQueryError, TimeoutError, RuntimeError, KeyError, IndexError) as exc:
        add("Pipeline", "failed", f"{type(exc).__name__}: {exc}")

    result.manual_steps = [
        f"Register once yourself at {result.register_url} with a throwaway email, then check the new User/Contact "
        "(the tool does not create accounts on the live site).",
    ] if result.register_url else []
    if spec.messaging is not None and result.register_url:
        result.manual_steps.append(
            f"Chat button: publish the '{spec.messaging.deployment}' Embedded Service Deployment in Setup and copy its "
            "Code Snippet values (scrtUrl, siteEndpoint) - the page component needs them; the tool does not have them."
        )
    return result


async def validate_site(org, spec: SiteSpec, site: Dict[str, Any], network_id: str, handler_id: str, meta=None) -> List[Check]:
    out: List[Check] = []

    def chk(id_: str, title: str, ok: bool, msg: str, fix: str = "", severity: str = BLOCKING) -> None:
        out.append(Check(id_, title, severity, PASS if ok else FAIL, msg, "" if ok else fix))

    chk("site_live", "Site is Live", site.get("status") == "Live", f"status = {site.get('status')}",
        "Set Network.status to Live and publish again.")

    regs = await org.query(
        f"SELECT ApexHandlerId, AccountId, ExecuteApexHandlerAsId FROM NetworkSelfRegistration WHERE NetworkId = '{network_id}'"
    )
    chk("registration_record", "Self-registration configured", bool(regs) and regs[0].get("ApexHandlerId") == handler_id,
        "record present with the handler" if regs else "no NetworkSelfRegistration record",
        "Create the NetworkSelfRegistration record (Account, handler, Execute-As user).")

    cls = await org.query(f"SELECT Status FROM ApexClass WHERE Id = '{handler_id}'")
    chk("handler_active", "Registration handler is active", bool(cls) and cls[0]["Status"] == "Active", "ApexClass.Status")

    base = site.get("siteUrl")
    if base:
        # The register page 404s until the first publish finishes, so give it a minute.
        status = 0
        for _ in range(12):
            status, _loc = await org.http_probe(f"{base}/SelfRegister")
            if status == 200:
                break
            await asyncio.sleep(spec.poll_seconds)
        chk("register_page", "Register page reachable", status == 200, f"GET /SelfRegister -> {status}",
            "Wait for the publish job, or publish again.")
        # Not following redirects: a site that sends every visitor to login answers
        # the landing page with 302 -> /login, which a following client reports as 200.
        status, loc = await org.http_probe(base)
        if status in (301, 308) and loc.rstrip("/") == base.rstrip("/"):
            status, loc = await org.http_probe(loc)  # canonical trailing-slash redirect, not a login bounce
        chk("landing_public", "Landing page public (no login)", status == 200,
            f"GET / -> {status}" + (f" -> {loc}" if loc else ""),
            "Set the site's authenticationType to AUTHENTICATED_WITH_PUBLIC_ACCESS_ENABLED and publish again.")
        if spec.content is not None:
            status, loc = await org.http_probe(f"{base}/{spec.content.secure_url}")
            chk("secure_requires_login", "My Account page requires login",
                status in (301, 302, 303) and "login" in loc.lower(),
                f"GET /{spec.content.secure_url} -> {status} {loc}".strip(),
                "The route's pageAccess must be RequiresLogin.")

    if spec.content is not None and meta is not None:
        sets = [e["full_name"] for e in await meta.list_metadata("SharingSet")]
        chk("sharing_set", "Sharing Set exists", spec.sharing_set_name in sets,
            f"{spec.sharing_set_name} present" if spec.sharing_set_name in sets else "missing",
            "Deploy the SharingSet for the member profile.")
        found_member = await org.query(f"SELECT Id FROM Profile WHERE Name = '{_q(spec.member_profile)}'")
        member_perms = await org.query(
            "SELECT SobjectType, PermissionsRead, PermissionsEdit FROM ObjectPermissions WHERE "
            f"Parent.Profile.Name = '{_q(spec.member_profile)}' AND SobjectType IN ('Contact', 'Account')"
        ) if found_member else []
        by_type = {p["SobjectType"]: p for p in member_perms}
        ok = (by_type.get("Contact", {}).get("PermissionsRead") and by_type.get("Contact", {}).get("PermissionsEdit")
              and by_type.get("Account", {}).get("PermissionsRead"))
        chk("member_permissions", "Members can read/edit Contact, read Account", bool(ok),
            "Contact R/E, Account R" if ok else f"found: {sorted(by_type)}",
            f"Grant those object permissions on the '{spec.member_profile}' profile.")

    if spec.messaging is not None:
        chan = await org.query(
            f"SELECT Id FROM MessagingChannel WHERE DeveloperName = '{_q(spec.messaging.channel)}'"
        )
        chk("messaging_channel", "Messaging channel exists", bool(chan),
            spec.messaging.channel if chan else "missing", "Deploy the MessagingChannel.")
        svc = await org.query(
            f"SELECT Id FROM EmbeddedServiceConfig WHERE DeveloperName = '{_q(spec.messaging.deployment)}'",
            tooling=True,
        )
        chk("messaging_deployment", "Embedded Service Deployment exists", bool(svc),
            spec.messaging.deployment if svc else "missing", "Deploy the EmbeddedServiceConfig.")

    guest = f"{spec.site_name} Profile"
    # An absent profile also yields zero permission rows, which would read as
    # "no access" - so prove the profile exists before trusting an empty answer.
    found = await org.query(f"SELECT Id FROM Profile WHERE Name = '{_q(guest)}'")
    if not found:
        chk("guest_no_pii", "Guest cannot read Contact/Account", False,
            f"guest profile '{guest}' not found, so access could not be checked",
            "Check the guest user's profile name in Setup > Sites.")
        return out
    perms = await org.query(
        "SELECT SobjectType, PermissionsRead FROM ObjectPermissions WHERE "
        f"Parent.Profile.Name = '{_q(guest)}' AND SobjectType IN ('Contact', 'Account')"
    )
    leaked = [p["SobjectType"] for p in perms if p.get("PermissionsRead")]
    chk("guest_no_pii", "Guest cannot read Contact/Account", not leaked,
        "no read access" if not leaked else f"guest profile can read: {', '.join(leaked)}",
        f"Remove Read on {', '.join(leaked)} from the '{guest}' profile.")
    return out


async def _bundle_name(meta, spec: SiteSpec) -> str:
    """The LWR site's DigitalExperienceBundle, e.g. 'site/Member_Hub1'."""
    names = [e["full_name"] for e in await meta.list_metadata("DigitalExperienceBundle")]
    stem = "site/" + spec.site_name.replace(" ", "_")
    matches = sorted(n for n in names if n and n.startswith(stem))
    if not matches:
        raise RuntimeError(f"no DigitalExperienceBundle named like {stem!r} (is this an LWR site?)")
    return matches[-1]


async def _deploy_pages(meta, spec: SiteSpec, content: "pages.PortalContent") -> str:
    """Retrieve the generated bundle, add/replace our pages, and deploy them with
    the LWC, Sharing Set and profile permissions in one transaction."""
    bundle = await _bundle_name(meta, spec)
    root = f"digitalExperiences/{bundle}"

    zip_bytes = await meta.wait_for_retrieve(
        await meta.start_retrieve_types({"DigitalExperienceBundle": [bundle]})
    )
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as archive:
        read = lambda path: archive.read(f"{root}/{path}").decode("utf-8")  # noqa: E731
        bundle_meta = read(f"{bundle.split('/')[-1]}.digitalExperience-meta.xml")
        site_dir = next(n for n in archive.namelist() if n.startswith(f"{root}/sfdc_cms__site/") and n.endswith("/content.json"))
        site_rel = site_dir[len(root) + 1:]
        site_content = read(site_rel)
        site_meta = read(site_rel.replace("content.json", "_meta.json"))
        login_view = json.loads(read("sfdc_cms__view/login/content.json"))
        register_view = json.loads(read("sfdc_cms__view/register/content.json"))

    start_url = "/" + content.secure_url
    files: Dict[str, str] = {
        f"{root}/{bundle.split('/')[-1]}.digitalExperience-meta.xml": bundle_meta,
        f"{root}/{site_rel}": pages.public_site_content(site_content),
        f"{root}/{site_rel.replace('content.json', '_meta.json')}": site_meta,
        f"{root}/sfdc_cms__view/login/content.json": json.dumps(pages.with_start_url(login_view, start_url), indent=2) + "\n",
        f"{root}/sfdc_cms__view/login/_meta.json": json.dumps({"apiName": "login", "type": "sfdc_cms__view", "path": "views"}, indent=2) + "\n",
        f"{root}/sfdc_cms__view/register/content.json": json.dumps(pages.with_start_url(register_view, start_url), indent=2) + "\n",
        f"{root}/sfdc_cms__view/register/_meta.json": json.dumps({"apiName": "register", "type": "sfdc_cms__view", "path": "views"}, indent=2) + "\n",
    }
    for path, text in pages.bundle_files(content).items():
        files[f"{root}/{path}"] = text
    files.update(pages.details_files(content, spec.api_version))
    files[f"sharingSets/{spec.sharing_set_name}.sharingSet"] = pages.sharing_set_xml(spec.sharing_set_name, spec.member_profile)
    files[f"profiles/{spec.member_profile}.profile"] = pages.member_profile_xml()

    types = {
        "DigitalExperienceBundle": [bundle],
        "LightningComponentBundle": [content.secure_component],
        "SharingSet": [spec.sharing_set_name],
        "Profile": [spec.member_profile],
    }
    result = await meta.deploy_and_wait(build_deploy_package(files, types, spec.api_version), check_only=False)
    if not result.success:
        raise RuntimeError("pages deploy failed: " + ("; ".join(str(f) for f in result.failures) or result.error_message or result.status))
    return f"{bundle}: {len(files)} files"


def format_result(result: SiteResult) -> str:
    lines = []
    if not result.preflight.can_proceed:
        from flowtool.portal_preflight import format_report
        return format_report(result.preflight)
    for s in result.steps:
        lines.append(f"[{s.status:<7}] {s.name}" + (f": {s.detail}" if s.detail else ""))
    for c in result.validation:
        lines.append(f"  validate [{c.status}] {c.title}: {c.message}")
    if result.register_url:
        lines.append(f"\nRegister page: {result.register_url}")
    for m in result.manual_steps:
        lines.append(f"MANUAL: {m}")
    return "\n".join(lines)


def main() -> None:
    import argparse

    from flowtool.orgs import get_org
    from flowtool.sfdc import MetadataClient

    parser = argparse.ArgumentParser(description="Create an LWR site with self-registration")
    parser.add_argument("--org", required=True)
    parser.add_argument("--site-name", required=True)
    parser.add_argument("--prefix")
    parser.add_argument("--admin")
    parser.add_argument("--headline", default=None, help="landing page headline; omit to skip the pages step")
    parser.add_argument("--intro", default="Create an account to manage your details.")
    parser.add_argument("--chat", action="store_true", help="also deploy the MIAW chat chain (channel, queue, deployment)")
    parser.add_argument("--reuse", action="store_true", help="the site already exists; reuse and update it")
    parser.add_argument("--apply", action="store_true", help="actually write to the org (default: plan only)")
    args = parser.parse_args()

    creds = get_org(args.org)
    content = pages.PortalContent(site_name=args.site_name, headline=args.headline, intro=args.intro) if args.headline else None
    spec = SiteSpec(site_name=args.site_name, content=content,
                    # Names carry the site: a deployment is bound to one site and cannot be re-pointed.
                    messaging=miaw.MiawSpec(channel_label=f"{args.site_name} Chat",
                                            queue_label=f"{args.site_name} Support") if args.chat else None,
                    want_miaw=args.chat, url_path_prefix=args.prefix, admin_username=args.admin,
                    allow_existing_site=args.reuse)

    async def run() -> SiteResult:
        org = OrgWriter(creds.instance_url, creds.access_token)
        async with MetadataClient(creds.instance_url, creds.access_token, spec.api_version) as meta:
            return await build_site(org, meta, spec, apply=args.apply)

    result = asyncio.run(run())
    print(format_result(result))
    raise SystemExit(0 if result.ok else 1)


if __name__ == "__main__":
    main()
