"""
An optional external reference (a JIRA key, a ticket URL, ...) stamped onto
whatever a session or plan builds, so a component in the org can be traced
back to the request that produced it.

Applied to a copy at deploy/export time, never to the stored IR - the IR the
person reviewed stays exactly as generated, and changing or clearing the
reference later never leaves a stale tag behind or stacks a second one.

Where it lands: the `description` of every metadata type that deploys one
(Flow and each of its elements, Object, Field, Custom Metadata Type, Platform
Event, Permission Set), and a leading comment in Apex and LWC source - those
have no deployed description to put it in.
"""

from __future__ import annotations

import re
from typing import Any, Optional

from .ir import Flow
from .ir_apex import ApexClass, ApexTrigger
from .ir_lwc import LightningComponent
from .ir_mdt import MetadataType
from .ir_object import CustomField, CustomObject
from .ir_platform_event import PlatformEvent

# Deliberately narrow: this text is written into XML and into source comments
# on the org, so nothing that could close a comment (`*/`), start markup or
# break a line is allowed - a JIRA key or ticket URL needs none of it.
_REFERENCE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _\-#./:]{0,79}$")


def clean_reference(raw: Optional[str]) -> Optional[str]:
    """None for blank; the trimmed value if valid; ValueError otherwise."""
    if raw is None:
        return None
    value = raw.strip()
    if not value:
        return None
    if not _REFERENCE_RE.match(value):
        raise ValueError(
            "Reference may only contain letters, digits, spaces and - _ # . / : "
            "(up to 80 characters), e.g. JIRA-123."
        )
    return value


def tag_description(existing: Optional[str], reference: str) -> str:
    tag = f"[{reference}]"
    if existing and existing.lstrip().startswith(tag):
        return existing
    return f"{tag} {existing}" if existing else tag


def _tag_source(body: str, reference: str, comment: str) -> str:
    line = f"{comment} Ref: {reference}"
    if body.lstrip().startswith(line):
        return body
    return f"{line}\n{body}"


def tag_value(value: Any, reference: Optional[str]) -> Any:
    """A tagged deep copy of `value`, or `value` itself when there is nothing
    to stamp (no reference, or a type that carries nowhere to put one)."""
    if not reference:
        return value

    if isinstance(value, Flow):
        flow = value.model_copy(deep=True)
        flow.description = tag_description(flow.description, reference)
        for element in flow.elements:
            if "description" in type(element).model_fields:
                element.description = tag_description(element.description, reference)
        return flow
    if isinstance(value, (CustomObject, CustomField, MetadataType, PlatformEvent)):
        copy = value.model_copy(deep=True)
        copy.description = tag_description(copy.description, reference)
        return copy
    if isinstance(value, (ApexClass, ApexTrigger)):
        copy = value.model_copy(deep=True)
        copy.body = _tag_source(copy.body, reference, "//")
        return copy
    if isinstance(value, LightningComponent):
        copy = value.model_copy(deep=True)
        copy.js = _tag_source(copy.js, reference, "//")
        return copy
    return value
