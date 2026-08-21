"""
Custom Object / Custom Field IR.

Sibling to ir.py's Flow: the model's only job is producing a valid instance of
these models, checked here before a single byte of metadata exists. Deliberately
flat compared to Flow - there is no element graph, just a shell (CustomObject)
and the fields hung off it (CustomField), matching how Salesforce itself splits
CustomObject and CustomField into separate deployable metadata types.

Scope is the common subset an implementer reaches for first: Text, Number,
Checkbox, Picklist, Lookup and MasterDetail. Roll-up summaries, formula fields
and exotic types are deliberately out of scope until a real need shows up.
"""

from __future__ import annotations

from typing import List, Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

from .ir import _check_api_name

FieldType = Literal[
    "Text", "Number", "Checkbox", "Picklist", "Lookup", "MasterDetail"
]

SharingModel = Literal["Private", "Read", "ReadWrite", "ReadWriteTransfer", "ControlledByParent"]


def _with_suffix(value: str) -> str:
    """
    A custom object's or custom field's api_name always ends `__c`. The model
    is not trusted to remember the suffix - it is appended here rather than
    validated and rejected, the same call made for the `GC_` flow prefix in
    llm.py: one fewer thing a repair round has to catch.
    """
    return value if value.endswith("__c") else f"{value}__c"


class CustomField(BaseModel):
    api_name: str = Field(description="Suffixed with __c automatically if missing")
    label: str
    type: FieldType
    object_api_name: str = Field(
        description="The object this field is added to - a standard object "
        "('Account'), or a custom object's api_name (suffixed __c automatically)"
    )
    description: Optional[str] = None
    required: bool = False
    unique: bool = False
    default_value: Optional[str] = Field(
        default=None,
        description="Literal or formula, written exactly as Salesforce expects "
        "it for this field's type. Not checked before the org sees it.",
    )

    # Text
    length: Optional[int] = Field(default=None, description="Text only, max 255")

    # Number
    precision: Optional[int] = Field(default=None, description="Number only: total digits")
    scale: Optional[int] = Field(default=None, description="Number only: digits after the point")

    # Picklist
    picklist_values: List[str] = Field(default_factory=list)

    # Lookup / MasterDetail
    reference_to: Optional[str] = Field(
        default=None,
        description="Lookup/MasterDetail only: the target object's api_name",
    )
    relationship_label: Optional[str] = None
    relationship_name: Optional[str] = None

    @field_validator("api_name", "object_api_name")
    @classmethod
    def valid_and_suffixed(cls, v: str) -> str:
        _check_api_name(v.removesuffix("__c"), "field api_name", max_length=40)
        return _with_suffix(v)

    @field_validator("reference_to")
    @classmethod
    def valid_reference(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        # A standard object ("Account") has no length limit worth enforcing -
        # only a custom one (ending __c) is capped at 40 characters for its
        # base name, the same as any other custom api_name here.
        if v.endswith("__c"):
            _check_api_name(v.removesuffix("__c"), "reference_to", max_length=40)
        else:
            _check_api_name(v, "reference_to")
        return v

    @model_validator(mode="after")
    def type_specific_shape(self) -> "CustomField":
        if self.type == "Text" and self.length is None:
            self.length = 255
        if self.type != "Text" and self.length is not None:
            raise ValueError(f"field {self.api_name!r}: length only applies to Text")

        if self.type == "Number" and self.precision is None:
            raise ValueError(f"field {self.api_name!r}: Number requires precision")
        if self.type != "Number" and (self.precision is not None or self.scale is not None):
            raise ValueError(
                f"field {self.api_name!r}: precision/scale only apply to Number"
            )

        if self.type == "Picklist" and not self.picklist_values:
            raise ValueError(f"field {self.api_name!r}: Picklist requires picklist_values")
        if self.type != "Picklist" and self.picklist_values:
            raise ValueError(
                f"field {self.api_name!r}: picklist_values only applies to Picklist"
            )

        if self.type in ("Lookup", "MasterDetail") and not self.reference_to:
            raise ValueError(f"field {self.api_name!r}: {self.type} requires reference_to")
        if self.type not in ("Lookup", "MasterDetail") and self.reference_to:
            raise ValueError(
                f"field {self.api_name!r}: reference_to only applies to Lookup/MasterDetail"
            )
        if self.type == "MasterDetail" and self.required:
            # A master-detail field is implicitly required by Salesforce; the
            # metadata does not carry a `required` flag for it at all, so an
            # explicit one here is a request the deploy will silently ignore.
            raise ValueError(
                f"field {self.api_name!r}: MasterDetail is always required - drop "
                "the explicit `required` flag"
            )
        return self


class CustomObject(BaseModel):
    api_name: str = Field(description="Suffixed with __c automatically if missing")
    label: str
    plural_label: str
    description: Optional[str] = None
    record_name: str = "Name"
    record_name_type: Literal["Text", "AutoNumber"] = "Text"
    record_name_display_format: Optional[str] = Field(
        default=None, description="AutoNumber only, e.g. 'INV-{0000}'"
    )
    deployment_status: Literal["Deployed", "InDevelopment"] = "Deployed"
    sharing_model: SharingModel = "ReadWrite"

    @field_validator("api_name")
    @classmethod
    def valid_and_suffixed(cls, v: str) -> str:
        _check_api_name(v.removesuffix("__c"), "object api_name", max_length=40)
        return _with_suffix(v)

    @model_validator(mode="after")
    def autonumber_needs_format(self) -> "CustomObject":
        if self.record_name_type == "AutoNumber" and not self.record_name_display_format:
            raise ValueError(
                f"object {self.api_name!r}: AutoNumber record name requires "
                "record_name_display_format"
            )
        if self.record_name_type == "Text" and self.record_name_display_format:
            raise ValueError(
                f"object {self.api_name!r}: record_name_display_format only "
                "applies to AutoNumber"
            )
        return self
