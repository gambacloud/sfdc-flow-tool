"""
Custom Metadata Type / Custom Metadata Record IR.

Sibling to ir_object.py's CustomObject/CustomField, not a reuse of it: a
__mdt type is close to a Custom Object but not close enough to share the
model safely - __c suffixing and sharingModel are unconditional there, and
its FieldType covers only the 6 types a regular custom field needs, missing
most of what a __mdt field actually supports (Date, DateTime, Email,
LongTextArea, Percent, Phone, TextArea, URL, Metadata Relationship) while
including two (Lookup, MasterDetail) that don't apply to a __mdt the same
way. A MetadataRelationship stands in for Lookup here - it can only ever
target another __mdt type, never an arbitrary object.

Unlike CustomObject/CustomField, a MetadataType's fields are embedded
directly in the one IR rather than split into a separate step - a __mdt is
typically described and created as one unit ("a Feature_Flag__mdt with an
Enabled__c checkbox"), the same reasoning ir_lwc.py's LightningComponent
embeds js/html/css as one artifact instead of three.

CustomMetadataRecord is the third model here: one row of an existing or
just-created __mdt type. Its `values` are always plain strings from the
model - the record's real Salesforce-side type (xsi:type on <value>, per
xsdmlgen_mdt.py) is worked out at render time from the MetadataType this
record belongs to when that's known, not authored by the LLM (see
xmlgen_mdt.py's generate_mdt_record).
"""

from __future__ import annotations

from typing import Dict, List, Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

from .ir import _check_api_name

MetadataFieldType = Literal[
    "Text", "TextArea", "LongTextArea", "Number", "Percent", "Checkbox",
    "Date", "DateTime", "Email", "Phone", "URL", "Picklist", "MetadataRelationship",
]

Visibility = Literal["Public", "Protected", "PackageProtected"]


def _with_c_suffix(value: str) -> str:
    return value if value.endswith("__c") else f"{value}__c"


def _with_mdt_suffix(value: str) -> str:
    return value if value.endswith("__mdt") else f"{value}__mdt"


class MetadataField(BaseModel):
    api_name: str = Field(description="Suffixed with __c automatically if missing")
    label: str
    type: MetadataFieldType
    description: Optional[str] = None
    required: bool = False

    # Text
    length: Optional[int] = Field(default=None, description="Text only, max 255")

    # Number / Percent
    precision: Optional[int] = Field(default=None, description="Number/Percent only: total digits")
    scale: Optional[int] = Field(default=None, description="Number/Percent only: digits after the point")

    # Picklist
    picklist_values: List[str] = Field(default_factory=list)

    # MetadataRelationship
    reference_to: Optional[str] = Field(
        default=None,
        description="MetadataRelationship only: the target __mdt type's api_name",
    )
    relationship_label: Optional[str] = None
    relationship_name: Optional[str] = None

    @field_validator("api_name")
    @classmethod
    def valid_and_suffixed(cls, v: str) -> str:
        _check_api_name(v.removesuffix("__c"), "mdt field api_name", max_length=40)
        return _with_c_suffix(v)

    @field_validator("reference_to")
    @classmethod
    def valid_reference(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        _check_api_name(v.removesuffix("__mdt"), "metadata relationship target", max_length=40)
        return _with_mdt_suffix(v)

    @model_validator(mode="after")
    def type_specific_shape(self) -> "MetadataField":
        if self.type == "Text" and self.length is None:
            self.length = 255
        if self.type != "Text" and self.length is not None:
            raise ValueError(f"mdt field {self.api_name!r}: length only applies to Text")

        if self.type in ("Number", "Percent") and self.precision is None:
            raise ValueError(f"mdt field {self.api_name!r}: {self.type} requires precision")
        if self.type not in ("Number", "Percent") and (self.precision is not None or self.scale is not None):
            raise ValueError(
                f"mdt field {self.api_name!r}: precision/scale only apply to Number/Percent"
            )

        if self.type == "Picklist" and not self.picklist_values:
            raise ValueError(f"mdt field {self.api_name!r}: Picklist requires picklist_values")
        if self.type != "Picklist" and self.picklist_values:
            raise ValueError(
                f"mdt field {self.api_name!r}: picklist_values only applies to Picklist"
            )

        if self.type == "MetadataRelationship" and not self.reference_to:
            raise ValueError(
                f"mdt field {self.api_name!r}: MetadataRelationship requires reference_to"
            )
        if self.type != "MetadataRelationship" and self.reference_to:
            raise ValueError(
                f"mdt field {self.api_name!r}: reference_to only applies to MetadataRelationship"
            )
        return self


class MetadataType(BaseModel):
    api_name: str = Field(description="Suffixed with __mdt automatically if missing")
    label: str
    plural_label: str
    description: Optional[str] = None
    visibility: Visibility = "Public"
    deployment_status: Literal["Deployed", "InDevelopment"] = "Deployed"
    fields: List[MetadataField] = Field(min_length=1)

    @field_validator("api_name")
    @classmethod
    def valid_and_suffixed(cls, v: str) -> str:
        _check_api_name(v.removesuffix("__mdt"), "mdt api_name", max_length=40)
        return _with_mdt_suffix(v)


class CustomMetadataRecord(BaseModel):
    type_api_name: str = Field(description="The __mdt type this record belongs to")
    developer_name: str = Field(description="No suffix - the record's own DeveloperName")
    label: str
    protected: bool = False
    values: Dict[str, str] = Field(default_factory=dict)

    @field_validator("type_api_name")
    @classmethod
    def valid_type_reference(cls, v: str) -> str:
        _check_api_name(v.removesuffix("__mdt"), "mdt record type_api_name", max_length=40)
        return _with_mdt_suffix(v)

    @field_validator("developer_name")
    @classmethod
    def valid_developer_name(cls, v: str) -> str:
        return _check_api_name(v, "mdt record developer_name", max_length=40)
