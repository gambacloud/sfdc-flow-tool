"""
Apex Class IR.

Deliberately thin next to Flow's IR or the Object/Field IR: Apex is code, not
a structural graph or a flat field list, so there is nothing to decompose. The
model's job is still IR only in spirit - it writes a `body` string and nothing
else - but the body itself is unstructured text no Pydantic schema can check
the way a Condition or a picklist value can.

Because of that, validation here is two-tier: this model rejects an empty body
and a bad api_name the same way every other IR does, and the heuristic checks
below (brace/paren/bracket balance, a class declaration matching api_name) run
one step earlier - in the generator's repair loop, on the raw payload - to
catch the class of mistake Pydantic cannot see: broken syntax inside a plain
string field.

v1 stops there deliberately: no real compiler runs, so a class that balances
its braces and declares the right name can still fail to compile (an unknown
symbol, a type error). Catching that needs an actual deploy validate-only pass
against an org - see flowtool/cli_validate.py (Phase 3.5) for that as an
optional extra, not a replacement for this cheap, dependency-free first check.
"""

from __future__ import annotations

import re
from typing import List, Literal, Optional

from pydantic import BaseModel, field_validator

from .ir import _check_api_name

_CLASS_DECL_RE = re.compile(r"\bclass\s+(\w+)")

# Brackets worth checking, and what closes each one.
_OPENERS_TO_CLOSERS = {"{": "}", "(": ")", "[": "]"}
_CLOSERS_TO_OPENERS = {v: k for k, v in _OPENERS_TO_CLOSERS.items()}


class ApexClass(BaseModel):
    api_name: str
    body: str
    description: Optional[str] = None
    api_version: str = "62.0"
    status: Literal["Active", "Inactive"] = "Active"

    @field_validator("api_name")
    @classmethod
    def valid_name(cls, v: str) -> str:
        return _check_api_name(v, "apex class api_name", max_length=40)

    @field_validator("body")
    @classmethod
    def not_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("apex class body must not be empty")
        return v


def heuristic_errors(api_name: str, body: str) -> List[str]:
    """
    Cheap, dependency-free sanity checks on Apex source: brace/paren/bracket
    balance, and a class declaration that matches api_name. Catches the
    mistakes a repair round can fix on its own; anything subtler (an
    unresolved symbol, a type error) needs a real compile and is out of scope
    here - see the module docstring.

    Runs on the raw payload before Pydantic sees it, the same as the
    dropped-element guard in llm.py's repair loop - so this takes strings
    straight from the JSON the model returned, not a validated ApexClass.
    """
    problems: List[str] = []

    stack: List[str] = []
    for char in body:
        if char in _OPENERS_TO_CLOSERS:
            stack.append(char)
        elif char in _CLOSERS_TO_OPENERS:
            if not stack or stack[-1] != _CLOSERS_TO_OPENERS[char]:
                problems.append(
                    f"unbalanced {char!r} - no matching "
                    f"{_CLOSERS_TO_OPENERS[char]!r} open at that point"
                )
                break
            stack.pop()
    else:
        if stack:
            problems.append(f"unbalanced brackets: {''.join(stack)!r} never closed")

    match = _CLASS_DECL_RE.search(body)
    if not match:
        problems.append("no 'class <Name>' declaration found in the body")
    elif api_name and match.group(1) != api_name:
        problems.append(
            f"the class is declared as {match.group(1)!r}, but api_name is "
            f"{api_name!r} - they must match, since the deployed file is "
            "named after api_name"
        )

    return problems
