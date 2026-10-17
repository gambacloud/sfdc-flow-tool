"""PromptTemplate IR -> GenAiPromptTemplate metadata XML. Deterministic, no LLM."""

from __future__ import annotations

import xml.etree.ElementTree as ET

from .ir_prompt_template import DEFINITION, PromptTemplate
from .xmlgen import METADATA_NS, _bool, _sub


def generate_prompt_template(template: PromptTemplate) -> str:
    ET.register_namespace("", METADATA_NS)
    root = ET.Element(f"{{{METADATA_NS}}}GenAiPromptTemplate")

    if template.description:
        _sub(root, "description", template.description)
    _sub(root, "developerName", template.api_name)
    _sub(root, "masterLabel", template.label)

    version = _sub(root, "templateVersions")
    _sub(version, "content", template.content)
    for item in template.inputs:
        el = _sub(version, "inputs")
        _sub(el, "apiName", item.name)
        _sub(el, "definition", DEFINITION[item.type])
        if item.description:
            _sub(el, "description", item.description)
        _sub(el, "masterLabel", item.name)
        _sub(el, "referenceName", f"Input:{item.name}")
        _sub(el, "required", _bool(item.required))
    _sub(version, "primaryModel", template.model)
    if template.response_format == "JSON":
        _sub(version, "responseFormat", "JSON")
        _sub(version, "outputSchema", template.schema_json())
    # No versionIdentifier: the org rejects an invented one and assigns its own.
    _sub(version, "status", "Published")

    _sub(root, "type", "einstein_gpt__flex")
    _sub(root, "visibility", "Global")

    ET.indent(root, space="    ")
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding="unicode") + "\n"
