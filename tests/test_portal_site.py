"""portal_site: pure helpers, plus the pipeline against a scripted fake org."""

import asyncio
import io
import json
import zipfile
from xml.etree import ElementTree as ET

import pytest

from flowtool.portal_site import (
    METADATA_NS, SiteSpec, build_site, handler_source, network_zip_member, patch_network_xml,
)
from flowtool.portal_pages import PortalContent
from flowtool.sfdc import DeployResult
from tests.test_portal_preflight import FakeOrg, healthy

NETWORK = f"""<?xml version="1.0" encoding="UTF-8"?>
<Network xmlns="{METADATA_NS}">
    <enableDirectMessages>true</enableDirectMessages>
    <networkMemberGroups>
        <profile>admin</profile>
    </networkMemberGroups>
    <networkPageOverrides>
        <loginPageOverrideSetting>Designer</loginPageOverrideSetting>
        <selfRegProfilePageOverrideSetting>Designer</selfRegProfilePageOverrideSetting>
    </networkPageOverrides>
    <selfRegistration>false</selfRegistration>
    <status>UnderConstruction</status>
</Network>
"""


def text(xml, path):
    root = ET.fromstring(xml)
    node = root
    for part in path.split("/"):
        node = node.find(f"{{{METADATA_NS}}}{part}")
        assert node is not None, path
    return node.text


def test_patch_network_enables_self_registration():
    out = patch_network_xml(NETWORK, "Customer Community User")
    assert text(out, "selfRegistration") == "true"
    assert text(out, "status") == "Live"
    assert text(out, "networkPageOverrides/selfRegProfilePageOverrideSetting") == "Configurable"
    profiles = [p.text for p in ET.fromstring(out).iter(f"{{{METADATA_NS}}}profile")]
    assert profiles == ["admin", "Customer Community User"]


def test_patch_network_keeps_everything_else_and_is_idempotent():
    once = patch_network_xml(NETWORK, "Customer Community User")
    twice = patch_network_xml(once, "customer community user")
    assert once == twice
    assert text(once, "enableDirectMessages") == "true"
    assert text(once, "networkPageOverrides/loginPageOverrideSetting") == "Designer"


def test_patch_network_inserts_missing_elements():
    bare = f'<?xml version="1.0"?><Network xmlns="{METADATA_NS}"></Network>'
    out = patch_network_xml(bare, "P", go_live=False)
    assert text(out, "selfRegistration") == "true"
    assert text(out, "networkPageOverrides/selfRegProfilePageOverrideSetting") == "Configurable"
    assert ET.fromstring(out).find(f"{{{METADATA_NS}}}status") is None


def test_handler_source_names_the_class_and_interface():
    src = handler_source("MyHandler")
    assert "global class MyHandler implements Auth.ConfigurableSelfRegHandler" in src
    assert "Site.createExternalUser" in src


def make_zip(path, content="<x/>"):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(path, content)
    return buf.getvalue()


def test_network_zip_member_matches_case_insensitively():
    assert network_zip_member(make_zip("networks/Forge LWR.network"), "forge lwr") == "networks/Forge LWR.network"
    with pytest.raises(RuntimeError):
        network_zip_member(make_zip("networks/Other.network"), "Forge LWR")


# --- pipeline ---------------------------------------------------------------

class FakeWriter(FakeOrg):
    def __init__(self, answers, sites=(), http=None):
        super().__init__(answers)
        self.sites = list(sites)
        self.posts = []
        self.created = []
        self.http = {"https://x.my.site.com/portal/my-account": (302, "https://x.my.site.com/portal/login?startURL=%2Fmy-account")}
        self.http.update(http or {})

    async def get_json(self, path):
        assert path == "connect/communities"
        return {"communities": self.sites}

    async def post_json(self, path, body):
        self.posts.append((path, body))
        if path == "connect/communities":
            self.sites.append({"id": "0DB1", "name": body["name"], "status": "Live",
                               "siteUrl": "https://x.my.site.com/portal"})
        return {}

    async def create(self, sobject, fields):
        self.created.append((sobject, fields))
        return "001NEW"

    async def http_probe(self, url):
        got = self.http.get(url, 200)
        return got if isinstance(got, tuple) else (got, "")


def bundle_zip():
    root = "digitalExperiences/site/Customer_Portal1"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(f"{root}/Customer_Portal1.digitalExperience-meta.xml", "<DigitalExperienceBundle/>")
        z.writestr(f"{root}/sfdc_cms__site/Customer_Portal1/content.json",
                   json.dumps({"type": "sfdc_cms__site", "title": "Customer Portal", "urlName": "customer-portal",
                               "contentBody": {"authenticationType": "AUTHENTICATED", "geoBotsAllowed": False}}))
        z.writestr(f"{root}/sfdc_cms__site/Customer_Portal1/_meta.json", "{}")
        for view, comp in (("login", "community_login:loginForm"), ("register", "community_login:selfRegister")):
            z.writestr(f"{root}/sfdc_cms__view/{view}/content.json", json.dumps(
                {"contentBody": {"component": {"children": [{"definition": comp, "attributes": {}, "id": "a"}]}}}))
    return buf.getvalue()


class FakeMeta:
    def __init__(self, deploy_ok=True, sharing_sets=("Portal_Users",)):
        self.deployed = []
        self.deploy_ok = deploy_ok
        self.sharing_sets = list(sharing_sets)

    async def start_retrieve_types(self, types):
        self.types = types
        return "job"

    async def wait_for_retrieve(self, job):
        if "DigitalExperienceBundle" in self.types:
            return bundle_zip()
        return make_zip("networks/Customer Portal.network", NETWORK)

    async def list_metadata(self, type_name):
        names = {"DigitalExperienceBundle": ["site/Customer_Portal1", "site/Other1"],
                 "SharingSet": self.sharing_sets}[type_name]
        return [{"full_name": n} for n in names]

    async def deploy_and_wait(self, zip_bytes, check_only=True):
        self.deployed.append((zip_bytes, check_only))
        return DeployResult(id="0Af", status="Succeeded" if self.deploy_ok else "Failed", success=self.deploy_ok)


def answers():
    base = healthy()
    # Most specific needles first: FakeOrg answers with the first substring match,
    # and the preflight's generic "WHERE Name = " would otherwise swallow them.
    a = {
        "FROM Account WHERE Name": [],
        "SELECT Id FROM Network WHERE Name = 'Customer Portal'": [{"Id": "0DM1"}],
        "FROM ApexClass WHERE Name": [{"Id": "01p1"}],
        "FROM NetworkSelfRegistration": [{"ApexHandlerId": "01p1", "AccountId": "001NEW", "ExecuteApexHandlerAsId": "005XX"}],
        "FROM ApexClass WHERE Id": [{"Status": "Active"}],
        "Parent.Profile.Name = 'Customer Community User'": [
            {"SobjectType": "Contact", "PermissionsRead": True, "PermissionsEdit": True},
            {"SobjectType": "Account", "PermissionsRead": True, "PermissionsEdit": False}],
        "FROM ObjectPermissions": [{"SobjectType": "Contact", "PermissionsRead": False}],
        "FROM Profile WHERE Name": [{"Id": "00eGUEST"}],
    }
    a.update(base)
    return a


def run(org, meta, content=None, **kw):
    spec = SiteSpec("Customer Portal", poll_seconds=0, content=content, **kw)
    return asyncio.run(build_site(org, meta, spec, apply=APPLY[0]))


APPLY = [True]


def portal_content():
    return PortalContent(site_name="Customer Portal", headline="Hi")


def test_plan_only_writes_nothing():
    APPLY[0] = False
    org, meta = FakeWriter(answers()), FakeMeta()
    result = run(org, meta)
    APPLY[0] = True
    assert [s.status for s in result.steps] == ["planned"] * 7
    assert not org.posts and not org.created and not meta.deployed


def test_blocked_preflight_stops_before_any_write():
    a = answers()
    a["FROM Domain"] = []
    org, meta = FakeWriter(a), FakeMeta()
    result = run(org, meta)
    assert not result.preflight.can_proceed and not result.steps
    assert not org.posts and not org.created and not meta.deployed


def test_full_pipeline_creates_validates_and_asks_for_manual_registration():
    a = answers()
    # NetworkSelfRegistration: first lookup (existence) empty, later validation sees it.
    seen = {"n": 0}
    real_query = FakeWriter.query

    async def query(self, soql, tooling=False):
        if "FROM NetworkSelfRegistration" in soql and "ApexHandlerId" not in soql:
            seen["n"] += 1
            return []
        return await real_query(self, soql, tooling)

    FakeWriter.query = query
    try:
        org, meta = FakeWriter(a), FakeMeta()
        result = run(org, meta)
    finally:
        FakeWriter.query = real_query

    assert result.ok, [(s.name, s.status, s.detail) for s in result.steps]
    assert [p[0] for p in org.posts] == ["connect/communities", "connect/communities/0DB1/publish"]
    assert org.posts[0][1] == {"name": "Customer Portal", "urlPathPrefix": "customerportal",
                               "templateName": "Build Your Own (LWR)"}
    kinds = [c[0] for c in org.created]
    assert kinds == ["Account", "NetworkSelfRegistration"]
    reg = dict(org.created)["NetworkSelfRegistration"]
    assert reg["AccountId"] == "001NEW" and reg["ApexHandlerId"] == "01p1" and reg["ExecuteApexHandlerAsId"] == "005XX"
    zip_bytes, check_only = meta.deployed[0]
    assert check_only is False
    names = zipfile.ZipFile(io.BytesIO(zip_bytes)).namelist()
    assert "classes/ForgeSelfRegHandler.cls" in names and "networks/Customer Portal.network" in names
    assert result.register_url == "https://x.my.site.com/portal/SelfRegister"
    assert result.manual_steps and "throwaway email" in result.manual_steps[0]


def test_deploy_failure_stops_before_data_and_publish():
    org, meta = FakeWriter(answers()), FakeMeta(deploy_ok=False)
    result = run(org, meta)
    assert not result.ok
    assert [s.status for s in result.steps if s.name == "Network + handler"] == ["failed"]
    assert not any(p[0].endswith("/publish") for p in org.posts)
    assert not any(c[0] == "NetworkSelfRegistration" for c in org.created)


def test_existing_registration_record_is_not_modified():
    org, meta = FakeWriter(answers()), FakeMeta()
    result = run(org, meta)
    rec = [s for s in result.steps if s.name == "Self-registration record"][0]
    assert rec.status == "skipped"
    assert not any(c[0] == "NetworkSelfRegistration" for c in org.created)


def test_validation_flags_a_guest_profile_that_reads_contacts():
    a = answers()
    a["FROM ObjectPermissions"] = [{"SobjectType": "Contact", "PermissionsRead": True}]
    result = run(FakeWriter(a), FakeMeta())
    bad = [c for c in result.validation if c.id == "guest_no_pii"][0]
    assert bad.status == "fail" and "Contact" in bad.message
    assert not result.ok


def test_validation_flags_register_page_404():
    result = run(FakeWriter(answers(), http={"https://x.my.site.com/portal/SelfRegister": 404}), FakeMeta())
    bad = [c for c in result.validation if c.id == "register_page"][0]
    assert bad.status == "fail"


def test_missing_guest_profile_is_a_failure_not_a_vacuous_pass():
    a = answers()
    a["FROM Profile WHERE Name"] = []
    a["FROM ObjectPermissions"] = []
    result = run(FakeWriter(a), FakeMeta())
    bad = [c for c in result.validation if c.id == "guest_no_pii"][0]
    assert bad.status == "fail" and "not found" in bad.message


# --- stage 3: pages + access ------------------------------------------------

def test_pages_step_deploys_everything_in_one_transaction():
    org, meta = FakeWriter(answers()), FakeMeta()
    result = run(org, meta, content=portal_content())
    assert result.ok, [(s.name, s.status, s.detail) for s in result.steps]
    zip_bytes, check_only = meta.deployed[1]
    assert check_only is False
    z = zipfile.ZipFile(io.BytesIO(zip_bytes))
    names = z.namelist()
    root = "digitalExperiences/site/Customer_Portal1"
    for expected in (
        f"{root}/Customer_Portal1.digitalExperience-meta.xml",
        f"{root}/sfdc_cms__route/My_Account__c/content.json",
        f"{root}/sfdc_cms__view/my_account/content.json",
        f"{root}/sfdc_cms__view/home/content.json",
        "lwc/portalMyDetails/portalMyDetails.js",
        "lwc/portalMyDetails/portalMyDetails.js-meta.xml",
        "sharingSets/Portal_Users.sharingSet",
        "profiles/Customer Community User.profile",
    ):
        assert expected in names, expected
    pkg = z.read("package.xml").decode()
    for t in ("DigitalExperienceBundle", "LightningComponentBundle", "SharingSet", "Profile"):
        assert f"<name>{t}</name>" in pkg


def test_site_is_made_public_and_geobots_dropped():
    meta = FakeMeta()
    run(FakeWriter(answers()), meta, content=portal_content())
    z = zipfile.ZipFile(io.BytesIO(meta.deployed[1][0]))
    site = json.loads(z.read("digitalExperiences/site/Customer_Portal1/sfdc_cms__site/Customer_Portal1/content.json"))
    assert site["contentBody"] == {"authenticationType": "AUTHENTICATED_WITH_PUBLIC_ACCESS_ENABLED"}


def test_login_and_register_views_redirect_to_the_secure_page():
    meta = FakeMeta()
    run(FakeWriter(answers()), meta, content=portal_content())
    z = zipfile.ZipFile(io.BytesIO(meta.deployed[1][0]))
    for view in ("login", "register"):
        data = json.loads(z.read(f"digitalExperiences/site/Customer_Portal1/sfdc_cms__view/{view}/content.json"))
        comp = data["contentBody"]["component"]["children"][0]
        assert comp["attributes"]["startUrl"] == "/my-account"


def test_no_content_skips_the_pages_step():
    meta = FakeMeta()
    result = run(FakeWriter(answers()), meta)
    assert [s.status for s in result.steps if s.name == "Pages + access"] == ["skipped"]
    assert len(meta.deployed) == 1


def test_landing_that_redirects_to_login_is_not_public():
    login = (302, "https://x.my.site.com/portal/login?startURL=%2Fportal%2F")
    result = run(FakeWriter(answers(), http={"https://x.my.site.com/portal": login}), FakeMeta(), content=portal_content())
    bad = [c for c in result.validation if c.id == "landing_public"][0]
    assert bad.status == "fail" and "login" in bad.message and not result.ok


def test_secure_page_that_serves_without_login_fails():
    open_page = {"https://x.my.site.com/portal/my-account": 200}
    result = run(FakeWriter(answers(), http=open_page), FakeMeta(), content=portal_content())
    assert [c for c in result.validation if c.id == "secure_requires_login"][0].status == "fail"


def test_missing_sharing_set_fails_validation():
    result = run(FakeWriter(answers()), FakeMeta(sharing_sets=()), content=portal_content())
    assert [c for c in result.validation if c.id == "sharing_set"][0].status == "fail"


def test_member_permissions_checked():
    a = answers()
    a["Parent.Profile.Name = 'Customer Community User'"] = [{"SobjectType": "Account", "PermissionsRead": True, "PermissionsEdit": False}]
    result = run(FakeWriter(a), FakeMeta(), content=portal_content())
    assert [c for c in result.validation if c.id == "member_permissions"][0].status == "fail"


def test_pages_deploy_failure_stops_before_publish():
    class Meta(FakeMeta):
        async def deploy_and_wait(self, zip_bytes, check_only=True):
            self.deployed.append((zip_bytes, check_only))
            ok = len(self.deployed) == 1  # network deploy passes, pages deploy fails
            return DeployResult(id="0Af", status="Succeeded" if ok else "Failed", success=ok)

    org = FakeWriter(answers())
    result = run(org, Meta(), content=portal_content())
    assert not result.ok
    assert not any(p[0].endswith("/publish") for p in org.posts)


def test_trailing_slash_redirect_on_the_landing_page_is_still_public():
    org = FakeWriter(answers(), http={"https://x.my.site.com/portal": (301, "https://x.my.site.com/portal/"),
                                      "https://x.my.site.com/portal/": 200})
    result = run(org, FakeMeta(), content=portal_content())
    assert [c for c in result.validation if c.id == "landing_public"][0].status == "pass"
