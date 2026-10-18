"""
Opening an existing Prompt Template from the org.

The reverse of `xmlgen_prompt_template`: the template's metadata file becomes the
same IR the tool generates, so it can be reviewed, revised and deployed again.
Only what the IR can say is read. A template of another kind (anything but a
`einstein_gpt__flex` one), an input type the IR does not know, or a response
format other than Text/JSON is refused with the reason, rather than imported as
something different from what is in the org - the same stance as opening a flow.
"""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from typing import Dict, List

from pydantic import ValidationError

from .ir_prompt_template import DEFINITION, PromptInput, PromptTemplate
from .prompt_activation import _files_from_zip
from .sfdc import MetadataClient, RetrieveError

_NS = {"m": "http://soap.sforce.com/2006/04/metadata"}
_TYPE_BY_DEFINITION = {v: k for k, v in DEFINITION.items()}


class UnsupportedTemplate(ValueError):
    """The template uses something the IR cannot represent."""


def _text(node, tag: str):
    found = node.find(f"m:{tag}", _NS)
    return found.text if found is not None and found.text is not None else None


def parse_prompt_template(xml: str, api_name: str) -> PromptTemplate:
    root = ET.fromstring(xml.lstrip("﻿"))
    kind = _text(root, "type")
    if kind != "einstein_gpt__flex":
        raise UnsupportedTemplate(
            f"{api_name} is a {kind or 'unknown'} template; only free-form (flex) templates can be opened here."
        )

    versions = root.findall("m:templateVersions", _NS)
    if not versions:
        raise UnsupportedTemplate(f"{api_name} has no version to open.")
    active = _text(root, "activeVersionIdentifier")
    version = next((v for v in versions if active and _text(v, "versionIdentifier") == active), versions[-1])

    inputs: List[PromptInput] = []
    for node in version.findall("m:inputs", _NS):
        definition = _text(node, "definition") or ""
        if definition not in _TYPE_BY_DEFINITION:
            raise UnsupportedTemplate(
                f"{api_name}: input {_text(node, 'apiName')!r} has type {definition!r}, which the tool cannot represent "
                "(only text, number, boolean, date and date-time inputs)."
            )
        inputs.append(PromptInput(
            name=_text(node, "apiName") or "",
            type=_TYPE_BY_DEFINITION[definition],
            description=_text(node, "description"),
            required=(_text(node, "required") or "true").lower() == "true",
        ))

    response_format = _text(version, "responseFormat") or "Text"
    if response_format not in ("Text", "JSON"):
        raise UnsupportedTemplate(f"{api_name}: response format {response_format!r} is not supported.")

    schema = None
    raw_schema = _text(version, "outputSchema")
    if raw_schema:
        try:
            schema = json.loads(raw_schema)
        except ValueError as exc:
            raise UnsupportedTemplate(f"{api_name}: its output schema is not valid JSON ({exc}).") from exc

    try:
        return PromptTemplate(
            api_name=api_name,
            label=_text(root, "masterLabel") or api_name,
            description=_text(root, "description"),
            content=_text(version, "content") or "",
            inputs=inputs,
            response_format=response_format,
            output_schema=schema,
            # A JSON template the org holds without a schema is still opened; saying so keeps
            # the generator's own rule (JSON needs a schema) intact for new templates.
            output_schema_unknown=response_format == "JSON" and schema is None,
            model=_text(version, "primaryModel") or PromptTemplate.model_fields["model"].default,
        )
    except ValidationError as exc:
        reasons = "; ".join(e["msg"] for e in exc.errors())
        raise UnsupportedTemplate(f"{api_name} cannot be opened as it is: {reasons}") from exc


async def list_prompt_templates(client: MetadataClient) -> List[Dict[str, str]]:
    """Prompt Templates the org's own metadata holds (managed ones are left out)."""
    found = await client.list_metadata("GenAiPromptTemplate")
    own = [f for f in found if f["full_name"] and not f.get("namespace_prefix")]
    return sorted(({"api_name": f["full_name"]} for f in own), key=lambda f: f["api_name"].lower())


async def retrieve_prompt_template(client: MetadataClient, api_name: str) -> PromptTemplate:
    zip_bytes = await client.wait_for_retrieve(await client.start_retrieve_types({"GenAiPromptTemplate": [api_name]}))
    files = _files_from_zip(zip_bytes)
    if api_name not in files:
        raise RetrieveError(f"{api_name} was not found in the org.")
    try:
        return parse_prompt_template(files[api_name], api_name)
    except UnsupportedTemplate as exc:
        raise RetrieveError(str(exc)) from exc
