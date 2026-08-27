"""
MetadataType / CustomMetadataRecord IR -> metadata XML.

Deterministic, no LLM involved - same rule as xmlgen.py/xmlgen_object.py,
whose `_sub`/`_bool` helpers and namespace this reuses.

A __mdt type deploys under the same Metadata API type name as a regular
Custom Object (`CustomObject`), just __mdt-suffixed with a <visibility>
element and no <sharingModel>/<nameField> - confirmed against Salesforce's
own Metadata API reference, not assumed. A record is a genuinely different
metadata type (`CustomMetadata`), one file per record, and - the one real
gotcha found researching this - a raw Metadata API deploy zip names that
file with a plain `.md` extension, not the `-meta.xml` suffix every other
member this tool emits uses.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from typing import Optional

from .ir_mdt import CustomMetadataRecord, MetadataField, MetadataType
from .xmlgen import METADATA_NS, _bool, _sub

XSI_NS = "http://www.w3.org/2001/XMLSchema-instance"


def _fill_mdt_field(el: ET.Element, field: MetadataField) -> None:
    """
    Populate `el` with one MetadataField's children, alphabetically by tag -
    same convention xmlgen_object.py's _fill_field follows and for the same
    reason (the Metadata API is picky about child ordering on retrieve-diff).
    """
    has_required = field.type not in ("MetadataRelationship",)

    if field.description:
        _sub(el, "description", field.description)

    _sub(el, "fullName", field.api_name)
    _sub(el, "label", field.label)

    if field.type == "Text":
        _sub(el, "length", str(field.length))

    if field.type in ("Number", "Percent"):
        _sub(el, "precision", str(field.precision))

    if field.type == "MetadataRelationship":
        _sub(el, "referenceTo", field.reference_to)
        if field.relationship_label:
            _sub(el, "relationshipLabel", field.relationship_label)
        if field.relationship_name:
            _sub(el, "relationshipName", field.relationship_name)

    if has_required:
        _sub(el, "required", _bool(field.required))

    if field.type in ("Number", "Percent"):
        _sub(el, "scale", str(field.scale or 0))

    _sub(el, "type", field.type)

    if field.type == "Picklist":
        value_set = _sub(el, "valueSet")
        definition = _sub(value_set, "valueSetDefinition")
        _sub(definition, "sorted", "false")
        for value in field.picklist_values:
            entry = _sub(definition, "value")
            _sub(entry, "fullName", value)
            _sub(entry, "default", "false")


def generate_mdt_type(mdt: MetadataType) -> str:
    """
    Render a validated MetadataType IR as deployable metadata XML - the
    `.object` content for a __mdt, the same file shape a regular
    CustomObject uses (xmlgen_object.generate_object) minus <sharingModel>
    and <nameField> (neither applies to a custom metadata type), plus
    <visibility>.
    """
    ET.register_namespace("", METADATA_NS)
    root = ET.Element(f"{{{METADATA_NS}}}CustomObject")

    _sub(root, "deploymentStatus", mdt.deployment_status)
    if mdt.description:
        _sub(root, "description", mdt.description)

    for field in sorted(mdt.fields, key=lambda f: f.api_name):
        field_el = _sub(root, "fields")
        _fill_mdt_field(field_el, field)

    _sub(root, "label", mdt.label)
    _sub(root, "pluralLabel", mdt.plural_label)
    _sub(root, "visibility", mdt.visibility)

    ET.indent(root, space="    ")
    body = ET.tostring(root, encoding="unicode")
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + body + "\n"


_XSI_TYPE_BY_FIELD_TYPE = {
    "Number": "xsd:double",
    "Percent": "xsd:double",
    "Checkbox": "xsd:boolean",
    "Date": "xsd:date",
    "DateTime": "xsd:dateTime",
}


def _infer_xsi_type(field_api_name: str, value: str, mdt_type: Optional[MetadataType]) -> str:
    """
    What `xsi:type` a record's `<value>` needs, per its target field's real
    type when known (the accurate path - mdt_type is the MetadataType this
    same plan generated, if any) or a cheap heuristic on the value string
    itself otherwise (e.g. a record targeting a __mdt type that already
    exists in the org rather than one this plan also creates). Every type
    not otherwise mapped - Text, TextArea, LongTextArea, Email, Phone, URL,
    Picklist, MetadataRelationship - is xsd:string, so that's the fallback
    on both paths.
    """
    if mdt_type is not None:
        field = next((f for f in mdt_type.fields if f.api_name == field_api_name), None)
        if field is not None:
            return _XSI_TYPE_BY_FIELD_TYPE.get(field.type, "xsd:string")

    lowered = value.strip().lower()
    if lowered in ("true", "false"):
        return "xsd:boolean"
    try:
        float(value)
        return "xsd:double"
    except ValueError:
        return "xsd:string"


def generate_mdt_record(record: CustomMetadataRecord, mdt_type: Optional[MetadataType] = None) -> str:
    """
    Render a validated CustomMetadataRecord IR as one `CustomMetadata`
    member document. `mdt_type` - the MetadataType this record belongs to,
    when this same plan also generated it - lets `xsi:type` be resolved
    from each field's real declared type instead of the string-only
    heuristic in _infer_xsi_type.
    """
    ET.register_namespace("", METADATA_NS)
    ET.register_namespace("xsi", XSI_NS)
    root = ET.Element(f"{{{METADATA_NS}}}CustomMetadata")

    _sub(root, "label", record.label)
    _sub(root, "protected", _bool(record.protected))

    for field_api_name, value in sorted(record.values.items()):
        values_el = _sub(root, "values")
        _sub(values_el, "field", field_api_name)
        xsi_type = _infer_xsi_type(field_api_name, value, mdt_type)
        value_el = _sub(values_el, "value", value)
        value_el.set(f"{{{XSI_NS}}}type", xsi_type)

    ET.indent(root, space="    ")
    body = ET.tostring(root, encoding="unicode")
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + body + "\n"
