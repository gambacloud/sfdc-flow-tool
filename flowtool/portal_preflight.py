"""
Preflight for the Experience Cloud portal builder: the PRE-DEPLOY checklist in
docs/portal-builder-plan.md, run read-only against an org.

Every check returns a Check; none of them writes anything. A check that cannot
run (the org refused the query) comes back as status "error" rather than
"pass" - an unanswered question is not a green light.

    python -m flowtool.portal_preflight --org forge-scratch --site-name "Customer Portal"
"""

from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import asdict, dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional

import httpx

from flowtool.sfdc import _normalise

BLOCKING = "blocking"
WARNING = "warning"
PASS, FAIL, SKIP, ERROR = "pass", "fail", "skip", "error"

# License names that give an external user a login. "Customer Community" is
# the sharing-set model, "Plus" the role/sharing-rule model; External Identity
# is login-only (no Contact/Account sharing), so it never satisfies a portal
# that shows Contact/Account data.
_LICENSES = {
    "customer_community": ["Customer Community", "Customer Community Login"],
    "customer_community_plus": ["Customer Community Plus", "Customer Community Plus Login"],
}


class OrgQueryError(RuntimeError):
    def __init__(self, message: str, code: str = ""):
        super().__init__(message)
        self.code = code


@dataclass
class PortalSpec:
    site_name: str
    url_path_prefix: Optional[str] = None
    license: str = "customer_community"  # key of _LICENSES
    admin_username: Optional[str] = None  # site admin / Execute-As; default: the connected user
    want_miaw: bool = False
    # Re-running against a site this tool already made: an existing site of the
    # same name is then expected, not a conflict.
    allow_existing_site: bool = False
    # "Object.Field" the portal pages will read; must already exist in the org
    # (or be created earlier in the same plan - pass those in `planned_fields`).
    required_fields: List[str] = field(default_factory=list)
    planned_fields: List[str] = field(default_factory=list)

    def prefix(self) -> str:
        if self.url_path_prefix:
            return self.url_path_prefix
        return "".join(c for c in self.site_name.lower() if c.isalnum())


@dataclass
class Check:
    id: str
    title: str
    severity: str
    status: str
    message: str = ""
    fix_hint: str = ""

    @property
    def blocks(self) -> bool:
        return self.severity == BLOCKING and self.status in (FAIL, ERROR)


@dataclass
class PreflightReport:
    checks: List[Check]

    @property
    def can_proceed(self) -> bool:
        return not any(c.blocks for c in self.checks)

    def to_dict(self) -> Dict[str, Any]:
        return {"can_proceed": self.can_proceed, "checks": [asdict(c) for c in self.checks]}


class OrgClient:
    """Minimal read-only REST client: SOQL (standard or Tooling) and describe."""

    def __init__(self, instance_url: str, access_token: str, api_version: str = "64.0"):
        self.base = _normalise(instance_url)
        self.api_version = api_version
        self._headers = {"Authorization": f"Bearer {access_token}"}

    def _url(self, path: str) -> str:
        return f"{self.base}/services/data/v{self.api_version}/{path}"

    @staticmethod
    def _raise(resp: httpx.Response) -> None:
        try:
            first = resp.json()[0]
            raise OrgQueryError(first.get("message", resp.text[:300]), first.get("errorCode", ""))
        except (ValueError, KeyError, IndexError, TypeError):
            raise OrgQueryError(f"{resp.status_code}: {resp.text[:300]}") from None

    async def query(self, soql: str, tooling: bool = False) -> List[Dict[str, Any]]:
        path = "tooling/query" if tooling else "query"
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.get(self._url(path), params={"q": soql}, headers=self._headers)
        if resp.status_code != 200:
            self._raise(resp)
        return resp.json().get("records", [])

    async def current_user_id(self) -> str:
        """The user that owns this token (SOQL has no UserInfo)."""
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(f"{self.base}/services/oauth2/userinfo", headers=self._headers)
        if resp.status_code != 200:
            raise OrgQueryError(f"userinfo failed ({resp.status_code})")
        return resp.json()["user_id"]

    async def describe(self, sobject: str) -> Dict[str, Any]:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.get(self._url(f"sobjects/{sobject}/describe"), headers=self._headers)
        if resp.status_code != 200:
            self._raise(resp)
        return resp.json()


def _q(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\\'")


# --- individual checks ------------------------------------------------------
# Each takes (org, spec, ctx) and returns a Check. ctx carries facts one check
# learned that a later one needs (the connected user, whether this is prod).

Ctx = Dict[str, Any]
CheckFn = Callable[[Any, PortalSpec, Ctx], Awaitable[Check]]


async def check_org(org, spec: PortalSpec, ctx: Ctx) -> Check:
    rows = await org.query("SELECT Name, IsSandbox, OrganizationType, TrialExpirationDate FROM Organization")
    row = rows[0]
    ctx["is_production"] = not row.get("IsSandbox") and "Developer" not in str(row.get("OrganizationType"))
    ctx["org_row"] = row
    if ctx["is_production"]:
        return Check("org", "Target org", WARNING, FAIL,
                     f"{row['Name']} looks like a production org ({row.get('OrganizationType')}).",
                     "Build in a sandbox or scratch org first; publish to production only with explicit confirmation.")
    return Check("org", "Target org", WARNING, PASS, f"{row['Name']} ({row.get('OrganizationType')}) - not production.")


async def check_digital_experiences(org, spec, ctx) -> Check:
    try:
        await org.query("SELECT Id FROM Network LIMIT 1")
    except OrgQueryError as exc:
        if exc.code in ("INVALID_TYPE", "NOT_FOUND"):
            return Check("digital_experiences", "Digital Experiences enabled", BLOCKING, FAIL,
                         "The Network object is not available.",
                         "Setup > Digital Experiences > Settings > Enable Digital Experiences (or the Communities feature in a scratch org definition).")
        raise
    return Check("digital_experiences", "Digital Experiences enabled", BLOCKING, PASS, "Network object is available.")


async def check_licenses(org, spec, ctx) -> Check:
    names = _LICENSES.get(spec.license)
    if names is None:
        return Check("licenses", "User licenses", BLOCKING, FAIL,
                     f"Unknown license model {spec.license!r}.", f"Use one of {sorted(_LICENSES)}.")
    quoted = ", ".join(f"'{n}'" for n in names)
    rows = await org.query(
        f"SELECT Name, Status, TotalLicenses, UsedLicenses FROM UserLicense WHERE Name IN ({quoted})"
    )
    free = {r["Name"]: (r["TotalLicenses"] or 0) - (r["UsedLicenses"] or 0) for r in rows if r.get("Status") == "Active"}
    ctx["license_free"] = free
    usable = {n: f for n, f in free.items() if f > 0}
    if usable:
        detail = ", ".join(f"{n}: {f} free" for n, f in sorted(usable.items()))
        return Check("licenses", "User licenses", BLOCKING, PASS, detail)
    if not free:
        return Check("licenses", "User licenses", BLOCKING, FAIL,
                     f"No active {' / '.join(names)} license in this org.",
                     "Licenses cannot be bought by this tool; get them added to the org.")
    return Check("licenses", "User licenses", BLOCKING, FAIL,
                 "All matching licenses are fully used: " + ", ".join(f"{n} ({f} free)" for n, f in free.items()),
                 "Free a license or buy more.")


async def check_site_name_free(org, spec, ctx) -> Check:
    prefix = spec.prefix()
    rows = await org.query(
        f"SELECT Name, UrlPathPrefix, Status FROM Network WHERE Name = '{_q(spec.site_name)}' "
        f"OR UrlPathPrefix = '{_q(prefix)}'"
    )
    if rows and spec.allow_existing_site:
        return Check("site_name", "Site name and URL prefix free", BLOCKING, PASS,
                     f"'{spec.site_name}' already exists and will be reused.")
    if rows:
        taken = ", ".join(f"{r['Name']} (/{r['UrlPathPrefix']}, {r['Status']})" for r in rows)
        return Check("site_name", "Site name and URL prefix free", BLOCKING, FAIL,
                     f"Already used: {taken}.", "Choose another site name or url path prefix.")
    return Check("site_name", "Site name and URL prefix free", BLOCKING, PASS,
                 f"'{spec.site_name}' and /{prefix} are unused.")


async def check_domain(org, spec, ctx) -> Check:
    rows = await org.query("SELECT Domain, DomainType FROM Domain")
    if not rows:
        return Check("domain", "Site domain", BLOCKING, FAIL, "The org has no domain for sites.",
                     "Setup > Digital Experiences > Settings: choose a domain and save.")
    ctx["domains"] = [r["Domain"] for r in rows]
    return Check("domain", "Site domain", BLOCKING, PASS, rows[0]["Domain"])


async def check_admin_user(org, spec, ctx) -> Check:
    if spec.admin_username:
        where = f"Username = '{_q(spec.admin_username)}'"
    else:
        where = f"Id = '{await org.current_user_id()}'"
    rows = await org.query(
        f"SELECT Username, IsActive, Profile.Name, Profile.PermissionsManageUsers FROM User WHERE {where}"
    )
    if not rows:
        return Check("admin_user", "Site admin / Execute-As user", BLOCKING, FAIL,
                     f"User {spec.admin_username!r} not found.", "Pass an existing active admin username.")
    user = rows[0]
    ctx["admin_username"] = user["Username"]
    problems = []
    if not user["IsActive"]:
        problems.append("is inactive")
    if not (user.get("Profile") or {}).get("PermissionsManageUsers"):
        problems.append("lacks Manage Users on its profile (a permission set may still grant it - verify)")
    if problems:
        return Check("admin_user", "Site admin / Execute-As user", BLOCKING, FAIL,
                     f"{user['Username']} " + " and ".join(problems) + ".",
                     "The self-registration handler runs as this user; use an active System Administrator.")
    return Check("admin_user", "Site admin / Execute-As user", BLOCKING, PASS, user["Username"])


async def check_miaw(org, spec, ctx) -> Check:
    if not spec.want_miaw:
        return Check("miaw", "Messaging (MIAW) prerequisites", BLOCKING, SKIP, "Not requested.")
    rows = await org.query("SELECT DeveloperName FROM ServiceChannel WHERE DeveloperName = 'sfdc_livemessage'")
    if not rows:
        return Check("miaw", "Messaging (MIAW) prerequisites", BLOCKING, FAIL,
                     "The Messaging service channel is missing, so Omni-Channel / Messaging is not enabled.",
                     "Setup > Omni-Channel Settings > Enable Omni-Channel, then Messaging Settings > Enable Messaging.")
    chat = getattr(spec, "messaging", None)
    if chat is not None and not spec.allow_existing_site:
        # An Embedded Service Deployment is bound to one site for good: the org rejects
        # re-pointing it ("This field can't be changed"), so a name clash is a hard stop.
        taken = await org.query(
            f"SELECT DeveloperName FROM EmbeddedServiceConfig WHERE DeveloperName = '{_q(chat.deployment)}'",
            tooling=True,
        )
        if taken:
            return Check("miaw", "Messaging (MIAW) prerequisites", BLOCKING, FAIL,
                         f"Deployment '{chat.deployment}' already exists and is bound to a site; it cannot be moved.",
                         "Use a different channel label for this site.")
    return Check("miaw", "Messaging (MIAW) prerequisites", BLOCKING, PASS, "Messaging service channel is present.")


async def check_person_accounts(org, spec, ctx) -> Check:
    try:
        await org.query("SELECT IsPersonAccount FROM Account LIMIT 1")
    except OrgQueryError:
        return Check("person_accounts", "Person Accounts", WARNING, PASS, "Not enabled - Contacts register under a business Account.")
    return Check("person_accounts", "Person Accounts", WARNING, FAIL,
                 "Person Accounts are enabled; the registration model differs (a Person Account is the user's contact).",
                 "Decide B2C (Person Account) vs B2B (Contact under an Account) before building the handler.")


async def check_sharing_model(org, spec, ctx) -> Check:
    rows = await org.query("SELECT DefaultAccountAccess, DefaultContactAccess FROM Organization")
    row = rows[0]
    private = [k for k in ("DefaultAccountAccess", "DefaultContactAccess") if row.get(k) in ("None", "Private", "ControlledByParent")]
    if private and spec.license == "customer_community":
        return Check("sharing_model", "External sharing for Account/Contact", WARNING, FAIL,
                     f"Internal default is private/controlled ({', '.join(private)}). "
                     "Customer Community users only see records through a Sharing Set.",
                     "The build must include a Sharing Set mapping User.ContactId/AccountId to the records.")
    return Check("sharing_model", "External sharing for Account/Contact", WARNING, PASS,
                 f"Account: {row.get('DefaultAccountAccess')}, Contact: {row.get('DefaultContactAccess')}.")


async def check_required_fields(org, spec, ctx) -> Check:
    if not spec.required_fields:
        return Check("fields", "Fields the portal reads", BLOCKING, SKIP, "None listed.")
    planned = {f.lower() for f in spec.planned_fields}
    cache: Dict[str, set] = {}
    missing = []
    for ref in spec.required_fields:
        obj, _, fld = ref.partition(".")
        if ref.lower() in planned:
            continue
        if obj not in cache:
            try:
                cache[obj] = {f["name"].lower() for f in (await org.describe(obj))["fields"]}
            except OrgQueryError:
                cache[obj] = set()
        if fld.lower() not in cache[obj]:
            missing.append(ref)
    if missing:
        return Check("fields", "Fields the portal reads", BLOCKING, FAIL,
                     "Missing: " + ", ".join(missing) + ".",
                     "Create them (add an object/field step to the plan) or remove them from the pages.")
    return Check("fields", "Fields the portal reads", BLOCKING, PASS, f"{len(spec.required_fields)} field(s) found or planned.")


CHECKS: List[CheckFn] = [
    check_org,
    check_digital_experiences,
    check_licenses,
    check_site_name_free,
    check_domain,
    check_admin_user,
    check_miaw,
    check_person_accounts,
    check_sharing_model,
    check_required_fields,
]

_META = {
    "check_org": ("org", "Target org", WARNING),
    "check_digital_experiences": ("digital_experiences", "Digital Experiences enabled", BLOCKING),
    "check_licenses": ("licenses", "User licenses", BLOCKING),
    "check_site_name_free": ("site_name", "Site name and URL prefix free", BLOCKING),
    "check_domain": ("domain", "Site domain", BLOCKING),
    "check_admin_user": ("admin_user", "Site admin / Execute-As user", BLOCKING),
    "check_miaw": ("miaw", "Messaging (MIAW) prerequisites", BLOCKING),
    "check_person_accounts": ("person_accounts", "Person Accounts", WARNING),
    "check_sharing_model": ("sharing_model", "External sharing for Account/Contact", WARNING),
    "check_required_fields": ("fields", "Fields the portal reads", BLOCKING),
}


async def run_preflight(org, spec: PortalSpec) -> PreflightReport:
    """Run every check. One failing query never hides the others."""
    ctx: Ctx = {}
    checks: List[Check] = []
    for fn in CHECKS:
        try:
            checks.append(await fn(org, spec, ctx))
        except OrgQueryError as exc:
            cid, title, severity = _META[fn.__name__]
            checks.append(Check(cid, title, severity, ERROR, f"Could not check: {exc}",
                                "The org refused the query; confirm the connected user can read this."))
    return PreflightReport(checks)


def format_report(report: PreflightReport) -> str:
    mark = {PASS: "PASS ", FAIL: "FAIL ", SKIP: "skip ", ERROR: "ERROR"}
    lines = []
    for c in report.checks:
        lines.append(f"[{mark[c.status]}] ({c.severity}) {c.title}: {c.message}")
        if c.status in (FAIL, ERROR) and c.fix_hint:
            lines.append(f"         fix: {c.fix_hint}")
    lines.append("")
    lines.append("Can proceed." if report.can_proceed else "BLOCKED - fix the blocking items above.")
    return "\n".join(lines)


def main() -> None:
    from flowtool.orgs import get_org

    parser = argparse.ArgumentParser(description="Read-only portal preflight")
    parser.add_argument("--org", help="sf CLI alias (default org if omitted)")
    parser.add_argument("--site-name", required=True)
    parser.add_argument("--prefix")
    parser.add_argument("--license", default="customer_community", choices=sorted(_LICENSES))
    parser.add_argument("--admin")
    parser.add_argument("--miaw", action="store_true")
    parser.add_argument("--field", action="append", default=[], help="Object.Field the pages read")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    creds = get_org(args.org)
    spec = PortalSpec(args.site_name, args.prefix, args.license, args.admin, args.miaw, args.field)
    report = asyncio.run(run_preflight(OrgClient(creds.instance_url, creds.access_token), spec))
    print(json.dumps(report.to_dict(), indent=2) if args.json else format_report(report))
    raise SystemExit(0 if report.can_proceed else 1)


if __name__ == "__main__":
    main()
