"""
PermissionSetGrant IR -> .permissionset XML.

Deterministic, no LLM involved - same rule as xmlgen.py/xmlgen_object.py,
whose `_sub`/`_bool` helpers and namespace this reuses.

Two entry points: generate_permission_set (a brand-new document) and
merge_permission_set_xml (folding new grants into an existing document
retrieved from the org, without touching anything already in it that this
grant doesn't concern). Field member names use the same dotted
`Object.Field` form CustomField's own package.xml member already uses
(build_deploy_package's docstring) - `Account.MyField__c` for a standard
object, `MyObject__c.MyField__c` for a custom one.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET

from .ir_permset import FieldGrant, ObjectGrant, PermissionSetGrant
from .xmlgen import METADATA_NS, _bool, _sub


def _field_member(grant: FieldGrant) -> str:
    return f"{grant.object_api_name}.{grant.field_api_name}"


def _add_field_permission(root: ET.Element, grant: FieldGrant) -> None:
    el = _sub(root, "fieldPermissions")
    _sub(el, "editable", _bool(grant.editable))
    _sub(el, "field", _field_member(grant))
    _sub(el, "readable", _bool(grant.readable))


def _add_object_permission(root: ET.Element, grant: ObjectGrant) -> None:
    el = _sub(root, "objectPermissions")
    _sub(el, "allowCreate", _bool(grant.allow_create))
    _sub(el, "allowDelete", _bool(grant.allow_delete))
    _sub(el, "allowEdit", _bool(grant.allow_edit))
    _sub(el, "allowRead", _bool(grant.allow_read))
    _sub(el, "modifyAllRecords", _bool(False))
    _sub(el, "object", grant.object_api_name)
    _sub(el, "viewAllRecords", _bool(False))


def generate_permission_set(grant: PermissionSetGrant, api_name: str) -> str:
    """Render a brand-new .permissionset document."""
    ET.register_namespace("", METADATA_NS)
    root = ET.Element(f"{{{METADATA_NS}}}PermissionSet")
    _sub(root, "label", grant.label)
    if grant.description:
        _sub(root, "description", grant.description)
    _sub(root, "hasActivationRequired", _bool(False))
    for field_grant in grant.field_grants:
        _add_field_permission(root, field_grant)
    for object_grant in grant.object_grants:
        _add_object_permission(root, object_grant)

    ET.indent(root, space="    ")
    body = ET.tostring(root, encoding="unicode")
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + body + "\n"


def merge_permission_set_xml(existing_xml: str, grant: PermissionSetGrant) -> str:
    """
    Fold `grant`'s field/object grants into an existing .permissionset
    document retrieved from the org - every block the grant doesn't concern
    is left exactly as retrieved. A field/object this grant also names but
    the existing document already has gets its access upgraded (readable/
    editable/allow* flipped true where the grant wants true) rather than
    duplicated - never downgraded, since this only ever adds access.
    """
    ns = {"m": METADATA_NS}
    ET.register_namespace("", METADATA_NS)
    root = ET.fromstring(existing_xml)

    existing_field_els = {
        el.findtext("m:field", "", ns): el for el in root.findall("m:fieldPermissions", ns)
    }
    for field_grant in grant.field_grants:
        member = _field_member(field_grant)
        el = existing_field_els.get(member)
        if el is not None:
            if field_grant.editable:
                _set_bool_child(el, "editable", ns, True)
            if field_grant.readable:
                _set_bool_child(el, "readable", ns, True)
        else:
            _add_field_permission(root, field_grant)

    existing_object_els = {
        el.findtext("m:object", "", ns): el for el in root.findall("m:objectPermissions", ns)
    }
    for object_grant in grant.object_grants:
        el = existing_object_els.get(object_grant.object_api_name)
        if el is not None:
            for tag, wanted in (
                ("allowRead", object_grant.allow_read),
                ("allowCreate", object_grant.allow_create),
                ("allowEdit", object_grant.allow_edit),
            ):
                if wanted:
                    _set_bool_child(el, tag, ns, True)
        else:
            _add_object_permission(root, object_grant)

    ET.indent(root, space="    ")
    body = ET.tostring(root, encoding="unicode")
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + body + "\n"


def _set_bool_child(parent: ET.Element, tag: str, ns: dict, value: bool) -> None:
    child = parent.find(f"m:{tag}", ns)
    if child is None:
        _sub(parent, tag, _bool(value))
    else:
        child.text = _bool(value)
