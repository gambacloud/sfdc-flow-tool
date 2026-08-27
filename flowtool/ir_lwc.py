"""
Lightning Web Component IR.

Unlike Apex, an LWC is not one file - it is a small bundle (`.js`, `.html`,
optionally `.css`, plus a `.js-meta.xml` sidecar). The IR mirrors that split:
`js`/`html`/`css` are LLM-authored free text (validated by cheap heuristics,
the same spirit as ApexClass.body - a real compiler is out of scope), while
`is_exposed`/`targets`/`api_version` are structured fields the model fills
directly. The LLM never authors `.js-meta.xml` itself; xmlgen_lwc.py builds it
deterministically from those structured fields, the same split ApexClass keeps
between its free-form `body` and its synthesized `.cls-meta.xml`.

`api_name` follows LWC's own naming rule, not Salesforce's general API-name
rule every other IR uses (`_check_api_name` in ir.py): a component folder/tag
name must be camelCase - start with a lowercase letter, letters and digits
only, no underscores - since it is also a valid HTML custom-element tag name
once hyphenated (`myComponent` -> `<c-my-component>`).
"""

from __future__ import annotations

import re
from typing import List, Optional

from pydantic import BaseModel, Field, field_validator

from .ir_apex import _bracket_balance_errors

_LWC_NAME_RE = re.compile(r"^[a-z][A-Za-z0-9]*$")
_CLASS_DECL_RE = re.compile(r"\bexport\s+default\s+class\s+(\w+)\s+extends\s+LightningElement\b")


def _pascal_case(api_name: str) -> str:
    return api_name[:1].upper() + api_name[1:]


class LightningComponent(BaseModel):
    api_name: str
    js: str
    html: str
    css: Optional[str] = None
    description: Optional[str] = None
    is_exposed: bool = False
    targets: List[str] = Field(default_factory=list)
    api_version: str = "62.0"

    @field_validator("api_name")
    @classmethod
    def valid_name(cls, v: str) -> str:
        if not _LWC_NAME_RE.match(v):
            raise ValueError(
                f"lwc api_name {v!r} must be camelCase: start with a lowercase "
                "letter, letters and digits only, no underscores or spaces "
                "(it doubles as the component's HTML tag name)"
            )
        if len(v) > 40:
            raise ValueError(f"lwc api_name {v!r} is over Salesforce's 40-character limit")
        return v

    @field_validator("js")
    @classmethod
    def js_not_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("lwc js must not be empty")
        return v

    @field_validator("html")
    @classmethod
    def html_not_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("lwc html must not be empty")
        return v


def heuristic_errors(api_name: str, js: str, html: str) -> List[str]:
    """
    Cheap, dependency-free sanity checks on an LWC's js/html, run on the raw
    payload before Pydantic sees it - same mechanism as ir_apex.py's
    heuristic_errors, plugged in via the generator's `_extra_error` hook.

    Not a real JS/HTML parser: brace balance on the js (reusing ir_apex.py's
    `_bracket_balance_errors`), a check that the exported class name matches
    the PascalCase of api_name, and a light `<template>` wrapper + angle-
    bracket balance check on the html. Catches the class of mistake a repair
    round can fix; a real compile is out of scope, same as Apex.
    """
    problems = _bracket_balance_errors(js)

    match = _CLASS_DECL_RE.search(js)
    if not match:
        problems.append(
            "no 'export default class <Name> extends LightningElement' "
            "declaration found in js"
        )
    elif api_name and match.group(1) != _pascal_case(api_name):
        problems.append(
            f"the exported class is named {match.group(1)!r}, but api_name is "
            f"{api_name!r} - the class must be named {_pascal_case(api_name)!r} "
            "(PascalCase of api_name)"
        )

    stripped = html.strip()
    if not (stripped.startswith("<template") and stripped.endswith("</template>")):
        problems.append("html must be wrapped in a single <template>...</template> root")

    open_count = html.count("<template")
    close_count = html.count("</template>")
    if open_count != close_count:
        problems.append(
            f"unbalanced <template> tags: {open_count} opening vs {close_count} closing"
        )

    return problems
