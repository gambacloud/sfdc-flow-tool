"""
Opening an existing Custom Object or Custom Field from the org.

The reverse of `xmlgen_object`: the retrieved `.object` file becomes the same IR
the tool generates (CustomObject, CustomField), so it can be reviewed, revised
and deployed again. Only what the IR can say is read. A field of a type the IR
does not know, a picklist fed by a global value set or a controlling field, a
lookup filter, a field that tracks history, and so on, is refused with the
reason rather than imported as something different from what is in the org -
the same stance as opening a flow.

Opening an object brings its custom fields along as separate field steps (the IR
keeps them apart). A field that cannot be represented is left out of that plan
and named in `OpenedObject.skipped`: deploying the plan does not touch it, since
a deploy never removes a field that is merely absent. The object's own shape
(name field, sharing model, deployment status) is refused when it does not fit.
Object-level switches the IR has no place for (history, reports, search, ...) are
not read; a redeploy leaves them as the org has them, as it does for objects the
tool itself created.
"""

from __future__ import annotations

import io
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from pydantic import ValidationError

from .ir_object import CustomField, CustomObject
from .sfdc import MetadataClient, RetrieveError

_NS = {"m": "http://soap.sforce.com/2006/04/metadata"}


class UnsupportedObject(ValueError):
    """The object or field uses something the IR cannot represent."""


@dataclass
class OpenedObject:
    object: CustomObject
    fields: List[CustomField] = field(default_factory=list)
    # "Field__c: reason" for each custom field left out of the plan.
    skipped: List[str] = field(default_factory=list)
    # Things the object has that the plan does not manage (record types), for the reviewer.
    notes: List[str] = field(default_factory=list)


def _text(node, tag: str) -> Optional[str]:
    found = node.find(f"m:{tag}", _NS)
    return found.text if found is not None and found.text is not None else None


def _flag(node, tag: str) -> bool:
    return (_text(node, tag) or "false").strip().lower() == "true"


# Metadata a field can carry that the IR has nowhere to put. Refused when set
# (a `false` flag is the default and says nothing).
_FIELD_FLAGS = {
    "externalId": "is an external id",
    "trackHistory": "tracks field history",
    "trackTrending": "is trended",
    "trackFeedHistory": "tracks feed history",
    "reparentableMasterDetail": "allows reparenting",
    "writeRequiresMasterRead": "has the 'read on the parent' sharing option",
    "displayLocationInDecimal": "displays its location in decimal",
}
_FIELD_VALUES = {
    "inlineHelpText": "has help text",
    "lookupFilter": "has a lookup filter",
    "encryptionScheme": "is encrypted",
    "businessOwnerGroup": "has a business owner",
    "businessOwnerUser": "has a business owner",
    "businessStatus": "has a business status",
    "securityClassification": "has a security classification",
    "complianceGroup": "has a compliance group",
    "relationshipOrder": "has a roll-up relationship order",
    "summaryOperation": "is a roll-up summary",
    "controllingField": "is a dependent picklist",
    "valueSettings": "is a dependent picklist",
    "fieldManageability": "has a manageability setting",
    "mask": "is masked",
}
_SUPPORTED_TYPES = {
    "Text", "TextArea", "LongTextArea", "Number", "Checkbox", "Picklist", "Lookup", "MasterDetail",
}
_FORMULA_RETURNS = {"Text", "Number", "Checkbox"}


def _refuse_unrepresentable(node, name: str) -> None:
    for tag, why in _FIELD_FLAGS.items():
        if _flag(node, tag):
            raise UnsupportedObject(f"{name} {why}, which the tool cannot represent.")
    for tag, why in _FIELD_VALUES.items():
        if node.find(f"m:{tag}", _NS) is not None:
            raise UnsupportedObject(f"{name} {why}, which the tool cannot represent.")


def _picklist_values(node, name: str) -> Tuple[List[str], Optional[str], bool]:
    value_set = node.find("m:valueSet", _NS)
    if value_set is None:
        raise UnsupportedObject(f"{name} has no picklist values.")
    if value_set.find("m:valueSetName", _NS) is not None:
        raise UnsupportedObject(
            f"{name} takes its values from a global value set ({_text(value_set, 'valueSetName')}), "
            "which the tool cannot represent."
        )
    definition = value_set.find("m:valueSetDefinition", _NS)
    if definition is None:
        raise UnsupportedObject(f"{name} has no picklist values.")
    if _flag(definition, "sorted"):
        raise UnsupportedObject(f"{name} sorts its values alphabetically, which the tool cannot represent.")
    values: List[str] = []
    default: Optional[str] = None
    for entry in definition.findall("m:value", _NS):
        value = _text(entry, "fullName") or ""
        label = _text(entry, "label")
        if label is not None and label != value:
            raise UnsupportedObject(f"{name}: picklist value {value!r} has a different label ({label!r}).")
        if entry.find("m:isActive", _NS) is not None and not _flag(entry, "isActive"):
            raise UnsupportedObject(f"{name}: picklist value {value!r} is inactive.")
        for extra in ("color", "description"):
            if entry.find(f"m:{extra}", _NS) is not None:
                raise UnsupportedObject(f"{name}: picklist value {value!r} has a {extra}, which the tool cannot represent.")
        values.append(value)
        if _flag(entry, "default"):
            if default is not None:
                raise UnsupportedObject(f"{name} has more than one default picklist value.")
            default = value
    return values, default, _flag(value_set, "restricted")


def parse_field(node, object_api_name: str) -> CustomField:
    api_name = _text(node, "fullName") or ""
    name = f"{object_api_name}.{api_name}"
    kind = _text(node, "type") or ""
    formula = _text(node, "formula")

    if formula is not None:
        if kind not in _FORMULA_RETURNS:
            raise UnsupportedObject(f"{name} is a {kind} formula; only Text, Number and Checkbox formulas can be opened.")
        if kind == "Number" and (_text(node, "formulaTreatBlanksAs") or "BlankAsZero") != "BlankAsZero":
            raise UnsupportedObject(f"{name} treats blanks as blank, which the tool cannot represent.")
        _refuse_unrepresentable(node, name)
        kwargs = dict(type="Formula", formula=formula, formula_returns=kind)
    elif kind in _SUPPORTED_TYPES:
        _refuse_unrepresentable(node, name)
        kwargs = dict(type=kind)
    else:
        raise UnsupportedObject(f"{name} is a {kind or 'unknown'} field, which the tool cannot represent.")

    def number(tag: str) -> Optional[int]:
        raw = _text(node, tag)
        return int(raw) if raw not in (None, "") else None

    try:
        if kind == "Picklist" and formula is None:
            values, default, restricted = _picklist_values(node, name)
            kwargs.update(picklist_values=values, restricted=restricted)
            if default is not None:
                kwargs["default_value"] = default
        else:
            default = _text(node, "defaultValue")
            if default is not None and formula is None:
                kwargs["default_value"] = default
        if formula is None:
            if kind in ("Text", "LongTextArea"):
                kwargs["length"] = number("length")
            if kind == "LongTextArea":
                kwargs["visible_lines"] = number("visibleLines")
            if kind == "Number":
                kwargs["precision"] = number("precision")
                kwargs["scale"] = number("scale")
            if kind in ("Text", "Number") and _flag(node, "unique"):
                if _flag(node, "caseSensitive"):
                    raise UnsupportedObject(f"{name} is unique and case sensitive, which the tool cannot represent.")
                kwargs["unique"] = True
            if kind in ("Lookup", "MasterDetail"):
                kwargs.update(
                    reference_to=_text(node, "referenceTo"),
                    relationship_label=_text(node, "relationshipLabel"),
                    relationship_name=_text(node, "relationshipName"),
                )
            if kind == "Lookup":
                required = _flag(node, "required")
                # The generator derives this from `required`; anything else would change on redeploy.
                expected = "Restrict" if required else "SetNull"
                actual = _text(node, "deleteConstraint") or "SetNull"
                if actual != expected:
                    raise UnsupportedObject(
                        f"{name} uses delete constraint {actual}, which the tool cannot represent "
                        f"(it writes {expected} for this field)."
                    )
            if kind not in ("MasterDetail", "Checkbox"):
                kwargs["required"] = _flag(node, "required")
        else:
            if kind == "Number":
                kwargs["precision"] = number("precision")
                kwargs["scale"] = number("scale")
        return CustomField(
            api_name=api_name,
            label=_text(node, "label") or api_name,
            object_api_name=object_api_name,
            description=_text(node, "description"),
            **kwargs,
        )
    except ValidationError as exc:
        reasons = "; ".join(e["msg"] for e in exc.errors())
        raise UnsupportedObject(f"{name} cannot be opened as it is: {reasons}") from exc
    except ValueError as exc:
        if isinstance(exc, UnsupportedObject):
            raise
        raise UnsupportedObject(f"{name} cannot be opened as it is: {exc}") from exc


def parse_custom_field(xml: str, object_api_name: str, field_api_name: str) -> CustomField:
    root = ET.fromstring(xml.lstrip("﻿"))
    for node in root.findall("m:fields", _NS):
        if _text(node, "fullName") == field_api_name:
            return parse_field(node, object_api_name)
    raise UnsupportedObject(f"{object_api_name}.{field_api_name} was not found in the retrieved metadata.")


def parse_custom_object(xml: str, api_name: str) -> OpenedObject:
    root = ET.fromstring(xml.lstrip("﻿"))
    if not api_name.endswith("__c"):
        raise UnsupportedObject(
            f"{api_name} is not a custom object (__c); open a custom field of it instead."
        )
    name_field = root.find("m:nameField", _NS)
    if name_field is None:
        raise UnsupportedObject(f"{api_name} has no name field to open.")
    record_name_type = _text(name_field, "type") or "Text"
    if record_name_type not in ("Text", "AutoNumber"):
        raise UnsupportedObject(f"{api_name}: its name field is {record_name_type!r}; only Text and AutoNumber can be opened.")
    status = _text(root, "deploymentStatus") or "Deployed"
    if status not in ("Deployed", "InDevelopment"):
        raise UnsupportedObject(f"{api_name}: deployment status {status!r} is not supported.")
    sharing = _text(root, "sharingModel") or "ReadWrite"
    try:
        obj = CustomObject(
            api_name=api_name,
            label=_text(root, "label") or api_name,
            plural_label=_text(root, "pluralLabel") or _text(root, "label") or api_name,
            description=_text(root, "description"),
            record_name=_text(name_field, "label") or "Name",
            record_name_type=record_name_type,
            record_name_display_format=_text(name_field, "displayFormat") if record_name_type == "AutoNumber" else None,
            deployment_status=status,
            sharing_model=sharing,
        )
    except ValidationError as exc:
        reasons = "; ".join(e["msg"] for e in exc.errors())
        raise UnsupportedObject(f"{api_name} cannot be opened as it is: {reasons}") from exc

    opened = OpenedObject(object=obj)
    for node in sorted(root.findall("m:fields", _NS), key=lambda n: (_text(n, "fullName") or "")):
        full = _text(node, "fullName") or ""
        if not full.endswith("__c"):
            continue  # a standard field the org customised; the plan does not touch it
        try:
            opened.fields.append(parse_field(node, api_name))
        except UnsupportedObject as exc:
            opened.skipped.append(str(exc))
    opened.notes = _record_type_notes(root, opened.fields)
    return opened


def _record_type_notes(root, fields: List[CustomField]) -> List[str]:
    """
    Record types are not in the IR, and a redeploy leaves them alone. What matters to a
    reviewer: picklist values are enabled per record type, so a value added to a picklist
    here is not offered on a record type until it is added there too.
    """
    types = root.findall("m:recordTypes", _NS)
    if not types:
        return []
    names = [_text(t, "fullName") or "" for t in types]
    notes = [f"The object has {len(types)} record type(s) ({', '.join(names)}) that this plan does not manage."]
    in_record_types = {
        _text(pv, "picklist") for t in types for pv in t.findall("m:picklistValues", _NS)
    }
    picklists = [f.api_name for f in fields if f.type == "Picklist" and f.api_name in in_record_types]
    if picklists:
        notes.append(
            f"Picklist values are enabled per record type for {', '.join(picklists)}: a value added here "
            "will not be offered on a record type until it is added there."
        )
    return notes


def _objects_from_zip(zip_bytes: bytes) -> Dict[str, str]:
    out: Dict[str, str] = {}
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as archive:
        for name in archive.namelist():
            if name.startswith("objects/") and name.endswith(".object"):
                out[name[len("objects/"):-len(".object")]] = archive.read(name).decode("utf-8")
    return out


async def list_custom_objects(client: MetadataClient) -> List[Dict[str, str]]:
    """The org's own custom objects (standard and managed ones are left out)."""
    found = await client.list_metadata("CustomObject")
    own = [
        f for f in found
        if f["full_name"] and f["full_name"].endswith("__c") and not f.get("namespace_prefix")
    ]
    return sorted(({"api_name": f["full_name"]} for f in own), key=lambda f: f["api_name"].lower())


async def list_custom_fields(client: MetadataClient) -> List[Dict[str, str]]:
    """The org's own custom fields as Object.Field__c (managed ones are left out)."""
    found = await client.list_metadata("CustomField")
    own = [
        f for f in found
        if f["full_name"] and "." in f["full_name"] and f["full_name"].endswith("__c") and not f.get("namespace_prefix")
    ]
    return sorted(({"api_name": f["full_name"]} for f in own), key=lambda f: f["api_name"].lower())


async def retrieve_custom_object(client: MetadataClient, api_name: str) -> OpenedObject:
    zip_bytes = await client.wait_for_retrieve(await client.start_retrieve_types({"CustomObject": [api_name]}))
    files = _objects_from_zip(zip_bytes)
    if api_name not in files:
        raise RetrieveError(f"{api_name} was not found in the org.")
    try:
        return parse_custom_object(files[api_name], api_name)
    except UnsupportedObject as exc:
        raise RetrieveError(str(exc)) from exc


async def retrieve_custom_field(client: MetadataClient, full_name: str) -> CustomField:
    """`full_name` is Object.Field__c."""
    object_api_name, _, field_api_name = full_name.partition(".")
    if not object_api_name or not field_api_name:
        raise RetrieveError(f"{full_name!r} is not Object.Field__c.")
    zip_bytes = await client.wait_for_retrieve(await client.start_retrieve_types({"CustomField": [full_name]}))
    files = _objects_from_zip(zip_bytes)
    if object_api_name not in files:
        raise RetrieveError(f"{full_name} was not found in the org.")
    try:
        return parse_custom_field(files[object_api_name], object_api_name, field_api_name)
    except UnsupportedObject as exc:
        raise RetrieveError(str(exc)) from exc
