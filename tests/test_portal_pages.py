"""portal_pages: the generators that need no org."""

import json
import re
from itertools import count

import pytest

from flowtool.ir_lwc import heuristic_errors
from flowtool.portal_pages import (
    Cta, PortalContent, bundle_files, details_component, details_files, landing_html,
    landing_view, member_profile_xml, public_site_content, secure_route, secure_view,
    sharing_set_xml, view_name, with_start_url,
)


def ids():
    n = count(1)
    return lambda: f"id-{next(n):04d}"


def all_ids(node, found=None):
    found = [] if found is None else found
    if isinstance(node, dict):
        for k, v in node.items():
            if k == "id":
                found.append(v)
            all_ids(v, found)
    elif isinstance(node, list):
        for v in node:
            all_ids(v, found)
    return found


C = PortalContent(site_name="Customer Portal")


def test_every_component_id_in_a_view_is_unique():
    for view in (landing_view(C, ids()), secure_view(C, ids())):
        found = all_ids(view)
        assert len(found) == len(set(found)) > 3


def test_section_config_uuids_match_the_component_ids():
    view = landing_view(C, ids())
    section = view["contentBody"]["component"]["children"][0]["children"][0]
    config = json.loads(section["attributes"]["sectionConfig"])
    assert config["UUID"] == section["id"]
    assert config["columns"][0]["UUID"] == section["children"][0]["id"]


def test_landing_html_escapes_user_text_and_links_ctas():
    html = landing_html('<script>alert(1)</script>', 'a & b "q"', [Cta("Go", "/x?a=1&b=2")])
    assert "<script>" not in html and "&lt;script&gt;" in html
    assert "a &amp; b &quot;q&quot;" in html
    assert 'href="/x?a=1&amp;b=2"' in html


def test_landing_view_offers_register_and_login():
    text = json.dumps(landing_view(C, ids()))
    assert "/SelfRegister" in text and "/login" in text


def test_secure_route_requires_login_and_uses_custom_route_naming():
    route = secure_route(C)["contentBody"]
    assert route["pageAccess"] == "RequiresLogin"
    assert route["routeType"] == "custom-my-account"
    assert C.secure_route.endswith("__c")
    assert route["activeViewId"] == view_name(C.secure_route) == "my_account"


def test_secure_view_places_the_custom_component():
    view = secure_view(C, ids())
    assert '"definition": "c:portalMyDetails"' in json.dumps(view, indent=0).replace('" : "', '": "') \
        or "c:portalMyDetails" in json.dumps(view)


def test_bundle_files_include_meta_for_every_content_file():
    files = bundle_files(C, ids())
    for path in files:
        if path.endswith("/content.json"):
            assert path.replace("content.json", "_meta.json") in files, path
    assert "sfdc_cms__route/My_Account__c/content.json" in files


def test_with_start_url_only_touches_login_and_register_components():
    view = {"children": [
        {"definition": "community_login:loginForm", "attributes": {"a": 1}},
        {"definition": "community_login:selfRegister", "attributes": {}},
        {"definition": "community_builder:htmlEditor", "attributes": {}},
    ]}
    out = with_start_url(view, "/my-account")
    kids = out["children"]
    assert kids[0]["attributes"] == {"a": 1, "startUrl": "/my-account"}
    assert kids[1]["attributes"]["startUrl"] == "/my-account"
    assert "startUrl" not in kids[2]["attributes"]
    assert "startUrl" not in view["children"][0]["attributes"]  # original untouched


def test_public_site_content_enables_guests_and_drops_geobots():
    existing = json.dumps({"type": "sfdc_cms__site", "title": "T", "urlName": "t",
                           "contentBody": {"authenticationType": "AUTHENTICATED", "geoBotsAllowed": False}})
    body = json.loads(public_site_content(existing))["contentBody"]
    assert body == {"authenticationType": "AUTHENTICATED_WITH_PUBLIC_ACCESS_ENABLED"}


def test_details_component_passes_the_repo_lwc_heuristics():
    comp = details_component(C)
    assert heuristic_errors(comp.api_name, comp.js, comp.html) == []
    assert "lightningCommunity__Page" in comp.targets and comp.is_exposed


def test_details_component_renders_requested_fields():
    comp = details_component(PortalContent(site_name="S", contact_fields=["Phone", "Loyalty__c"], account_fields=["Name", "Industry"]))
    assert 'field-name="Loyalty__c"' in comp.html
    assert re.search(r'lightning-output-field field-name="Industry"', comp.html)


def test_field_names_cannot_inject_markup():
    with pytest.raises(ValueError):
        details_component(PortalContent(site_name="S", contact_fields=['Phone"><script>']))


def test_details_files_layout():
    files = details_files(C)
    assert set(files) == {
        "lwc/portalMyDetails/portalMyDetails.js",
        "lwc/portalMyDetails/portalMyDetails.html",
        "lwc/portalMyDetails/portalMyDetails.js-meta.xml",
    }


def test_sharing_set_maps_account_and_cases_but_never_contact_as_object():
    xml = sharing_set_xml("Portal_Users", "Customer Community User")
    assert "<object>Account</object>" in xml and "<object>Case</object>" in xml
    assert "<object>Contact</object>" not in xml  # the org rejects Contact as a sharing set object
    assert "<profiles>Customer Community User</profiles>" in xml
    assert "<object>Case</object>" not in sharing_set_xml("P", "X", include_cases=False)


def test_member_profile_grants_contact_edit_and_account_read_only():
    xml = member_profile_xml()
    contact = xml.split("<object>Contact</object>")[0].rsplit("<objectPermissions>", 1)[1]
    account = xml.split("<object>Account</object>")[0].rsplit("<objectPermissions>", 1)[1]
    assert "<allowEdit>true</allowEdit>" in contact
    assert "<allowEdit>false</allowEdit>" in account
    assert "<allowDelete>true" not in xml
