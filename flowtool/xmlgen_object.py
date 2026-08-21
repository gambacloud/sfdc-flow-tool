"""
CustomObject / CustomField IR -> metadata XML.

Deterministic, no LLM involved - the same rule as xmlgen.py, whose `_sub`/
`_bool` helpers and namespace this reuses. Flat by design: unlike a Flow there
is no element graph or layout to compute, just the object shell and one field
at a time, matching how Salesforce deploys CustomObject and CustomField as
separate metadata members.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET

from .ir_object import CustomField, CustomObject
from .xmlgen import METADATA_NS, _bool, _sub


def generate_object(obj: CustomObject) -> str:
    """
    Render a validated CustomObject IR as deployable metadata XML.

    This is the object shell only - its fields are separate CustomField
    members (see generate_field), deployed alongside it.
    """
    ET.register_namespace("", METADATA_NS)
    root = ET.Element(f"{{{METADATA_NS}}}CustomObject")

    _sub(root, "deploymentStatus", obj.deployment_status)
    if obj.description:
        _sub(root, "description", obj.description)
    _sub(root, "label", obj.label)

    name_field = _sub(root, "nameField")
    if obj.record_name_type == "AutoNumber":
        _sub(name_field, "displayFormat", obj.record_name_display_format)
    _sub(name_field, "label", obj.record_name)
    _sub(name_field, "type", obj.record_name_type)

    _sub(root, "pluralLabel", obj.plural_label)
    _sub(root, "sharingModel", obj.sharing_model)

    ET.indent(root, space="    ")
    body = ET.tostring(root, encoding="unicode")
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + body + "\n"


def generate_field(field: CustomField) -> str:
    """
    Render a validated CustomField IR as deployable metadata XML.

    Children are emitted alphabetically by tag, matching what Salesforce
    itself writes on retrieve - the same ordering rule xmlgen.py follows for
    Flow XML, and for the same reason: the Metadata API is picky about it.
    """
    ET.register_namespace("", METADATA_NS)
    root = ET.Element(f"{{{METADATA_NS}}}CustomField")

    has_required = field.type in ("Text", "Number", "Picklist", "Lookup")
    has_unique = field.type in ("Text", "Number")

    # defaultValue
    if field.type == "Checkbox":
        # A Checkbox has no `required` concept - it always holds a value - but
        # it must carry an explicit default, or the org rejects the deploy.
        _sub(root, "defaultValue", field.default_value or "false")
    elif field.default_value is not None:
        _sub(root, "defaultValue", field.default_value)

    # description
    if field.description:
        _sub(root, "description", field.description)

    # fullName
    _sub(root, "fullName", field.api_name)

    # label
    _sub(root, "label", field.label)

    # length
    if field.type == "Text":
        _sub(root, "length", str(field.length))

    # precision
    if field.type == "Number":
        _sub(root, "precision", str(field.precision))

    # referenceTo
    if field.type in ("Lookup", "MasterDetail"):
        _sub(root, "referenceTo", field.reference_to)

    # relationshipLabel
    if field.type in ("Lookup", "MasterDetail") and field.relationship_label:
        _sub(root, "relationshipLabel", field.relationship_label)

    # relationshipName
    if field.type in ("Lookup", "MasterDetail") and field.relationship_name:
        _sub(root, "relationshipName", field.relationship_name)

    # required
    if has_required:
        _sub(root, "required", _bool(field.required))

    # scale
    if field.type == "Number":
        _sub(root, "scale", str(field.scale or 0))

    # type
    _sub(root, "type", field.type)

    # unique
    if has_unique and field.unique:
        _sub(root, "unique", "true")

    # valueSet
    if field.type == "Picklist":
        value_set = _sub(root, "valueSet")
        definition = _sub(value_set, "valueSetDefinition")
        _sub(definition, "sorted", "false")
        for value in field.picklist_values:
            entry = _sub(definition, "value")
            _sub(entry, "fullName", value)
            _sub(entry, "default", "false")

    ET.indent(root, space="    ")
    body = ET.tostring(root, encoding="unicode")
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + body + "\n"
