"""
Platform Event IR -> metadata XML.

Deterministic, no LLM involved - same rule as xmlgen.py/xmlgen_object.py/
xmlgen_mdt.py, whose `_sub` helper and namespace this reuses.

A platform event deploys under the same Metadata API type name as a regular
Custom Object (`CustomObject`), __e-suffixed, with `<eventType>`/
`<publishBehavior>` added and no `<sharingModel>`/`<nameField>` - confirmed
against Salesforce's own Metadata API and Platform Events documentation, not
assumed (see ir_platform_event.py's module docstring for the field-type
research). `eventType` is not part of the IR at all: `HighVolume` is the only
value Salesforce still accepts, so it is emitted here unconditionally rather
than exposed as a choice with exactly one valid answer.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET

from .ir_platform_event import PlatformEvent, PlatformEventField
from .xmlgen import METADATA_NS, _bool, _sub


def _fill_field(el: ET.Element, field: PlatformEventField) -> None:
    """
    Populate `el` with one PlatformEventField's children, alphabetically by
    tag - same convention xmlgen_object.py's _fill_field and xmlgen_mdt.py's
    _fill_mdt_field follow, and for the same reason (the Metadata API is
    picky about child ordering on retrieve-diff).
    """
    if field.type == "Checkbox":
        _sub(el, "defaultValue", field.default_value or "false")

    if field.description:
        _sub(el, "description", field.description)

    _sub(el, "fullName", field.api_name)
    _sub(el, "label", field.label)

    if field.type in ("Text", "LongTextArea"):
        _sub(el, "length", str(field.length))

    if field.type == "Number":
        _sub(el, "precision", str(field.precision))

    _sub(el, "required", _bool(field.required))

    if field.type == "Number":
        _sub(el, "scale", str(field.scale or 0))

    _sub(el, "type", field.type)

    if field.type == "LongTextArea":
        _sub(el, "visibleLines", str(field.visible_lines))


def generate_platform_event(event: PlatformEvent) -> str:
    """
    Render a validated PlatformEvent IR as deployable metadata XML - the
    `.object` content for a __e type, the same file shape a regular
    CustomObject/__mdt type uses (xmlgen_object.generate_object,
    xmlgen_mdt.generate_mdt_type) minus <sharingModel>/<nameField> (neither
    applies to a platform event), plus <eventType>/<publishBehavior>.
    """
    ET.register_namespace("", METADATA_NS)
    root = ET.Element(f"{{{METADATA_NS}}}CustomObject")

    _sub(root, "deploymentStatus", "Deployed")
    if event.description:
        _sub(root, "description", event.description)
    _sub(root, "eventType", "HighVolume")

    for field in sorted(event.fields, key=lambda f: f.api_name):
        field_el = _sub(root, "fields")
        _fill_field(field_el, field)

    _sub(root, "label", event.label)
    _sub(root, "pluralLabel", event.plural_label)
    _sub(root, "publishBehavior", event.publish_behavior)

    ET.indent(root, space="    ")
    body = ET.tostring(root, encoding="unicode")
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + body + "\n"
