"""
Platform Event IR.

Sibling to ir_mdt.py's MetadataType, not a reuse of it: a platform event is
also a CustomObject under the hood (__e suffix instead of __mdt), but its own
field-type set is smaller still - Salesforce restricts a platform event's
custom fields to Checkbox, Date, Date/Time, Number, Text and Text Area (Long)
only (confirmed against Salesforce's own Platform Events developer guide, not
assumed) - no Picklist, Lookup, Master-Detail, Currency or Time, all of which
at least one of CustomField/MetadataField allows.

Like a Custom Metadata Type, a platform event's fields are embedded in this
one IR rather than split into a separate per-field planner step - "an
Order_Placed event with an AccountId and an Amount field" is one request, not
several, the same reasoning ir_mdt.py's module docstring gives.

`eventType` has no field here at all: the only value Salesforce still accepts
is `HighVolume` (`StandardVolume` is deprecated and now errors on create), so
there is no real choice to expose - xmlgen_platform_event.py always emits it.
"""

from __future__ import annotations

from typing import List, Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

from .ir import _check_api_name

PlatformEventFieldType = Literal["Text", "Number", "Checkbox", "Date", "DateTime", "LongTextArea"]

PublishBehavior = Literal["PublishAfterCommit", "PublishImmediately"]


def _with_c_suffix(value: str) -> str:
    return value if value.endswith("__c") else f"{value}__c"


def _with_e_suffix(value: str) -> str:
    return value if value.endswith("__e") else f"{value}__e"


class PlatformEventField(BaseModel):
    api_name: str = Field(description="Suffixed with __c automatically if missing")
    label: str
    type: PlatformEventFieldType
    description: Optional[str] = None
    required: bool = False
    default_value: Optional[str] = Field(
        default=None,
        description="Checkbox only: 'true' or 'false' - Salesforce requires an "
        "explicit default for a Checkbox field, the same as CustomField's own.",
    )

    # Text / LongTextArea
    length: Optional[int] = Field(
        default=None, description="Text (max 255) or LongTextArea (max 131072)"
    )
    visible_lines: Optional[int] = Field(
        default=None, description="LongTextArea only: how many lines show at once"
    )

    # Number
    precision: Optional[int] = Field(default=None, description="Number only: total digits")
    scale: Optional[int] = Field(default=None, description="Number only: digits after the point")

    @field_validator("api_name")
    @classmethod
    def valid_and_suffixed(cls, v: str) -> str:
        _check_api_name(v.removesuffix("__c"), "platform event field api_name", max_length=40)
        return _with_c_suffix(v)

    @model_validator(mode="after")
    def type_specific_shape(self) -> "PlatformEventField":
        if self.type == "Text" and self.length is None:
            self.length = 255
        if self.type == "LongTextArea" and self.length is None:
            self.length = 32768
        if self.type not in ("Text", "LongTextArea") and self.length is not None:
            raise ValueError(
                f"field {self.api_name!r}: length only applies to Text/LongTextArea"
            )

        # Confirmed live (checkOnly) against a real dev org: LongTextArea
        # needs both length and visibleLines, or Salesforce rejects the
        # deploy - "Must specify 'visibleLines' for a CustomField of type
        # LongTextArea".
        if self.type == "LongTextArea" and self.visible_lines is None:
            self.visible_lines = 3
        if self.type != "LongTextArea" and self.visible_lines is not None:
            raise ValueError(
                f"field {self.api_name!r}: visible_lines only applies to LongTextArea"
            )

        if self.type == "Number" and self.precision is None:
            raise ValueError(f"field {self.api_name!r}: Number requires precision")
        if self.type != "Number" and (self.precision is not None or self.scale is not None):
            raise ValueError(
                f"field {self.api_name!r}: precision/scale only apply to Number"
            )

        # A Checkbox always holds a value - Salesforce rejects the deploy
        # without an explicit default, confirmed live (checkOnly) against a
        # real dev org: "Must specify 'defaultValue' for a CustomField of
        # type Checkbox". Every other type here has no defaultValue concept.
        if self.type == "Checkbox" and self.default_value is None:
            self.default_value = "false"
        if self.type != "Checkbox" and self.default_value is not None:
            raise ValueError(f"field {self.api_name!r}: default_value only applies to Checkbox")
        return self


class PlatformEvent(BaseModel):
    api_name: str = Field(description="Suffixed with __e automatically if missing")
    label: str
    plural_label: str
    description: Optional[str] = None
    publish_behavior: PublishBehavior = "PublishAfterCommit"
    fields: List[PlatformEventField] = Field(min_length=1)

    @field_validator("api_name")
    @classmethod
    def valid_and_suffixed(cls, v: str) -> str:
        _check_api_name(v.removesuffix("__e"), "platform event api_name", max_length=40)
        return _with_e_suffix(v)
