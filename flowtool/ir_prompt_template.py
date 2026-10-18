"""
Prompt Template IR (Prompt Builder, "flex" template).

A prompt template is how a Flow asks the model a question and gets text, or
JSON, back: the Flow's action is `generatePromptResponse` with the template's
api name as `actionName`, each input passed as `Input:<name>`, and the answer
read from the action's `promptResponse` output.

Shape established against a real scratch org (feature Einstein1AIPlatform,
API 68), not assumed:
- the template type is `einstein_gpt__flex`, visibility `Global`;
- a version carries `content`, `inputs` (each `primitive://String` etc. with
  referenceName `Input:<name>`), `primaryModel`, `responseFormat` and, for JSON,
  an `outputSchema` string;
- a made-up `versionIdentifier` is rejected ("version identifier ... invalid") -
  leave it out and the org assigns one;
- the content refers to inputs as `{!$Input:name}`.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

from .ir import _check_api_name

InputType = Literal["String", "Number", "Boolean", "Date", "DateTime"]
ResponseFormat = Literal["Text", "JSON"]

DEFAULT_MODEL = "sfdc_ai__DefaultOpenAIGPT4OmniMini"

# What the org's `definition` attribute calls each type.
DEFINITION = {
    "String": "primitive://String",
    "Number": "primitive://Number",
    "Boolean": "primitive://Boolean",
    "Date": "primitive://Date",
    "DateTime": "primitive://DateTime",
}

_INPUT_REF_RE = re.compile(r"\{!\$Input:([A-Za-z][A-Za-z0-9_]*)\}")


class PromptInput(BaseModel):
    name: str = Field(description="Letters, digits and underscores; the content refers to it as {!$Input:name}")
    type: InputType = "String"
    description: Optional[str] = None
    required: bool = True

    @field_validator("name")
    @classmethod
    def valid_name(cls, v: str) -> str:
        return _check_api_name(v, "prompt input name")


class PromptTemplate(BaseModel):
    api_name: str
    label: str
    description: Optional[str] = None
    # Not deployed - see CustomField.reasoning in ir_object.py.
    reasoning: Optional[str] = None
    content: str = Field(
        description="The instructions sent to the model. Refer to each input as "
        "{!$Input:name}. When response_format is JSON, say exactly which keys to return."
    )
    inputs: List[PromptInput] = Field(default_factory=list)
    response_format: ResponseFormat = "Text"
    output_schema: Optional[Dict[str, Any]] = Field(
        default=None,
        description="JSON only: a JSON Schema for the reply (type object, properties, required).",
    )
    model: str = DEFAULT_MODEL
    # Set only by opening a template from the org that answers in JSON but carries no schema.
    # Never set it when writing a new template: JSON then needs output_schema (checked below).
    output_schema_unknown: bool = False

    @field_validator("api_name")
    @classmethod
    def valid_name(cls, v: str) -> str:
        return _check_api_name(v, "prompt template api_name", max_length=80)

    @field_validator("content")
    @classmethod
    def content_not_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("prompt content must not be empty")
        return v

    @model_validator(mode="after")
    def consistent(self) -> "PromptTemplate":
        names = [i.name for i in self.inputs]
        dupes = sorted({n for n in names if names.count(n) > 1})
        if dupes:
            raise ValueError(f"duplicate input name(s): {', '.join(dupes)}")

        used = set(_INPUT_REF_RE.findall(self.content))
        undeclared = sorted(used - set(names))
        if undeclared:
            raise ValueError(
                "content refers to input(s) that are not declared: "
                + ", ".join("{!$Input:%s}" % n for n in undeclared)
            )
        unused = [i.name for i in self.inputs if i.required and i.name not in used]
        if unused:
            raise ValueError(
                f"required input(s) never used in the content: {', '.join(unused)} - "
                "reference them as {!$Input:name} or drop them"
            )

        if self.response_format == "JSON":
            schema = self.output_schema
            if schema is None and self.output_schema_unknown:
                return self
            if not schema or schema.get("type") != "object" or not schema.get("properties"):
                raise ValueError(
                    "response_format JSON needs output_schema: a JSON Schema with type "
                    "'object' and its 'properties'"
                )
            missing = [r for r in schema.get("required", []) if r not in schema["properties"]]
            if missing:
                raise ValueError(f"output_schema requires undeclared propert(ies): {', '.join(missing)}")
        elif self.output_schema is not None:
            raise ValueError("output_schema only applies to response_format JSON")
        return self

    def schema_json(self) -> Optional[str]:
        """The schema as the compact string the metadata carries."""
        return json.dumps(self.output_schema, separators=(",", ":")) if self.output_schema else None

    def output_keys(self) -> List[str]:
        return list((self.output_schema or {}).get("properties", {}))
