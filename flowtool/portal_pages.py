"""
Portal builder, stage 3: the pages and the access model.

Pure generators (no network): a public landing page, a secure "My account"
page, the LWC that shows and edits the signed-in user's Contact, the Sharing
Set, and the member profile's object permissions. portal_site.py deploys them.

Shapes here were established against a real LWR scratch org:
- a view needs a unique id on every component, so ids come from a generator;
- a custom route's api name must end in `__c`, its routeType is `custom-<x>`;
- a route is "RequiresLogin" or "UseParent" (public follows the site);
- `Contact` is not a valid Sharing Set object (the user already owns their
  Contact), `Account` is, and Case maps ContactId to the user's Contact.
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional

from flowtool.ir_lwc import LightningComponent
from flowtool.xmlgen_lwc import generate_lwc

METADATA_NS = "http://soap.sforce.com/2006/04/metadata"
IdFactory = Callable[[], str]


def _uuid() -> str:
    return str(uuid.uuid4())


@dataclass
class Cta:
    label: str
    href: str
    primary: bool = True


@dataclass
class PortalContent:
    """What the user asked the portal to say. Everything is plain text/HTML-escaped
    by the generators - nothing here is trusted as markup."""
    site_name: str
    headline: str = "Welcome"
    intro: str = "Create an account to manage your details."
    contact_fields: List[str] = field(default_factory=lambda: ["FirstName", "LastName", "Email", "Phone"])
    account_fields: List[str] = field(default_factory=lambda: ["Name"])
    secure_route: str = "My_Account__c"   # api name (must end __c)
    secure_url: str = "my-account"
    secure_title: str = "My Account"
    secure_component: str = "portalMyDetails"


# --- helpers ---------------------------------------------------------------

def _esc(text: str) -> str:
    return (text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;"))


def view_name(route_api_name: str) -> str:
    """View api name that pairs with a custom route: lowercase, no __c."""
    return re.sub(r"__c$", "", route_api_name).lower()


def _fresh_ids(node: Any, new_id: IdFactory, mapping: Optional[Dict[str, str]] = None) -> Any:
    """Replace every component id (and the UUIDs inside sectionConfig strings)
    so a view copied from another one does not collide with it."""
    mapping = {} if mapping is None else mapping

    def swap(old: str) -> str:
        return mapping.setdefault(old, new_id())

    if isinstance(node, dict):
        out = {}
        for k, v in node.items():
            if k == "id" and isinstance(v, str):
                out[k] = swap(v)
            elif k == "sectionConfig" and isinstance(v, str):
                out[k] = re.sub(r'"UUID":"([0-9a-f-]{36})"', lambda m: f'"UUID":"{swap(m.group(1))}"', v)
            else:
                out[k] = _fresh_ids(v, new_id, mapping)
        return out
    if isinstance(node, list):
        return [_fresh_ids(v, new_id, mapping) for v in node]
    return node


def _section(children: List[Dict[str, Any]], new_id: IdFactory) -> Dict[str, Any]:
    col, sec = new_id(), new_id()
    return {
        "attributes": {
            "backgroundImageConfig": "", "backgroundImageOverlay": "rgba(0,0,0,0)",
            "componentSpacerSize": "", "layoutDirectionDesktop": "row",
            "layoutDirectionMobile": "column", "layoutDirectionTablet": "column",
            "maxContentWidth": "", "sectionColumnGutterWidth": "",
            "sectionConfig": json.dumps({"UUID": sec, "columns": [
                {"UUID": col, "columnName": "Column 1", "columnKey": "col1",
                 "columnWidth": "12", "seedComponents": None}]}, separators=(",", ":")),
            "sectionMinHeight": "", "sectionVerticalAlign": "flex-start",
        },
        "children": [{"children": children, "id": col, "name": "col1", "title": "Column 1", "type": "region"}],
        "definition": "community_layout:section", "id": sec, "type": "component",
    }


def _view(title: str, url_name: str, view_type: str, content: List[Dict[str, Any]], new_id: IdFactory) -> Dict[str, Any]:
    seo = {
        "attributes": {"customHeadTags": "", "description": "", "pageTitle": title, "recordId": "{!recordId}"},
        "definition": "community_builder:seoAssistant", "id": new_id(), "type": "component",
    }
    return {
        "type": "sfdc_cms__view",
        "title": title,
        "contentBody": {
            "component": {
                "children": [
                    {"children": [_section(content, new_id)], "id": new_id(), "name": "content",
                     "title": "Content", "type": "region"},
                    {"children": [seo], "id": new_id(), "name": "sfdcHiddenRegion",
                     "title": "sfdcHiddenRegion", "type": "region"},
                ],
                "definition": "community_layout:sldsFlexibleLayout", "id": new_id(), "type": "component",
            },
            "dataProviders": [],
            "themeLayoutType": "Inner",
            "viewType": view_type,
        },
        "urlName": url_name,
    }


def _html_block(html: str, new_id: IdFactory) -> Dict[str, Any]:
    return {"attributes": {"imageInfos": "", "richTextValue": html},
            "definition": "community_builder:htmlEditor", "id": new_id(), "type": "component"}


# --- pages -----------------------------------------------------------------

def landing_html(headline: str, intro: str, ctas: Iterable[Cta]) -> str:
    buttons = "".join(
        f'<a href="{_esc(c.href)}" class="slds-button {"slds-button_brand" if c.primary else "slds-button_neutral"}" '
        f'style="margin:0 8px;">{_esc(c.label)}</a>'
        for c in ctas
    )
    return (
        '<div style="display:flex;flex-direction:column;align-items:center;text-align:center;margin:60px 16px;">'
        f'<h1 class="slds-text-heading_large slds-p-bottom_small">{_esc(headline)}</h1>'
        f'<p class="slds-p-bottom_large" style="max-width:560px;">{_esc(intro)}</p>'
        f'<div>{buttons}</div></div>'
    )


def landing_view(content: PortalContent, new_id: IdFactory = _uuid) -> Dict[str, Any]:
    """The public home page: a message and Register / Log in buttons."""
    html = landing_html(content.headline, content.intro, [Cta("Register", "/SelfRegister"), Cta("Log in", "/login", False)])
    return _view("Home", "home", "home", [_html_block(html, new_id)], new_id)


def secure_view(content: PortalContent, new_id: IdFactory = _uuid) -> Dict[str, Any]:
    component = {"attributes": {}, "definition": f"c:{content.secure_component}", "id": new_id(), "type": "component"}
    return _view(content.secure_title, content.secure_url, f"custom-{content.secure_url}", [component], new_id)


def secure_route(content: PortalContent) -> Dict[str, Any]:
    return {
        "type": "sfdc_cms__route",
        "title": content.secure_title,
        "contentBody": {
            "activeViewId": view_name(content.secure_route),
            "configurationTags": [],
            "pageAccess": "RequiresLogin",
            "routeType": f"custom-{content.secure_url}",
            "urlPrefix": content.secure_url,
        },
        "urlName": content.secure_url,
    }


def with_start_url(view: Dict[str, Any], start_url: str) -> Dict[str, Any]:
    """Copy of a retrieved login/register view whose login and self-register
    components send the user to start_url afterwards."""
    out = json.loads(json.dumps(view))

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            if node.get("definition") in ("community_login:loginForm", "community_login:selfRegister"):
                node.setdefault("attributes", {})["startUrl"] = start_url
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(out)
    return out


def bundle_meta_xml(label: str) -> str:
    """The bundle's own .digitalExperience file; a partial deploy still needs it
    (the org rejects the bundle with "Required field is missing: label")."""
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<DigitalExperienceBundle xmlns="{METADATA_NS}">\n'
        f"    <label>{_esc(label)}</label>\n"
        "</DigitalExperienceBundle>\n"
    )


def public_site_content(existing: str) -> str:
    """The retrieved sfdc_cms__site content with guest access switched on. A site is
    AUTHENTICATED by default, which sends every visitor (even to the home page) to
    login. `geoBotsAllowed` is dropped: the org writes it on retrieve but rejects it
    on deploy ("additionalProperties is false")."""
    data = json.loads(existing)
    body = data.setdefault("contentBody", {})
    body.pop("geoBotsAllowed", None)
    body["authenticationType"] = "AUTHENTICATED_WITH_PUBLIC_ACCESS_ENABLED"
    return json.dumps(data, indent=2) + "\n"


def bundle_files(content: PortalContent, new_id: IdFactory = _uuid) -> Dict[str, str]:
    """Bundle-relative path -> file text for the pages this stage adds or replaces."""
    route_dir = f"sfdc_cms__route/{content.secure_route}"
    view_dir = f"sfdc_cms__view/{view_name(content.secure_route)}"
    dump = lambda o: json.dumps(o, indent=2) + "\n"  # noqa: E731
    return {
        f"{route_dir}/_meta.json": dump({"apiName": content.secure_route, "type": "sfdc_cms__route", "path": "routes"}),
        f"{route_dir}/content.json": dump(secure_route(content)),
        f"{view_dir}/_meta.json": dump({"apiName": view_name(content.secure_route), "type": "sfdc_cms__view", "path": "views"}),
        f"{view_dir}/content.json": dump(secure_view(content, new_id)),
        "sfdc_cms__view/home/_meta.json": dump({"apiName": "home", "type": "sfdc_cms__view", "path": "views"}),
        "sfdc_cms__view/home/content.json": dump(landing_view(content, new_id)),
    }


# --- the LWC that shows/edits the user's own Contact -------------------------

_FIELD_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")


def _check_fields(fields: Iterable[str]) -> List[str]:
    out = list(fields)
    bad = [f for f in out if not _FIELD_RE.match(f)]
    if bad:
        raise ValueError(f"invalid field api name(s): {', '.join(bad)}")
    return out


def details_component(content: PortalContent, api_version: str = "64.0") -> LightningComponent:
    contact = _check_fields(content.contact_fields)
    account = _check_fields(content.account_fields)
    name = content.secure_component
    cls = name[:1].upper() + name[1:]
    contact_inputs = "\n".join(f'                <lightning-input-field field-name="{f}"></lightning-input-field>' for f in contact)
    account_outputs = "\n".join(f'                <lightning-output-field field-name="{f}"></lightning-output-field>' for f in account)
    html = f"""<template>
    <template lwc:if={{contactId}}>
        <lightning-card title="My details" icon-name="standard:contact">
            <lightning-record-edit-form object-api-name="Contact" record-id={{contactId}} onsuccess={{handleSuccess}}>
                <lightning-messages></lightning-messages>
                <div class="slds-p-horizontal_medium">
{contact_inputs}
                </div>
                <div class="slds-p-around_medium">
                    <lightning-button type="submit" variant="brand" label="Save"></lightning-button>
                </div>
            </lightning-record-edit-form>
        </lightning-card>
    </template>
    <template lwc:if={{accountId}}>
        <lightning-card title="My account" icon-name="standard:account">
            <lightning-record-view-form object-api-name="Account" record-id={{accountId}}>
                <div class="slds-p-horizontal_medium">
{account_outputs}
                </div>
            </lightning-record-view-form>
        </lightning-card>
    </template>
    <template lwc:if={{error}}>
        <p class="slds-text-color_error slds-p-around_medium">{{error}}</p>
    </template>
</template>
"""
    js = f"""import {{ LightningElement, wire }} from 'lwc';
import {{ getRecord }} from 'lightning/uiRecordApi';
import {{ ShowToastEvent }} from 'lightning/platformShowToastEvent';
import USER_ID from '@salesforce/user/Id';
import CONTACT_ID from '@salesforce/schema/User.ContactId';
import ACCOUNT_ID from '@salesforce/schema/User.AccountId';

export default class {cls} extends LightningElement {{
    contactId;
    accountId;
    error;

    @wire(getRecord, {{ recordId: USER_ID, fields: [CONTACT_ID, ACCOUNT_ID] }})
    wiredUser({{ data, error }}) {{
        if (data) {{
            this.contactId = data.fields.ContactId.value;
            this.accountId = data.fields.AccountId.value;
            this.error = undefined;
        }} else if (error) {{
            this.error = 'Your details could not be loaded.';
        }}
    }}

    handleSuccess() {{
        this.dispatchEvent(new ShowToastEvent({{ title: 'Saved', variant: 'success' }}));
    }}
}}
"""
    return LightningComponent(
        api_name=name, js=js, html=html, is_exposed=True,
        targets=["lightningCommunity__Page", "lightningCommunity__Default"],
        api_version=api_version,
    )


def details_files(content: PortalContent, api_version: str = "64.0") -> Dict[str, str]:
    return generate_lwc(details_component(content, api_version))


# --- access model ----------------------------------------------------------

def sharing_set_xml(name: str, profile: str, include_cases: bool = True) -> str:
    cases = """    <accessMappings>
        <accessLevel>Edit</accessLevel>
        <object>Case</object>
        <objectField>ContactId</objectField>
        <userField>Contact</userField>
    </accessMappings>
""" if include_cases else ""
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<SharingSet xmlns="{METADATA_NS}">
    <accessMappings>
        <accessLevel>Read</accessLevel>
        <object>Account</object>
        <objectField>Id</objectField>
        <userField>Account</userField>
    </accessMappings>
{cases}    <description>Portal users see their own Account{" and Cases" if include_cases else ""}</description>
    <name>{_esc(name)}</name>
    <profiles>{_esc(profile)}</profiles>
</SharingSet>
"""


def member_profile_xml() -> str:
    """Contact editable (own record), Account read-only. Merged into the existing profile."""
    def perm(obj: str, edit: bool) -> str:
        return (
            "    <objectPermissions>\n"
            "        <allowCreate>false</allowCreate>\n"
            "        <allowDelete>false</allowDelete>\n"
            f"        <allowEdit>{str(edit).lower()}</allowEdit>\n"
            "        <allowRead>true</allowRead>\n"
            "        <modifyAllRecords>false</modifyAllRecords>\n"
            f"        <object>{obj}</object>\n"
            "        <viewAllRecords>false</viewAllRecords>\n"
            "    </objectPermissions>\n"
        )
    return f'<?xml version="1.0" encoding="UTF-8"?>\n<Profile xmlns="{METADATA_NS}">\n{perm("Contact", True)}{perm("Account", False)}</Profile>\n'
