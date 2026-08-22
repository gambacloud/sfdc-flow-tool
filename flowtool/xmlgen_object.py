"""
CustomObject / CustomField IR -> metadata XML.

Deterministic, no LLM involved - the same rule as xmlgen.py, whose `_sub`/
`_bool` helpers and namespace this reuses.

A new object's own fields are embedded directly inside its `.object` file
(generate_object's `fields` argument), not deployed as separate standalone
CustomField members - confirmed live: a standalone CustomField, correctly
named and correctly suffixed, still failed checkOnly with "named in
package.xml, but was not found in zipped directory" for every field tried,
on both a brand-new object and a pre-existing standard one. Embedding is not
a guess - it is exactly the shape Salesforce itself returns a CustomObject
in on retrieve, so there is no ambiguity about the convention. generate_field
still exists for a field added to an object this deploy does *not* also
create (where there is nothing to embed it into) - that path remains the
open question the live failure surfaced, not yet resolved.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from typing import Sequence

from .ir_object import CustomField, CustomObject
from .xmlgen import METADATA_NS, _bool, _sub


def _fill_field(el: ET.Element, field: CustomField) -> None:
    """
    Populate `el` with one CustomField's children, alphabetically by tag -
    matching what Salesforce itself writes on retrieve, the same ordering
    rule xmlgen.py follows for Flow XML and for the same reason: the
    Metadata API is picky about it. Shared by generate_field (a standalone
    CustomField document) and generate_object (fields embedded under
    <fields> inside a CustomObject document) - the schema for one field's
    own properties is identical either way, only the parent differs.
    """
    has_required = field.type in ("Text", "Number", "Picklist", "Lookup")
    has_unique = field.type in ("Text", "Number")

    # defaultValue
    if field.type == "Checkbox":
        # A Checkbox has no `required` concept - it always holds a value - but
        # it must carry an explicit default, or the org rejects the deploy.
        _sub(el, "defaultValue", field.default_value or "false")
    elif field.default_value is not None:
        _sub(el, "defaultValue", field.default_value)

    # description
    if field.description:
        _sub(el, "description", field.description)

    # fullName
    _sub(el, "fullName", field.api_name)

    # label
    _sub(el, "label", field.label)

    # length
    if field.type == "Text":
        _sub(el, "length", str(field.length))

    # precision
    if field.type == "Number":
        _sub(el, "precision", str(field.precision))

    # referenceTo
    if field.type in ("Lookup", "MasterDetail"):
        _sub(el, "referenceTo", field.reference_to)

    # relationshipLabel
    if field.type in ("Lookup", "MasterDetail") and field.relationship_label:
        _sub(el, "relationshipLabel", field.relationship_label)

    # relationshipName
    if field.type in ("Lookup", "MasterDetail") and field.relationship_name:
        _sub(el, "relationshipName", field.relationship_name)

    # required
    if has_required:
        _sub(el, "required", _bool(field.required))

    # scale
    if field.type == "Number":
        _sub(el, "scale", str(field.scale or 0))

    # type
    _sub(el, "type", field.type)

    # unique
    if has_unique and field.unique:
        _sub(el, "unique", "true")

    # valueSet
    if field.type == "Picklist":
        value_set = _sub(el, "valueSet")
        definition = _sub(value_set, "valueSetDefinition")
        _sub(definition, "sorted", "false")
        for value in field.picklist_values:
            entry = _sub(definition, "value")
            _sub(entry, "fullName", value)
            _sub(entry, "default", "false")


def generate_object(obj: CustomObject, fields: Sequence[CustomField] = ()) -> str:
    """
    Render a validated CustomObject IR as deployable metadata XML.

    `fields` are this object's own new fields, embedded inline as <fields>
    children - see the module docstring for why this replaced deploying them
    as separate CustomField members. Order follows CustomObject's own
    alphabetical child ordering: <fields> sorts between <description> and
    <label>, and the fields themselves are sorted by api_name so the output
    is stable run to run regardless of what order they were generated in.
    """
    ET.register_namespace("", METADATA_NS)
    root = ET.Element(f"{{{METADATA_NS}}}CustomObject")

    _sub(root, "deploymentStatus", obj.deployment_status)
    if obj.description:
        _sub(root, "description", obj.description)

    for field in sorted(fields, key=lambda f: f.api_name):
        field_el = _sub(root, "fields")
        _fill_field(field_el, field)

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
    Render a validated CustomField IR as a standalone metadata document -
    for a field added to an object this same deploy does not also create
    (so there is nothing to embed it into). See the module docstring: this
    path is the one still unconfirmed against a real org.
    """
    ET.register_namespace("", METADATA_NS)
    root = ET.Element(f"{{{METADATA_NS}}}CustomField")
    _fill_field(root, field)

    ET.indent(root, space="    ")
    body = ET.tostring(root, encoding="unicode")
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + body + "\n"
