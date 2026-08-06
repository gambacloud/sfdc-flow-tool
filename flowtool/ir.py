"""
Flow IR — the single source of truth.

Everything is derived from this model: the Mermaid graph the user approves,
the Markdown documentation, and the Flow XML that gets deployed. The LLM's only
job is to produce a valid instance of this model; it never writes XML.

Design rule that drives everything here: conditions and values are STRUCTURED,
never free-form strings. A condition is (left, operator, right) with a typed
right-hand side. This is what makes it impossible to emit garbage like
<leftValueReference>New Customer</leftValueReference>.
"""

from __future__ import annotations

import re
from typing import Annotated, Dict, List, Literal, Optional, Union

from pydantic import BaseModel, Field, field_validator, model_validator

# Salesforce API names: start with a letter, alphanumeric + underscore,
# no consecutive underscores, no trailing underscore.
API_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9]*(?:_[A-Za-z0-9]+)*$")

Operator = Literal[
    "EqualTo",
    "NotEqualTo",
    "GreaterThan",
    "GreaterThanOrEqualTo",
    "LessThan",
    "LessThanOrEqualTo",
    "StartsWith",
    "EndsWith",
    "Contains",
    "IsNull",
    "IsChanged",
    "WasSet",
]

DataType = Literal[
    "String", "Number", "Currency", "Boolean", "Date", "DateTime", "SObject", "Picklist"
]


class ApiName(str):
    """Marker type; validation happens in the models below."""


def _check_api_name(value: str, what: str) -> str:
    if not API_NAME_RE.match(value):
        raise ValueError(
            f"{what} {value!r} is not a valid Salesforce API name "
            "(letters/digits/underscores, must start with a letter, "
            "no consecutive or trailing underscores)"
        )
    return value


class Value(BaseModel):
    """
    A typed right-hand side. Exactly one field must be set.

    `element_reference` points at another element, variable or $Record field —
    it becomes <elementReference> rather than a literal.
    """

    string_value: Optional[str] = None
    number_value: Optional[float] = None
    boolean_value: Optional[bool] = None
    date_value: Optional[str] = None
    date_time_value: Optional[str] = None
    element_reference: Optional[str] = None

    @model_validator(mode="after")
    def exactly_one(self) -> "Value":
        set_fields = [k for k, v in self.model_dump().items() if v is not None]
        if len(set_fields) != 1:
            raise ValueError(
                f"Value must have exactly one field set, got {set_fields or 'none'}"
            )
        return self


class Condition(BaseModel):
    """One structured condition. Never a formula string."""

    left: str = Field(description="Field or variable reference, e.g. '$Record.Amount'")
    operator: Operator
    right: Optional[Value] = Field(
        default=None, description="Omitted for IsNull / IsChanged"
    )

    @model_validator(mode="after")
    def right_required_unless_unary(self) -> "Condition":
        unary = {"IsNull", "IsChanged", "WasSet"}
        if self.operator in unary:
            return self
        if self.right is None:
            raise ValueError(f"operator {self.operator} requires a right-hand value")
        return self


class Variable(BaseModel):
    name: str
    data_type: DataType
    is_collection: bool = False
    is_input: bool = False
    is_output: bool = False
    object_type: Optional[str] = Field(
        default=None, description="Required when data_type is SObject"
    )

    @field_validator("name")
    @classmethod
    def valid_name(cls, v: str) -> str:
        return _check_api_name(v, "variable name")

    @model_validator(mode="after")
    def sobject_needs_type(self) -> "Variable":
        if self.data_type == "SObject" and not self.object_type:
            raise ValueError(f"variable {self.name!r}: SObject requires object_type")
        return self


# --------------------------------------------------------------------------
# Elements
# --------------------------------------------------------------------------


class BaseElement(BaseModel):
    name: str = Field(description="API name — unique within the flow")
    label: str
    # An admin's own note on the element. Modelled rather than ignored: dropping
    # it would delete their documentation on the next deploy.
    description: Optional[str] = None
    next: Optional[str] = Field(
        default=None,
        description="Name of the next element. None means the path ends here — "
        "no <connector> is emitted. Flow XML has no 'End' element.",
    )

    @field_validator("name")
    @classmethod
    def valid_name(cls, v: str) -> str:
        return _check_api_name(v, "element name")


class FaultCapable(BaseElement):
    """
    Elements that can leave the flow's transaction, and so can fail. Salesforce
    allows a faultConnector on exactly these; Assignment, Decision and Loop
    cannot fail and have none.
    """

    fault_next: Optional[str] = Field(
        default=None, description="Where to go if this element fails."
    )


class AssignmentItem(BaseModel):
    to_reference: str
    operator: Literal["Assign", "Add", "Subtract", "AddItem"] = "Assign"
    value: Value


class Assignment(BaseElement):
    type: Literal["Assignment"] = "Assignment"
    items: List[AssignmentItem] = Field(min_length=1)


class Outcome(BaseModel):
    name: str
    label: str
    conditions: List[Condition] = Field(min_length=1)
    condition_logic: Literal["and", "or"] = "and"
    next: Optional[str] = None

    @field_validator("name")
    @classmethod
    def valid_name(cls, v: str) -> str:
        return _check_api_name(v, "outcome name")


class Decision(BaseElement):
    type: Literal["Decision"] = "Decision"
    outcomes: List[Outcome] = Field(min_length=1)
    default_outcome_label: str = "Default"
    # `next` on a Decision is the default connector target.


class RecordFilter(BaseModel):
    field: str
    operator: Operator
    value: Optional[Value] = None


class FieldValue(BaseModel):
    """
    One field assignment. A list rather than a mapping, because Flow XML models
    it that way (`<inputAssignments>` repeats) and because a mapping with
    arbitrary keys cannot be expressed in a constrained JSON schema.
    """

    field: str
    value: Value


class InputAssignment(BaseModel):
    """A value passed into a subflow. Keyed by variable name, not field name."""

    name: str
    value: Value


class GetRecords(FaultCapable):
    type: Literal["GetRecords"] = "GetRecords"
    object: str
    filters: List[RecordFilter] = Field(default_factory=list)
    filter_logic: Literal["and", "or"] = "and"
    first_record_only: bool = True
    store_output_automatically: bool = True
    sort_field: Optional[str] = None
    sort_order: Optional[Literal["Asc", "Desc"]] = None


class RecordCreate(FaultCapable):
    type: Literal["RecordCreate"] = "RecordCreate"
    # Required in field mode; omitted when creating from a variable, because
    # Salesforce takes the object from the variable and the XML carries no
    # <object> at all.
    object: Optional[str] = None
    fields: List[FieldValue] = Field(default_factory=list)
    # When set, creates from an existing sObject variable instead of field-by-field.
    input_reference: Optional[str] = None

    @model_validator(mode="after")
    def needs_fields_or_reference(self) -> "RecordCreate":
        if not self.fields and not self.input_reference:
            raise ValueError(
                f"{self.name}: RecordCreate needs either fields or input_reference"
            )
        if self.input_reference and (self.fields or self.object):
            raise ValueError(
                f"{self.name}: input_reference creates the record from a variable, "
                "which already carries its object and field values, so it cannot be "
                "combined with object or fields. Use one or the other."
            )
        if self.fields and not self.object:
            raise ValueError(
                f"{self.name}: RecordCreate with fields needs an object"
            )
        return self


class RecordUpdate(FaultCapable):
    type: Literal["RecordUpdate"] = "RecordUpdate"
    # Either update a record already in memory (input_reference, e.g. '$Record')
    # or find records by filter (object + filters).
    input_reference: Optional[str] = None
    object: Optional[str] = None
    filters: List[RecordFilter] = Field(default_factory=list)
    filter_logic: Literal["and", "or"] = "and"
    fields: List[FieldValue] = Field(default_factory=list)

    @model_validator(mode="after")
    def needs_target(self) -> "RecordUpdate":
        if not self.input_reference and not self.object:
            raise ValueError(
                f"{self.name}: RecordUpdate needs input_reference or object"
            )
        # Update Records has three legitimate shapes:
        #   1. input_reference alone      - update the record as it stands
        #   2. object + filters + fields  - find records by criteria, set values
        #   3. input_reference + fields   - take the ID from a record, set values
        # Only filters are exclusive with input_reference: they select records,
        # which is what the reference already did.
        #
        # Whether a particular reference may be written to is a separate
        # question - a Get Records output stored automatically is read-only -
        # but that depends on what the reference points at, so it cannot be
        # settled here. It lives in the model's instructions instead.
        if self.input_reference and self.filters:
            raise ValueError(
                f"{self.name}: input_reference already identifies the records to "
                "update, so it cannot be combined with filters. Use one or the "
                "other."
            )
        return self


class RecordDelete(FaultCapable):
    type: Literal["RecordDelete"] = "RecordDelete"
    input_reference: Optional[str] = None
    object: Optional[str] = None
    filters: List[RecordFilter] = Field(default_factory=list)

    @model_validator(mode="after")
    def needs_target(self) -> "RecordDelete":
        if not self.input_reference and not self.object:
            raise ValueError(
                f"{self.name}: RecordDelete needs input_reference or object"
            )
        return self


class Loop(BaseElement):
    type: Literal["Loop"] = "Loop"
    collection_reference: str
    iteration_order: Literal["Asc", "Desc"] = "Asc"
    first_element: Optional[str] = Field(
        default=None, description="First element inside the loop body"
    )
    # `next` is the no-more-values connector (what runs after the loop).


class ActionCall(FaultCapable):
    """
    Any invocable action: an email alert, Send Email, an Apex @InvocableMethod,
    Post to Chatter, Submit for Approval. They differ only by `action_type`.

    `action_type` is a plain string rather than a fixed list: Salesforce keeps
    adding types, and a closed list would refuse real flows that use one this
    build has not heard of.
    """

    type: Literal["ActionCall"] = "ActionCall"
    action_name: str = Field(
        description="The action's API name - the Email Alert's name, the Apex "
        "class's invocable name, and so on."
    )
    action_type: str = Field(
        description="e.g. emailAlert, emailSimple, apex, submit, chatterPost, "
        "quickAction."
    )
    input_parameters: List["InputAssignment"] = Field(default_factory=list)
    store_output_automatically: bool = False


class Subflow(FaultCapable):
    type: Literal["Subflow"] = "Subflow"
    flow_name: str
    input_assignments: List[InputAssignment] = Field(default_factory=list)


ScreenFieldType = Literal["DisplayText", "InputField", "LargeTextArea"]

# Screen inputs hold a value, and the value has a type. DisplayText shows text
# and holds nothing, which is why data_type is optional and checked below.
ScreenDataType = Literal["String", "Number", "Currency", "Date", "DateTime", "Boolean"]


class ScreenField(BaseModel):
    """
    One thing on a screen: a paragraph of text, or a box the user types into.

    The name is not local to the screen. Flow puts screen fields in the same
    namespace as elements and variables, so `{!Customer_Name}` anywhere later in
    the flow reads what was typed here. That is why it must be a valid API name
    and unique flow-wide, and it is what makes a screen input usable as the
    `left` of a condition or the value of an assignment.
    """

    name: str
    field_type: ScreenFieldType
    field_text: str = Field(
        description="For DisplayText, the text shown. For an input, its label."
    )
    data_type: Optional[ScreenDataType] = Field(
        default=None, description="Required for InputField; not carried by the others."
    )
    is_required: bool = False

    @field_validator("name")
    @classmethod
    def valid_name(cls, v: str) -> str:
        return _check_api_name(v, "screen field name")

    @model_validator(mode="after")
    def shape_matches_type(self) -> "ScreenField":
        if self.field_type == "InputField":
            if not self.data_type:
                raise ValueError(
                    f"screen field {self.name!r}: an InputField holds a value, so it "
                    "needs a data_type"
                )
        elif self.data_type:
            raise ValueError(
                f"screen field {self.name!r}: a {self.field_type} carries no "
                "data_type. Use field_type 'InputField' to collect a typed value."
            )
        if self.field_type == "DisplayText" and self.is_required:
            raise ValueError(
                f"screen field {self.name!r}: DisplayText shows text and collects "
                "nothing, so it cannot be required."
            )
        return self


class Screen(BaseElement):
    """
    A screen shown to a user.

    Only a screen flow can hold one. A record-triggered or autolaunched flow runs
    with nobody watching, and Salesforce rejects a screen in either — the check
    is on Flow, where the process type lives.

    Screens cannot fail the way a DML element can, so there is no fault path.
    """

    type: Literal["Screen"] = "Screen"
    fields: List[ScreenField] = Field(default_factory=list)
    # Runtime chrome. Modelled rather than assumed, because these are what the
    # user sees and dropping them would turn Back and Pause back on for an admin
    # who deliberately turned them off.
    allow_back: bool = True
    allow_finish: bool = True
    allow_pause: bool = True
    show_header: bool = True
    show_footer: bool = True


Element = Annotated[
    Union[
        ActionCall,
        Assignment,
        Decision,
        GetRecords,
        RecordCreate,
        RecordUpdate,
        RecordDelete,
        Loop,
        Screen,
        Subflow,
    ],
    Field(discriminator="type"),
]


# --------------------------------------------------------------------------
# Start + Flow
# --------------------------------------------------------------------------


class Start(BaseModel):
    """
    Record-triggered flows set object + trigger_type. Autolaunched flows leave
    them empty and are invoked by Apex or another flow.
    """

    next: Optional[str] = None
    object: Optional[str] = None
    record_trigger_type: Optional[
        Literal["Create", "Update", "CreateAndUpdate", "Delete"]
    ] = None
    trigger_type: Optional[
        Literal["RecordAfterSave", "RecordBeforeSave", "RecordBeforeDelete", "Scheduled"]
    ] = None
    filters: List[RecordFilter] = Field(default_factory=list)
    filter_logic: Literal["and", "or"] = "and"
    # "Only when a record is updated to meet the condition requirements".
    # Changes when the flow runs, so it cannot be dropped silently.
    only_when_changed_to_meet_criteria: bool = False

    @model_validator(mode="after")
    def trigger_consistency(self) -> "Start":
        if self.object and not self.trigger_type:
            raise ValueError("a record-triggered start requires trigger_type")
        if self.trigger_type and self.trigger_type != "Scheduled" and not self.object:
            raise ValueError(f"trigger_type {self.trigger_type} requires an object")
        if self.object and not self.record_trigger_type:
            raise ValueError("a record-triggered start requires record_trigger_type")
        return self


class Flow(BaseModel):
    api_name: str
    label: str
    description: Optional[str] = None
    api_version: str = "62.0"
    # "Flow" is Salesforce's name for a screen flow — the one a user runs and
    # watches. "AutoLaunchedFlow" covers both record-triggered and autolaunched.
    process_type: Literal["AutoLaunchedFlow", "Flow"] = "AutoLaunchedFlow"
    status: Literal["Draft", "Active"] = "Draft"
    start: Start
    elements: List[Element] = Field(default_factory=list)
    variables: List[Variable] = Field(default_factory=list)

    @field_validator("api_name")
    @classmethod
    def valid_name(cls, v: str) -> str:
        return _check_api_name(v, "flow api_name")

    @property
    def interview_label(self) -> str:
        return f"{self.label} {{!$Flow.CurrentDateTime}}"

    def by_name(self) -> Dict[str, Element]:
        return {e.name: e for e in self.elements}

    @staticmethod
    def successors(element: Element) -> List[str]:
        """Every element this one can hand control to."""
        targets: List[str] = []
        if isinstance(element, Decision):
            targets.extend(oc.next for oc in element.outcomes if oc.next)
        if isinstance(element, Loop) and element.first_element:
            targets.append(element.first_element)
        fault = getattr(element, "fault_next", None)
        if fault:
            targets.append(fault)
        if element.next:
            targets.append(element.next)
        return targets

    def connector_map(self) -> str:
        """
        Every connector, as text. Goes into the unreachable-element error so the
        reader - usually the model repairing its own output - can see where the
        chain actually breaks instead of guessing which link is missing.
        """
        lines = [f"  start -> {self.start.next or '(nothing)'}"]
        for element in self.elements:
            if isinstance(element, Decision):
                for outcome in element.outcomes:
                    lines.append(
                        f"  {element.name} outcome {outcome.name} -> "
                        f"{outcome.next or '(ends)'}"
                    )
                lines.append(f"  {element.name} default -> {element.next or '(ends)'}")
            elif isinstance(element, Loop):
                lines.append(
                    f"  {element.name} each -> {element.first_element or '(nothing)'}"
                )
                lines.append(f"  {element.name} done -> {element.next or '(ends)'}")
            else:
                lines.append(f"  {element.name} -> {element.next or '(ends)'}")
            fault = getattr(element, "fault_next", None)
            if fault:
                lines.append(f"  {element.name} on fault -> {fault}")
        return "\n".join(lines)

    def reachable(self) -> set:
        """Names reachable from the start connector."""
        by_name = self.by_name()
        seen: set = set()
        queue = [self.start.next] if self.start.next else []
        while queue:
            name = queue.pop()
            if name in seen or name not in by_name:
                continue
            seen.add(name)
            queue.extend(self.successors(by_name[name]))
        return seen

    @model_validator(mode="after")
    def references_resolve(self) -> "Flow":
        """
        Every connector target must name an element that exists. This is the
        check that prevents the dangling <targetReference>End_1</targetReference>
        class of bug — an ended path is `next: None`, not a reference to nothing.
        """
        known = {e.name for e in self.elements}
        if len(known) != len(self.elements):
            dupes = [e.name for e in self.elements]
            seen, repeated = set(), set()
            for n in dupes:
                if n in seen:
                    repeated.add(n)
                seen.add(n)
            raise ValueError(f"duplicate element names: {sorted(repeated)}")

        problems: List[str] = []

        def check(target: Optional[str], where: str) -> None:
            if target is not None and target not in known:
                problems.append(f"{where} points at unknown element {target!r}")

        check(self.start.next, "start")
        for el in self.elements:
            check(el.next, f"{el.name}.next")
            if isinstance(el, Decision):
                for oc in el.outcomes:
                    check(oc.next, f"{el.name}.{oc.name}.next")
            if isinstance(el, Loop):
                check(el.first_element, f"{el.name}.first_element")
            check(getattr(el, "fault_next", None), f"{el.name}.fault_next")

        if problems:
            raise ValueError("unresolved references: " + "; ".join(problems))
        return self

    @model_validator(mode="after")
    def everything_is_connected(self) -> "Flow":
        """
        Salesforce rejects a flow whose Start goes nowhere ("The flow can't run
        because nothing is connected to the Start element"), and an element no
        path reaches is dead weight that usually means a missing connector.
        Both are cheaper to catch here than in a deploy.

        Runs after references_resolve, so every name is already known to exist.
        """
        if self.elements and not self.start.next:
            raise ValueError(
                "start.next is empty, so nothing is connected to the Start element "
                "and the flow cannot run. Set start.next to the name of the first "
                "element."
            )

        reachable = self.reachable()
        orphans = {e.name for e in self.elements} - reachable
        if orphans:
            # Naming the orphans says what is stranded but not where the missing
            # link starts, and a repair that cannot find the loose end just
            # re-emits the same flow. The loose end is always a reachable
            # element whose path stops, so name those too.
            by_name = self.by_name()
            dead_ends = [
                name for name in sorted(reachable) if not self.successors(by_name[name])
            ]
            hint = (
                f"\nPaths that currently stop: {dead_ends}. The connector you are "
                "missing almost certainly belongs to one of these - set its `next` "
                "to whatever should run after it."
                if dead_ends
                else ""
            )
            raise ValueError(
                f"unreachable elements: {sorted(orphans)}. No path from Start "
                "reaches them. Here is every connector as it stands:\n"
                f"{self.connector_map()}\n"
                "Set the connector that should lead to each unreachable element. "
                "For a Decision, that is the outcome's own `next` - an outcome "
                "with next=null ends the path instead of continuing to the "
                f"element you meant.{hint}"
            )
        return self

    @model_validator(mode="after")
    def screens_belong_to_a_screen_flow(self) -> "Flow":
        """
        Salesforce will not run a screen where nobody is watching, and it will
        not start a screen flow from a record change. Both are rejected at
        deploy; both are cheaper to catch here.
        """
        screens = sorted(e.name for e in self.elements if isinstance(e, Screen))
        if screens and self.process_type != "Flow":
            raise ValueError(
                f"screens {screens} need process_type 'Flow'. A "
                f"{self.process_type} runs in the background with no user to show "
                "them to, so Salesforce refuses a screen in one. Either set "
                "process_type to 'Flow' and drop the record trigger, or build the "
                "logic without screens."
            )
        if self.process_type == "Flow" and self.start.object:
            raise ValueError(
                "a screen flow is launched by a user, not by a record change, so "
                f"start.object ({self.start.object!r}), record_trigger_type and "
                "trigger_type must all be empty. To react to a record change, use "
                "process_type 'AutoLaunchedFlow' - but note that such a flow "
                "cannot contain screens."
            )
        return self

    @model_validator(mode="after")
    def names_are_unique(self) -> "Flow":
        """
        Elements, variables and screen fields share one namespace: `{!Total}` can
        only mean one thing, so a screen input named after a variable makes the
        reference ambiguous and Salesforce rejects the flow.

        Duplicate element names are caught earlier with a more specific message;
        this is the check that spans the three kinds.
        """
        owners: Dict[str, List[str]] = {}

        def claim(name: str, owner: str) -> None:
            owners.setdefault(name, []).append(owner)

        for element in self.elements:
            claim(element.name, "an element")
            if isinstance(element, Screen):
                for screen_field in element.fields:
                    claim(screen_field.name, f"a field on screen {element.name}")
        for variable in self.variables:
            claim(variable.name, "a variable")

        clashes = {
            name: kinds for name, kinds in owners.items() if len(kinds) > 1
        }
        if clashes:
            detail = "; ".join(
                f"{name!r} is {' and '.join(kinds)}" for name, kinds in sorted(clashes.items())
            )
            raise ValueError(
                "elements, variables and screen fields share one namespace, so "
                f"each name can be used once: {detail}. Rename one of them."
            )
        return self
