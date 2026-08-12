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


# --------------------------------------------------------------------------
# Custom condition logic
# --------------------------------------------------------------------------
#
# A decision outcome, or a set of record filters, combines its conditions with
# `and`, with `or`, or with an expression over their positions: "1 OR (2 AND 3)".
# Conditions are numbered from 1, in the order they are listed.
#
# Nothing outside this file checks any of it. Salesforce accepts an expression
# that names a condition past the end of the list, an expression with unbalanced
# brackets, and the literal string "banana" - all pass checkOnly and deploy.
# That makes this the second free-form string in the IR, alongside a formula
# expression, and the only one of the two that can be checked at all: the
# numbers have to line up with conditions that exist.
#
# The risk runs the other way too. Editing a flow renumbers its conditions, so
# an outcome that drops its second condition leaves "1 OR (2 AND 3)" pointing
# one place past the end. That is the case this catches most often.

_LOGIC_TOKEN = re.compile(r"\s*(\(|\)|\d+|[A-Za-z]+|\S)")


def _tokenise_logic(expression: str) -> List[str]:
    tokens, position = [], 0
    while position < len(expression):
        match = _LOGIC_TOKEN.match(expression, position)
        if not match:
            break
        tokens.append(match.group(1))
        position = match.end()
    return tokens


def referenced_conditions(expression: str) -> set:
    """
    The condition numbers a custom logic expression uses.

    Raises ValueError describing what is wrong, in terms of the expression
    rather than of the parser: the message is read by whoever wrote the flow.

        expr   := term (OR term)*
        term   := factor (AND factor)*
        factor := NOT factor | '(' expr ')' | number
    """
    tokens = _tokenise_logic(expression)
    if not tokens:
        raise ValueError("it is empty")

    used: set = set()
    position = 0

    def peek() -> Optional[str]:
        return tokens[position] if position < len(tokens) else None

    def take() -> str:
        nonlocal position
        token = tokens[position]
        position += 1
        return token

    def factor() -> None:
        token = peek()
        if token is None:
            raise ValueError("it stops where a condition number was expected")
        if token.upper() == "NOT":
            take()
            factor()
            return
        if token == "(":
            take()
            expr()
            if peek() != ")":
                raise ValueError("a '(' is never closed")
            take()
            return
        if token.isdigit():
            take()
            used.add(int(token))
            return
        raise ValueError(
            f"{token!r} is not a condition number, AND, OR, NOT or a bracket"
        )

    def term() -> None:
        factor()
        while peek() is not None and peek().upper() == "AND":
            take()
            factor()

    def expr() -> None:
        term()
        while peek() is not None and peek().upper() == "OR":
            take()
            term()

    expr()
    if position < len(tokens):
        leftover = tokens[position]
        if leftover == ")":
            raise ValueError("there is a ')' with no '(' to match it")
        raise ValueError(
            f"{leftover!r} is left over at the end - two conditions need an "
            "AND or an OR between them"
        )
    return used


_FILTER_LOGIC_HELP = (
    "'and' when every filter must match, 'or' when any one will do, or an "
    "expression over the filter numbers such as '1 OR (2 AND 3)'. Filters are "
    "numbered from 1, in order."
)


def _check_logic(logic: str, count: int, where: str, field: str, item: str) -> None:
    """
    Validate one logic string against the conditions it combines.

    A condition the expression never mentions is left alone. It is evaluated and
    ignored, which is odd but not broken, and refusing it would be guessing at
    intent rather than following a rule. The approval document points it out
    instead, so a person decides.
    """
    if logic.lower() in ("and", "or"):
        return
    try:
        used = referenced_conditions(logic)
    except ValueError as problem:
        raise ValueError(
            f"{where}: {field} {logic!r} cannot be read - {problem}. Use 'and', "
            f"'or', or an expression over the {item} numbers such as "
            "'1 OR (2 AND 3)'."
        ) from None

    out_of_range = sorted(n for n in used if n < 1 or n > count)
    if out_of_range:
        listed = ", ".join(str(n) for n in out_of_range)
        raise ValueError(
            f"{where}: {field} {logic!r} refers to {item} {listed}, but there "
            f"{'is' if count == 1 else 'are'} only {count}. {item.capitalize()}s "
            "are numbered from 1 in the order they are listed. Salesforce "
            "deploys this without complaint and then evaluates it wrongly, so "
            "the numbers have to be right here."
        )


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
    # The admin's own note. Modelled rather than ignored for the same reason an
    # element's is: dropping it would delete their documentation on redeploy.
    description: Optional[str] = None
    # Decimal places. Salesforce writes it on Number and Currency; it is left
    # unconstrained here because only Number was ever observed, and guessing
    # which other types may carry it would refuse flows that already deploy.
    scale: Optional[int] = None
    # What the variable holds before anything assigns to it. Real defaults are
    # not only literals - a DateTime commonly defaults to an element reference
    # such as $Flow.CurrentDateTime - so this is a full Value.
    value: Optional[Value] = None

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


# Salesforce's FlowAssignmentOperator, as the org accepts it. Widening this to
# the real enum is safe in a way that widening a free-form string is not: the
# org checks it and names a wrong one - "'Banana' is not a valid value for the
# enum 'FlowAssignmentOperator'" - so a mistake fails validation rather than
# deploying. AssignCount was found in two live flows and refused by the four
# this used to allow.
AssignmentOperator = Literal[
    "Assign",           # set it
    "Add",              # numbers, dates, and string concatenation
    "Subtract",
    "AssignCount",      # set a number to how many items a collection holds
    "AddItem",          # append to a collection
    "AddAtStart",
    "RemoveFirst",
    "RemoveBeforeFirst",
    "RemoveAfterFirst",
    "RemovePosition",
    "RemoveAll",
    "RemoveUncommon",   # keep only what both collections have
]


class AssignmentItem(BaseModel):
    to_reference: str
    operator: AssignmentOperator = "Assign"
    value: Value


class Assignment(BaseElement):
    type: Literal["Assignment"] = "Assignment"
    items: List[AssignmentItem] = Field(min_length=1)


class Outcome(BaseModel):
    name: str
    label: str
    conditions: List[Condition] = Field(min_length=1)
    condition_logic: str = Field(
        default="and",
        description="'and' when every condition must hold, 'or' when any one "
        "will do, or an expression over the condition numbers such as "
        "'1 OR (2 AND 3)'. Conditions are numbered from 1, in order.",
    )
    next: Optional[str] = None

    @field_validator("name")
    @classmethod
    def valid_name(cls, v: str) -> str:
        return _check_api_name(v, "outcome name")

    @model_validator(mode="after")
    def logic_matches_conditions(self) -> "Outcome":
        _check_logic(
            self.condition_logic, len(self.conditions),
            f"outcome {self.name!r}", "condition_logic", "condition",
        )
        return self


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
    filter_logic: str = Field(default="and", description=_FILTER_LOGIC_HELP)
    # None means the flow never said. Older flows omit it and take their answer
    # from the variable they store into, so writing a value back would decide
    # something the flow left open - and "true" would turn a query over many
    # records into one over the first.
    first_record_only: Optional[bool] = True
    store_output_automatically: bool = True
    # Manual storage: the records go into this variable instead of into the
    # element's own output. The alternative to automatic storage, not an
    # addition to it.
    output_reference: Optional[str] = None
    sort_field: Optional[str] = None
    sort_order: Optional[Literal["Asc", "Desc"]] = None
    # Which fields to fetch. Empty means all of them, which is what Flow
    # Builder does by default. Naming them is a real choice an admin makes -
    # it is what the query actually asks for - and it sits alongside automatic
    # storage rather than replacing it.
    queried_fields: List[str] = Field(default_factory=list)
    # The third way of handing the records back: one variable per field.
    output_assignments: List[OutputAssignment] = Field(
        default_factory=list,
        description="Put individual fields into variables, instead of keeping "
        "the whole record. The oldest of the three ways, and exclusive with "
        "both the others.",
    )
    # "When no records are found, set the variables to null." Modelled rather
    # than assumed: the compiler used to write `false` unconditionally, so a
    # flow that said true came back saying false - a behaviour change, silently,
    # on the one flag that decides what a failed lookup leaves behind.
    assign_null_values_if_no_records_found: bool = False

    @model_validator(mode="after")
    def logic_matches_filters(self) -> "GetRecords":
        _check_logic(self.filter_logic, len(self.filters),
                     self.name, "filter_logic", "filter")
        return self

    @model_validator(mode="after")
    def one_way_to_store_the_records(self) -> "GetRecords":
        """
        There are three, and they are answers to the same question:

          - automatic storage keeps the records in this element's own output,
            read as `{!Get_Account.Name}`
          - `output_reference` puts the whole record in a variable
          - `output_assignments` puts individual fields into variables

        A flag nobody set follows the shape - as on Create Records, and for the
        same reason. The org states both of the pairs it rejects, and they are
        quoted here because the wording is what someone will search for.
        """
        if self.output_reference and self.output_assignments:
            raise ValueError(
                f"{self.name}: \"You can't use the sObjectOutputReference field "
                "with the outputAssignments field.\" Either put the whole record "
                "in a variable, or assign its fields one at a time."
            )
        if self.output_reference or self.output_assignments:
            if "store_output_automatically" not in self.model_fields_set:
                self.store_output_automatically = False
            elif self.store_output_automatically:
                if self.output_assignments:
                    raise ValueError(
                        f"{self.name}: \"You can't use the outputAssignments "
                        "field with the storeOutputAutomatically field.\" Either "
                        "assign the fields you want, or keep the record and read "
                        f"its fields as {{!{self.name}.FieldName}}."
                    )
                raise ValueError(
                    f"{self.name}: output_reference stores the records in a "
                    "variable, which is what store_output_automatically would "
                    "otherwise do here. Use one or the other."
                )
        return self


class OutputAssignment(BaseModel):
    """
    One field of a retrieved record, put into a variable of its own.

    The oldest of the three ways Get Records can hand back what it found, and
    the most explicit: instead of keeping the record and reading
    `{!Get_Account.Name}`, each field is assigned somewhere by name.

    Salesforce checks neither half. A `field` that does not exist on the object
    deploys - knowing better would need the object's schema, which this tool
    does not read - and so does an `assign_to_reference` naming a variable the
    flow never defines, which is checked on Flow because it can be.
    """

    field: str = Field(description="The field on the retrieved record.")
    assign_to_reference: str = Field(description="The variable it goes into.")


class RecordCreate(FaultCapable):
    type: Literal["RecordCreate"] = "RecordCreate"
    # Required in field mode; omitted when creating from a variable, because
    # Salesforce takes the object from the variable and the XML carries no
    # <object> at all.
    object: Optional[str] = None
    fields: List[FieldValue] = Field(default_factory=list)
    # When set, creates from an existing sObject variable instead of field-by-field.
    input_reference: Optional[str] = None
    # The two ways to get at the record just created. Automatic storage puts the
    # whole record in the element's own output; this puts only its Id into a
    # variable you name. Real flows use one or the other - they never appear
    # together - so the validator below keeps them apart.
    assign_record_id_to_reference: Optional[str] = None
    store_output_automatically: bool = True

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

    @model_validator(mode="after")
    def one_way_to_return_the_record(self) -> "RecordCreate":
        """
        There are two ways to get at the record just created, and they cannot be
        combined. Creating from a variable allows neither: the record is already
        in hand. All three rules come from the org rejecting the combination,
        quoted below.

        A flag nobody set is not a decision, so an unset one follows the shape
        rather than making every caller restate what the shape already implies.
        """
        chose_a_shape = self.input_reference or self.assign_record_id_to_reference
        if chose_a_shape:
            if "store_output_automatically" not in self.model_fields_set:
                self.store_output_automatically = False
            elif self.store_output_automatically:
                culprit = (
                    "sObjectInputReference" if self.input_reference
                    else "assignReturnIdToReference"
                )
                raise ValueError(
                    f"{self.name}: \"You can't use the storeOutputAutomatically "
                    f"field with the {culprit} field.\" Automatic storage puts "
                    "the whole new record in this element's output; the other "
                    "shapes replace it. Pick one."
                )
        if self.input_reference and self.assign_record_id_to_reference:
            raise ValueError(
                f"{self.name}: \"You can't use the sObjectInputReference field "
                "with the assignReturnIdToReference field.\" A record created "
                "from a variable is already in that variable, Id included."
            )
        return self


class RecordUpdate(FaultCapable):
    type: Literal["RecordUpdate"] = "RecordUpdate"
    # Either update a record already in memory (input_reference, e.g. '$Record')
    # or find records by filter (object + filters).
    input_reference: Optional[str] = None
    object: Optional[str] = None
    filters: List[RecordFilter] = Field(default_factory=list)
    filter_logic: str = Field(default="and", description=_FILTER_LOGIC_HELP)
    fields: List[FieldValue] = Field(default_factory=list)

    @model_validator(mode="after")
    def logic_matches_filters(self) -> "RecordUpdate":
        _check_logic(self.filter_logic, len(self.filters),
                     self.name, "filter_logic", "filter")
        return self

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
    # The older way of naming the current item. Without it the item is read as
    # the loop's own name; with it, it lands in a variable you declared - which
    # is still the only way to change the item inside the body and collect the
    # result. The org checks that the variable exists and that its type matches
    # the collection, so this carries no rule of its own beyond existing.
    assign_next_value_to_reference: Optional[str] = Field(
        default=None,
        description="A variable to hold the current item. Leave empty to read "
        "it as the loop's own name instead.",
    )
    # `next` is the no-more-values connector (what runs after the loop).


class CollectionFilter(BaseElement):
    """
    Keeps only the items of a collection that match conditions - the same shape
    as a Get Records filter, but over a collection already in memory instead of
    a query.

    Evaluating a condition needs a name for "the item being tested right now",
    the same way a Loop needs one for the current item - `current_item` is that
    placeholder, read from `conditions` as e.g. `{name}.Status`. The result is
    this element's own output, read as `{!ElementName}`, the same as automatic
    storage everywhere else in this IR.
    """

    type: Literal["CollectionFilter"] = "CollectionFilter"
    collection_reference: str
    current_item: str = Field(
        description="A variable to hold the item while a condition is being "
        "tested. Referenced from conditions as e.g. '{name}.Field'."
    )
    conditions: List[Condition] = Field(min_length=1)
    condition_logic: str = Field(default="and", description=_FILTER_LOGIC_HELP)

    @model_validator(mode="after")
    def logic_matches_conditions(self) -> "CollectionFilter":
        _check_logic(self.condition_logic, len(self.conditions),
                     f"collection filter {self.name!r}", "condition_logic",
                     "condition")
        return self


class CollectionSortOption(BaseModel):
    sort_field: str
    sort_order: Literal["Asc", "Desc"] = "Asc"
    # Where a blank or missing value sorts to. Salesforce writes this alongside
    # every sort option it has ever been seen to emit, so it is modelled rather
    # than assumed - a round trip that dropped it would change where empty
    # values land without anyone asking for that.
    does_put_empty_string_and_null_first: bool = False


class CollectionSort(BaseElement):
    """Reorders a collection already in memory. The result is this element's
    own output, read as `{!ElementName}`."""

    type: Literal["CollectionSort"] = "CollectionSort"
    collection_reference: str
    sort_options: List[CollectionSortOption] = Field(min_length=1)


class DataTypeMapping(BaseModel):
    """
    One generic Apex type parameter, pinned to a concrete type for this call.

    Some invocable actions and components are written against a generic type -
    `T__inputRecord` rather than a fixed SObject - so the same action can run
    against any object. `type_name` is the generic parameter as the action
    declares it; `type_value` is what it means here, almost always an SObject
    API name.
    """

    type_name: str = Field(
        description="The generic type parameter's name, e.g. 'T__inputRecord'."
    )
    type_value: str = Field(
        description="The concrete type it stands for in this call, e.g. 'Account'."
    )


class ComponentOutput(BaseModel):
    """
    One value a component or action hands back, and the variable it lands in.

    The mirror image of an input: an input is named on the component and carries
    a value in, an output is named on the component and names a flow resource to
    write out to. Both names belong to the component's own signature, and the org
    checks them - "We can't find this output attribute" - so a wrong one fails
    validation rather than deploying and silently doing nothing.
    """

    name: str = Field(
        description="The property the component exposes, e.g. 'value'."
    )
    assign_to_reference: str = Field(
        description="The variable to store it in."
    )


class ActionCall(FaultCapable):
    """
    Any invocable action: an email alert, Send Email, an Apex @InvocableMethod,
    Post to Chatter, Submit for Approval. They differ only by `action_type`.

    `action_type` is a plain string rather than a fixed list: Salesforce keeps
    adding types, and a closed list would refuse real flows that use one this
    build has not heard of.
    """

    type: Literal["ActionCall"] = "ActionCall"
    data_type_mappings: List[DataTypeMapping] = Field(
        default_factory=list,
        description="Pins a generic Apex-typed action to concrete types, for "
        "actions written against T__ style type parameters.",
    )
    # Whether the action runs inside the flow's transaction or gets its own.
    # Which values a given action allows is the action's business, and the org
    # says so by name - "The action 'EMAILSIMPLE' only supports
    # 'CurrentTransaction'" - so this models the enum and leaves the rest there.
    flow_transaction_model: Optional[
        Literal["CurrentTransaction", "NewTransaction", "Automatic"]
    ] = Field(
        default=None,
        description="'NewTransaction' for work that cannot run inside the "
        "current one, such as a callout after a record was saved.",
    )
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
    # The older alternative to automatic storage: each output assigned to a
    # variable by name instead of all of them kept under the element's own
    # name. Mirrors the same choice on a screen component. Never both.
    output_parameters: List[ComponentOutput] = Field(default_factory=list)
    # For an action that runs asynchronously (an external service, a long-running
    # Apex action): wait for it to finish rather than continuing immediately.
    # Confirmed against the org's own live Metadata API schema
    # (describeValueType on FlowActionCall) rather than a public sample - this
    # combination is real but was not found in any example flow.
    is_wait_until_completed: bool = False
    # How long to wait before giving up and following timeout_next instead.
    # Same shape as a scheduled path's offset: both or neither.
    timeout_offset: Optional[int] = None
    timeout_offset_unit: Optional[
        Literal["Minutes", "Hours", "Days", "Weeks", "Months"]
    ] = None
    timeout_next: Optional[str] = Field(
        default=None,
        description="Where to go if the action does not finish within the "
        "timeout. Only meaningful alongside is_wait_until_completed.",
    )

    @model_validator(mode="after")
    def one_way_to_get_the_outputs(self) -> "ActionCall":
        if self.output_parameters and self.store_output_automatically:
            raise ValueError(
                f"{self.name}: \"You can't use the storeOutputAutomatically "
                "field with the outputParameters field.\" Either assign each "
                "output to a variable, or set store_output_automatically and "
                f"read them as {{!{self.name}.outputName}}."
            )
        return self

    @model_validator(mode="after")
    def timeout_needs_both_halves(self) -> "ActionCall":
        if (self.timeout_offset is None) != (self.timeout_offset_unit is None):
            missing = "timeout_offset_unit" if self.timeout_offset_unit is None else "timeout_offset"
            raise ValueError(
                f"{self.name}: a timeout needs both timeout_offset and "
                f"timeout_offset_unit, and {missing} is missing."
            )
        return self


class SubflowOutputAssignment(BaseModel):
    """One of the called flow's own output variables, put into a variable here."""

    name: str = Field(description="The output variable's name in the called flow.")
    assign_to_reference: str = Field(description="The variable in this flow it goes into.")


class Subflow(FaultCapable):
    type: Literal["Subflow"] = "Subflow"
    flow_name: str
    input_assignments: List[InputAssignment] = Field(default_factory=list)
    # The two ways to read back what the called flow produced. Manual
    # assignment names each output one at a time; automatic storage keeps them
    # all under this element's own name, read as {!Subflow_Element.OutputVar}.
    # Mirrors the same choice on Get Records and Create Records.
    output_assignments: List[SubflowOutputAssignment] = Field(default_factory=list)
    store_output_automatically: bool = False


ScreenFieldType = Literal[
    "DisplayText",
    "InputField",
    "LargeTextArea",
    "RadioButtons",
    "DropdownBox",
    "MultiSelectCheckboxes",
    "MultiSelectPicklist",
    "ComponentInstance",
    # A standard component that also picks from a list of options - the
    # "Choice Lookup" component (flowruntime:choiceLookup), which is a
    # component in every way except that it takes choice_references too.
    "ComponentChoice",
    # Layout rather than content. A RegionContainer is a section and holds
    # Regions; a Region is a column and holds ordinary fields. Exactly two
    # levels: the org refuses a container inside a region by name.
    "RegionContainer",
    "Region",
]

# The two layout types, which hold other fields instead of holding a value.
LAYOUT_FIELD_TYPES = frozenset({"RegionContainer", "Region"})

# Screen inputs hold a value, and the value has a type. DisplayText shows text
# and holds nothing, which is why data_type is optional and checked below.
#
# Picklist and Multipicklist are deliberately absent. They are types a *choice
# set* has, not types a screen field has: the org rejects both outright here,
# including on a multi-select, which stores its several answers as a String.
ScreenDataType = Literal[
    "String", "Number", "Currency", "Date", "DateTime", "Boolean",
]

# The field types that present a list of options rather than a free-text box.
# Each one needs at least one choice to show, and nothing else may carry choices
# - except ComponentChoice, which may but does not have to: see
# _OPTIONALLY_CHOICE_FIELD_TYPES below.
CHOICE_FIELD_TYPES = frozenset(
    {"RadioButtons", "DropdownBox", "MultiSelectCheckboxes", "MultiSelectPicklist"}
)

# ComponentChoice (the "Choice Lookup" standard component) offers options built
# from a Choice or DynamicChoiceSet the same way RadioButtons etc. do, but it is
# also seen with none at all - other standard lookup components share the same
# fieldType without offering a fixed list. So choice_references is permitted
# here without being required, unlike the plain choice field types above.
_OPTIONALLY_CHOICE_FIELD_TYPES = frozenset({"ComponentChoice"})

# Types that hold no value at all.
_NO_VALUE_FIELD_TYPES = frozenset({"DisplayText", "LargeTextArea"})

# ComponentInstance is neither. A component declares its own inputs and outputs,
# so the field needs no data_type - but the org accepts one, and refusing what
# the org accepts would turn a deployable flow into an unopenable one. So it is
# optional here: not required, not forbidden.
# Neither required nor forbidden. A component declares its own types; a
# section or a column holds no value at all, and the org takes a dataType on
# any of the three without complaint.
_OPTIONAL_DATA_TYPE_FIELD_TYPES = frozenset(
    {"ComponentInstance", "ComponentChoice", "RegionContainer", "Region"}
)


class Choice(BaseModel):
    """
    One fixed option, defined once and referenced by name from any number of
    screen fields. It is a resource, not an element - nothing connects to it.
    """

    name: str
    choice_text: str = Field(description="What the user sees for this option.")
    data_type: Literal["String", "Number", "Currency", "Date", "DateTime", "Boolean"] = (
        "String"
    )
    value: Optional[Value] = Field(
        default=None,
        description="What selecting it stores. Defaults to the text shown.",
    )

    @field_validator("name")
    @classmethod
    def valid_name(cls, v: str) -> str:
        return _check_api_name(v, "choice name")


class DynamicChoiceSet(BaseModel):
    """
    Options built when the flow runs, in one of three ways: a live query
    against an object, a collection already in memory, or a picklist field's
    values. Exactly one - a choice set drawn from records has no picklist to
    read, and a fixed collection needs no filters of its own to query with.
    """

    name: str
    data_type: Literal[
        "String", "Number", "Currency", "Date", "DateTime", "Boolean",
        "Picklist", "Multipicklist",
    ] = "String"

    # Query mode: one option per matching record, queried live.
    object: Optional[str] = None
    filters: List["RecordFilter"] = Field(default_factory=list)
    filter_logic: str = Field(default="and", description=_FILTER_LOGIC_HELP)
    sort_field: Optional[str] = None
    sort_order: Optional[Literal["Asc", "Desc"]] = None
    limit: Optional[int] = None

    # Shared by query mode and collection mode: which field of each record is
    # shown, and which is stored.
    display_field: Optional[str] = Field(
        default=None, description="Record field shown to the user, e.g. 'Name'."
    )
    value_field: Optional[str] = Field(
        default=None, description="Record field stored, e.g. 'Id'."
    )

    # Collection mode: one option per record already in memory, in this
    # collection variable, instead of a live query. Confirmed against the
    # org's own live Metadata API schema (describeValueType on
    # FlowDynamicChoiceSet) - real, but not found in any public sample flow.
    collection_reference: Optional[str] = Field(
        default=None,
        description="A collection variable to build options from, instead of "
        "querying an object. Still needs display_field and value_field.",
    )

    # Picklist mode: one option per value defined on a picklist field.
    picklist_object: Optional[str] = None
    picklist_field: Optional[str] = None

    @field_validator("name")
    @classmethod
    def valid_name(cls, v: str) -> str:
        return _check_api_name(v, "choice set name")

    @model_validator(mode="after")
    def logic_matches_filters(self) -> "DynamicChoiceSet":
        _check_logic(self.filter_logic, len(self.filters),
                     f"choice set {self.name!r}", "filter_logic", "filter")
        return self

    @model_validator(mode="after")
    def one_mode_or_the_other(self) -> "DynamicChoiceSet":
        query_mode = any(
            (self.object, self.filters, self.sort_field, self.limit)
        )
        collection_mode = bool(self.collection_reference)
        picklist_mode = any((self.picklist_object, self.picklist_field))

        if sum((query_mode, collection_mode, picklist_mode)) > 1:
            raise ValueError(
                f"choice set {self.name!r}: options come from exactly one of a "
                "live query (object + filters/sort_field/limit), a collection "
                "already in memory (collection_reference), or a picklist "
                "(picklist_object + picklist_field) - not more than one."
            )
        if picklist_mode:
            if not (self.picklist_object and self.picklist_field):
                raise ValueError(
                    f"choice set {self.name!r}: a picklist choice set needs both "
                    "picklist_object and picklist_field."
                )
            # The org rejects any other type here outright: "The data type of
            # 'X' can't be 'String'". Multipicklist is allowed, but only against
            # a picklist field that is itself multi-select.
            if self.data_type not in ("Picklist", "Multipicklist"):
                raise ValueError(
                    f"choice set {self.name!r}: a choice set built from a picklist "
                    "has data_type 'Picklist' - or 'Multipicklist' when the "
                    f"picklist field itself is multi-select - not {self.data_type!r}."
                )
            return self
        if collection_mode:
            missing = [
                field for field, value in (
                    ("display_field", self.display_field),
                    ("value_field", self.value_field),
                ) if not value
            ]
            if missing:
                raise ValueError(
                    f"choice set {self.name!r}: a choice set built from a "
                    f"collection needs {missing} - which field of each item is "
                    "shown, and which is stored."
                )
            return self
        if not query_mode and not (self.display_field or self.value_field):
            raise ValueError(
                f"choice set {self.name!r}: needs a live query "
                "(object + display_field + value_field), a collection "
                "(collection_reference + display_field + value_field), or a "
                "picklist (picklist_object + picklist_field) to build its "
                "options from."
            )
        missing = [
            field for field, value in (
                ("object", self.object),
                ("display_field", self.display_field),
                ("value_field", self.value_field),
            ) if not value
        ]
        if missing:
            raise ValueError(
                f"choice set {self.name!r}: a record choice set needs {missing} - "
                "which object to read, which field the user sees, and which field "
                "is stored."
            )
        return self


ResourceDataType = Literal[
    "String", "Number", "Currency", "Date", "DateTime", "Boolean"
]


class Constant(BaseModel):
    """A fixed value written once and referenced by name. Never reassigned."""

    name: str
    data_type: ResourceDataType
    value: Value
    description: Optional[str] = None

    @field_validator("name")
    @classmethod
    def valid_name(cls, v: str) -> str:
        return _check_api_name(v, "constant name")


class Formula(BaseModel):
    """
    A value recomputed from an expression each time it is read.

    `expression` is Salesforce formula syntax, and it is the one place in this
    IR that holds a free-form string. That is not the exception it looks like:
    the rule elsewhere is that a *condition* must be structured, because a
    condition written as text is how nonsense reaches leftValueReference. A
    formula resource is an expression in Salesforce too, so a string is the
    faithful shape rather than a shortcut.

    The consequence is worth knowing, and it is worse than it looks: an
    expression calling a function that does not exist, and referencing a
    resource that does not exist, was accepted by the org under checkOnly. So a
    formula is the one thing in a flow that nothing verifies before it is live -
    not this IR, and not the validation step either. That is why the approval
    documentation quotes the expression verbatim: a person is the only check.
    """

    name: str
    data_type: ResourceDataType
    expression: str = Field(
        description="Salesforce formula syntax, e.g. '{!v_Total} * 0.2' or "
        "'TODAY()'. References to other resources are written {!name}."
    )
    scale: Optional[int] = None
    description: Optional[str] = None

    @field_validator("name")
    @classmethod
    def valid_name(cls, v: str) -> str:
        return _check_api_name(v, "formula name")


class TextTemplate(BaseModel):
    """
    A block of text with merge fields, written once and referenced by name.

    Its content is not documentation - it is what gets emailed or shown - so it
    belongs in the flow proper and on the screen where the flow is approved.
    """

    name: str
    text: str = Field(
        description="The body. Merge fields look like {!variable_name}."
    )
    is_viewed_as_plain_text: bool = False
    description: Optional[str] = None

    @field_validator("name")
    @classmethod
    def valid_name(cls, v: str) -> str:
        return _check_api_name(v, "text template name")


class VisibilityRule(BaseModel):
    """
    When a field is shown at all.

    The conditions read other fields on the same screen, which is the one place
    a screen field is used as an input to something else. Nothing checks the
    references: the org validated a rule reading `No_Such_Field` without a
    word, and the field then simply never appears. So Flow checks them.
    """

    conditions: List[Condition] = Field(min_length=1)
    condition_logic: str = Field(default="and", description=_FILTER_LOGIC_HELP)

    @model_validator(mode="after")
    def logic_matches_conditions(self) -> "VisibilityRule":
        _check_logic(self.condition_logic, len(self.conditions),
                     "a visibility rule", "condition_logic", "condition")
        return self


class ValidationRule(BaseModel):
    """
    A formula the entered value must satisfy, and what to say when it does not.

    `error_message` is required by the org - "Required field is missing:
    errorMessage" - and rightly: a validation that fails silently is worse than
    none. The formula is the same free-form string as everywhere else, and the
    org accepted `BANANA(` without complaint, so a person reading it is the
    only check there is.
    """

    error_message: str = Field(
        description="Shown to the user when the value does not pass."
    )
    formula_expression: str = Field(
        description="A formula that must be true, e.g. '{!Quantity} > 0'."
    )


class ScreenField(BaseModel):
    """
    One thing on a screen: a paragraph of text, a box the user types into, a
    list of options to pick from, or a custom component.

    The name is not local to the screen. Flow puts screen fields in the same
    namespace as elements and variables, so `{!Customer_Name}` anywhere later in
    the flow reads what was typed here. That is why it must be a valid API name
    and unique flow-wide, and it is what makes a screen input usable as the
    `left` of a condition or the value of an assignment.
    """

    name: str
    field_type: ScreenFieldType
    # Optional only because a ComponentInstance has none: the component draws
    # its own labels. Every other type carries one in every flow observed, and
    # the validator below keeps it that way.
    field_text: Optional[str] = Field(
        default=None,
        description="For DisplayText, the text shown. For any input, its label. "
        "A ComponentInstance has none - the component labels itself.",
    )
    data_type: Optional[ScreenDataType] = Field(
        default=None,
        description="Required for InputField and for any field with choices, "
        "and for a field bound to a record with object_field_reference.",
    )
    # Binds the field straight to a record field instead of collecting a value
    # of its own - e.g. '$Record.Name' or 'Get_Account.Name'. Confirmed
    # against a real dev org's checkOnly validation: the org rejects the field
    # outright ("You can't save a flow without setting the data type value")
    # unless data_type is also set, even though the value being shown already
    # has a type.
    object_field_reference: Optional[str] = None
    data_type_mappings: List[DataTypeMapping] = Field(
        default_factory=list,
        description="Pins a generic Apex-typed component to concrete types, "
        "for components written against T__ style type parameters.",
    )
    is_required: bool = False
    # Shows the field's value without letting it be edited, or greys it out
    # entirely. Distinct flags Salesforce has been seen to emit independently
    # of each other, so both are modelled rather than folded into one.
    #
    # A full Value rather than a plain bool: confirmed against the org's own
    # live Metadata API schema (describeValueType on FlowScreenField), both
    # soapType FlowElementReferenceOrValue - so either can be a literal true or
    # a reference to a variable/formula, the same as default_value below. A
    # plain bool would silently read a dynamic one as false.
    is_read_only: Optional[Value] = None
    is_disabled: Optional[Value] = None
    # Confirmed present on the same live schema, alongside the two above.
    # Never seen set in any example examined - modelled as a plain optional
    # bool since the org describes it as a simple boolean, not a Value.
    is_visible: Optional[bool] = None
    choice_references: List[str] = Field(
        default_factory=list,
        description="Names of Choice or DynamicChoiceSet resources, in the order "
        "they are shown. Only for RadioButtons, DropdownBox, "
        "MultiSelectCheckboxes and MultiSelectPicklist.",
    )
    # What the field already holds when the screen opens. A full Value, because
    # a default is often a reference to a variable rather than a literal.
    default_value: Optional[Value] = None
    # Which option is already selected on a picker. A reference, so it is
    # checked against the defined choices alongside choice_references.
    default_selected_choice: Optional[str] = None
    # Decimal places, as on a variable, and left unconstrained for the same
    # reason: refusing it on a type never seen carrying it would reject flows
    # that already deploy.
    scale: Optional[int] = None

    # ---- ComponentInstance only ------------------------------------------
    # A custom LWC or Aura component dropped onto the screen. The four below
    # travel together and mean nothing on any other field type.
    extension_name: Optional[str] = Field(
        default=None,
        description="The component, as namespace:name - 'c:myComponent' for one "
        "in this org, or a standard one such as 'flowruntime:slider' or "
        "'forceContent:fileUpload'. Only ever name a component you know exists.",
    )
    input_parameters: List[InputAssignment] = Field(
        default_factory=list,
        description="Values passed into the component, keyed by the property "
        "names it declares.",
    )
    output_parameters: List[ComponentOutput] = Field(
        default_factory=list,
        description="Values the component hands back, each into a named variable. "
        "The alternative to store_output_automatically, never both.",
    )
    store_output_automatically: bool = Field(
        default=False,
        description="Keep every output under the field's own name instead of "
        "assigning each one: {!fieldName.outputName}.",
    )
    # What a component already holding values does when the user navigates Back
    # and then forward again. Only UseStoredValues appears in any flow seen, but
    # the org accepts ResetValues too, so both are modelled. None means the flow
    # never said - which is not the same as saying UseStoredValues.
    inputs_on_revisit: Optional[Literal["UseStoredValues", "ResetValues"]] = Field(
        default=None,
        description="On returning to this screen: keep what was entered, or "
        "recompute the inputs.",
    )

    # ---- Anything ---------------------------------------------------------
    help_text: Optional[str] = Field(
        default=None, description="Shown behind the little help icon."
    )
    visibility: Optional[VisibilityRule] = Field(
        default=None,
        description="Show this field only when these conditions hold.",
    )
    validation: Optional[ValidationRule] = None

    # ---- RegionContainer and Region only ----------------------------------
    # A section holds columns and a column holds fields. Nothing else nests.
    fields: List["ScreenField"] = Field(
        default_factory=list,
        description="For a RegionContainer, its Regions. For a Region, the "
        "fields in that column. Empty for anything else.",
    )
    region_container_type: Optional[
        Literal["SectionWithoutHeader", "SectionWithHeader"]
    ] = Field(
        default=None,
        description="Whether the section shows a heading. With a heading, "
        "field_text is the heading.",
    )

    @field_validator("name")
    @classmethod
    def valid_name(cls, v: str) -> str:
        return _check_api_name(v, "screen field name")

    @field_validator("extension_name")
    @classmethod
    def valid_extension_name(cls, v: Optional[str]) -> Optional[str]:
        """
        Salesforce names a component namespace-first. Without the namespace the
        org reports only "We can't find an extension called ...", which reads
        like the component is missing rather than misnamed.
        """
        if v is None:
            return v
        namespace, _, component = v.partition(":")
        if not namespace or not component:
            raise ValueError(
                f"extension_name {v!r} needs a namespace: 'c:{v}' for a component "
                "in this org, or the owning namespace for a standard one "
                "('flowruntime:slider', 'forceContent:fileUpload')."
            )
        return v

    @model_validator(mode="after")
    def layout_nests_exactly_two_deep(self) -> "ScreenField":
        """
        Section holds column holds field, and no further. The org states the
        limit itself: "A RegionContainer screen field can't be a child of a
        Region screen field."
        """
        if self.field_type == "RegionContainer":
            if not self.region_container_type:
                raise ValueError(
                    f"screen field {self.name!r}: a section needs a "
                    "region_container_type - 'SectionWithoutHeader', or "
                    "'SectionWithHeader' with field_text as the heading."
                )
            if self.region_container_type == "SectionWithHeader" and not self.field_text:
                raise ValueError(
                    f"screen field {self.name!r}: \"you must specify a value for "
                    "the fieldText field\" - a section with a header needs the "
                    "heading to show."
                )
            wrong = [f.name for f in self.fields if f.field_type != "Region"]
            if wrong:
                raise ValueError(
                    f"screen field {self.name!r}: a section holds columns, so "
                    f"every entry in fields must be a Region. {wrong} are not. "
                    "Put the fields inside a Region."
                )
        elif self.field_type == "Region":
            wrong = [f.name for f in self.fields
                     if f.field_type in LAYOUT_FIELD_TYPES]
            if wrong:
                raise ValueError(
                    f"screen field {self.name!r}: \"A RegionContainer screen "
                    "field can't be a child of a Region screen field.\" "
                    f"{wrong} cannot go inside a column. Sections do not nest."
                )
            widths = [p for p in self.input_parameters if p.name == "width"]
            if not widths:
                raise ValueError(
                    f"screen field {self.name!r}: \"The {self.name!r} Region "
                    "screen field requires a width input parameter.\" Add an "
                    "input_parameter named 'width' holding a string from '1' to "
                    "'12' - the columns in one section should add up to 12."
                )
        elif self.fields:
            raise ValueError(
                f"screen field {self.name!r}: a {self.field_type} holds a value, "
                "not other fields. Use a RegionContainer to make a section."
            )
        return self

    @model_validator(mode="after")
    def shape_matches_type(self) -> "ScreenField":
        takes_choices = self.field_type in CHOICE_FIELD_TYPES
        may_have_choices = takes_choices or self.field_type in _OPTIONALLY_CHOICE_FIELD_TYPES
        is_component = self.field_type in ("ComponentInstance", "ComponentChoice")
        is_layout = self.field_type in LAYOUT_FIELD_TYPES

        if self.validation and self.field_type == "DisplayText":
            raise ValueError(
                f"screen field {self.name!r}: \"The screen field of type "
                "DisplayText doesn\'t support validation rules.\" It collects "
                "nothing, so there is nothing to validate."
            )

        if is_component and not self.extension_name:
            raise ValueError(
                f"screen field {self.name!r}: a {self.field_type} is a "
                "placeholder for a component, so it needs extension_name to "
                "say which one."
            )
        if not is_component:
            # A Region is the one exception, and it is not really one: its width
            # is carried as an input parameter named "width", which is how
            # Salesforce spells it. The tag is shared; the meaning is not.
            component_only = (
                ("extension_name", self.extension_name),
                ("output_parameters", self.output_parameters),
                ("store_output_automatically", self.store_output_automatically),
                ("inputs_on_revisit", self.inputs_on_revisit),
            )
            if self.field_type != "Region":
                component_only += (("input_parameters", self.input_parameters),)
            stray = [
                attribute for attribute, present in component_only if present
            ]
            if stray:
                raise ValueError(
                    f"screen field {self.name!r}: {', '.join(stray)} describe a "
                    f"custom component, and a {self.field_type} is not one. Use "
                    "field_type 'ComponentInstance' to place a component."
                )

        # Verbatim from the org, which refuses the pair outright:
        #   "You can't use the storeOutputAutomatically field with the
        #    outputParameters field."
        # Both shapes are valid alone. Together the flow does not deploy, and
        # nothing about the two of them looks wrong until it is rejected.
        if self.output_parameters and self.store_output_automatically:
            raise ValueError(
                f"screen field {self.name!r}: \"You can't use the "
                "storeOutputAutomatically field with the outputParameters field.\" "
                "Either assign each output to a variable, or set "
                "store_output_automatically and read them as "
                f"{{!{self.name}.outputName}}."
            )

        if takes_choices and not self.choice_references:
            raise ValueError(
                f"screen field {self.name!r}: a {self.field_type} shows a list of "
                "options, so it needs at least one entry in choice_references "
                "naming a Choice or a DynamicChoiceSet."
            )
        if self.choice_references and not may_have_choices:
            raise ValueError(
                f"screen field {self.name!r}: a {self.field_type} has nowhere to "
                f"show options, so it cannot carry choice_references. Use one of "
                f"{sorted(CHOICE_FIELD_TYPES)} to let the user pick."
            )

        if self.field_type in _NO_VALUE_FIELD_TYPES:
            if self.data_type:
                raise ValueError(
                    f"screen field {self.name!r}: a {self.field_type} carries no "
                    "data_type. Use field_type 'InputField' to collect a typed value."
                )
        elif self.field_type not in _OPTIONAL_DATA_TYPE_FIELD_TYPES:
            if not self.data_type:
                raise ValueError(
                    f"screen field {self.name!r}: a {self.field_type} holds a value, "
                    "so it needs a data_type"
                )

        # A component labels itself, so it needs no field_text - and no flow yet
        # seen gives one. It is left permitted rather than refused for the same
        # reason as data_type above: the org takes it, and a flow that deploys
        # must stay openable.
        #
        # Everything else must say what it shows. Every non-component field in
        # every flow examined carries one, so a missing label is a mistake.
        # A column has no label of its own, and a section only has one when
        # it shows a header - which is checked above, where the org states it.
        if not is_component and not is_layout and not self.field_text:
            raise ValueError(
                f"screen field {self.name!r}: a {self.field_type} needs field_text "
                + ("to say what it shows."
                   if self.field_type == "DisplayText" else "for its label.")
            )

        if self.field_type == "DisplayText":
            if self.is_required:
                raise ValueError(
                    f"screen field {self.name!r}: DisplayText shows text and "
                    "collects nothing, so it cannot be required."
                )
            if self.default_value is not None:
                raise ValueError(
                    f"screen field {self.name!r}: DisplayText holds no value, so "
                    "it has nothing to default. Its text is field_text."
                )

        if self.default_selected_choice and not takes_choices:
            raise ValueError(
                f"screen field {self.name!r}: only a field that shows options can "
                f"have one selected already. Use one of {sorted(CHOICE_FIELD_TYPES)}."
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

    def all_fields(self) -> List[ScreenField]:
        """
        Every field on the screen, including the ones inside sections and
        columns.

        Sections arrived after the flow-level checks were written, and each of
        those walked `fields` one level deep. A field inside a column would have
        escaped all of them: its name would not have been checked for
        collisions, its choices would not have been resolved, and its component
        outputs would not have been checked for a variable to land in - all of
        them silently, on the fields most likely to be in a real screen.
        """
        found: List[ScreenField] = []

        def walk(fields: List[ScreenField]) -> None:
            for screen_field in fields:
                found.append(screen_field)
                if screen_field.fields:
                    walk(screen_field.fields)

        walk(self.fields)
        return found
    # Runtime chrome. Modelled rather than assumed, because these are what the
    # user sees and dropping them would turn Back and Pause back on for an admin
    # who deliberately turned them off.
    allow_back: bool = True
    allow_finish: bool = True
    allow_pause: bool = True
    show_header: bool = True
    show_footer: bool = True
    # Custom wording for the navigation buttons. None means Salesforce's own
    # default label, not an empty button - so these are only written when set.
    paused_text: Optional[str] = None
    next_or_finish_button_label: Optional[str] = None
    back_button_label: Optional[str] = None
    pause_button_label: Optional[str] = None
    # Behind the screen's own help icon - distinct from a field's help_text.
    help_text: Optional[str] = None


class WaitEvent(BaseModel):
    """
    One thing a Pause is waiting for, and where to go when it happens.

    `input_parameters` is a name/value bag rather than named fields, which is
    against the grain of the rest of this IR. It is deliberate: the same tag
    carries a platform event as carries a time, and the keys differ per event
    type. Modelling only the times would refuse every event-driven pause; a bag
    holds any of them and round-trips exactly.

    What the bag costs is checking, so the two time events are checked here by
    name. That is not belt and braces - the org validates *nothing* inside a
    wait. It accepted an AlarmEvent with no parameters, an AlarmEvent whose
    parameter was called AlarmTimeX, and an eventType of BananaEvent. All three
    deploy, and a pause with no time to resume at simply never resumes.
    """

    name: str
    label: Optional[str] = None
    event_type: str = Field(
        default="AlarmEvent",
        description="'AlarmEvent' to resume at a time, 'DateRefAlarmEvent' to "
        "resume relative to a date field on a record, or the API name of a "
        "platform event to resume when one arrives.",
    )
    next: Optional[str] = Field(
        default=None, description="What runs when this is what woke the flow."
    )
    conditions: List[Condition] = Field(
        default_factory=list,
        description="Extra conditions that must hold for this event to resume "
        "the flow.",
    )
    condition_logic: str = Field(default="and", description=_FILTER_LOGIC_HELP)
    input_parameters: List[InputAssignment] = Field(
        default_factory=list,
        description="What the event needs. For AlarmEvent: one named "
        "'AlarmTime' holding a DateTime. For DateRefAlarmEvent: "
        "'SalesforceObject', 'BaseDateTimeFieldName', 'RecordId', and "
        "'TimeOffset' with 'TimeOffsetUnit'.",
    )

    @field_validator("name")
    @classmethod
    def valid_name(cls, v: str) -> str:
        return _check_api_name(v, "wait event name")

    @model_validator(mode="after")
    def the_event_can_actually_fire(self) -> "WaitEvent":
        _check_logic(self.condition_logic, len(self.conditions),
                     f"wait event {self.name!r}", "condition_logic", "condition")

        given = {parameter.name for parameter in self.input_parameters}

        def require(needed: set, why: str) -> None:
            missing = sorted(needed - given)
            if missing:
                raise ValueError(
                    f"wait event {self.name!r}: a {self.event_type} {why}, so it "
                    f"needs the input parameter{'' if len(missing) == 1 else 's'} "
                    f"{', '.join(repr(m) for m in missing)}. Salesforce deploys "
                    "this without them and then never resumes."
                )

        if self.event_type == "AlarmEvent":
            require({"AlarmTime"}, "resumes at a moment in time")
        elif self.event_type == "DateRefAlarmEvent":
            require(
                {"SalesforceObject", "BaseDateTimeFieldName", "RecordId"},
                "resumes relative to a date field on a record",
            )
            # Both or neither, as on a scheduled path: an offset with no unit
            # does not say how much, and no offset at all means the date itself.
            has_offset = "TimeOffset" in given
            has_unit = "TimeOffsetUnit" in given
            if has_offset != has_unit:
                missing = "TimeOffsetUnit" if has_unit is False else "TimeOffset"
                raise ValueError(
                    f"wait event {self.name!r}: an offset needs both "
                    f"'TimeOffset' and 'TimeOffsetUnit', and {missing!r} is "
                    "missing. Leave out both to resume on the date itself."
                )
        return self


TransformActionType = Literal[
    "Map", "Count", "Sum", "GetItemByIndex", "InnerJoin", "InvocableAction"
]


class TransformValueAction(BaseModel):
    """
    One thing that produces a value inside a Transform.

    `transform_type` has six shapes, confirmed against the org's own live
    Metadata API schema (describeValueType on FlowTransformValueAction) -
    'Map' is the simple case, a value copied straight from a field or
    variable. The other five are aggregations over a collection or a call to
    an action, and this build has no example of any of them to model their
    individual shapes confidently - so, like a Wait event's parameters, they
    are read and written back through `input_parameters` rather than guessed
    at. A flow using one of the five still round-trips exactly; it is only
    not drawn as anything more specific than "a transform action".
    """

    name: Optional[str] = None
    transform_type: TransformActionType
    value: Optional[Value] = Field(
        default=None, description="The source, for transform_type 'Map'."
    )
    output_field_api_name: Optional[str] = None
    assign_to_reference: Optional[str] = None
    input_parameters: List[InputAssignment] = Field(default_factory=list)


class TransformValue(BaseModel):
    """
    One path of the transform's output, and what fills it.

    For everything but an InnerJoin, the path is named on each action instead
    - `output_field_api_name` for a field, or nothing at all for a value that
    stands alone. Verified against a real dev org's own checkOnly validation:
    a Map action's TransformValue named directly was rejected outright -
    "The flow metadata specifies 'Name' for the name of a transformValue,
    which is supported only if transformType is InnerJoin."
    """

    name: Optional[str] = None
    label: Optional[str] = None
    description: Optional[str] = None
    actions: List[TransformValueAction] = Field(min_length=1)

    @model_validator(mode="after")
    def name_only_for_a_join(self) -> "TransformValue":
        if self.name and any(a.transform_type != "InnerJoin" for a in self.actions):
            raise ValueError(
                f"\"The flow metadata specifies {self.name!r} for the name of "
                "a transformValue, which is supported only if transformType "
                "is InnerJoin.\" Drop the name, or name the field on each "
                "action instead with output_field_api_name."
            )
        return self


class Transform(BaseElement):
    """
    Builds a record or an Apex-defined object from other values - the
    structured alternative to assembling one field at a time with Assignment.

    Confirmed against the org's own live Metadata API schema (describeValueType
    on FlowTransform) rather than a public sample flow - none was found using
    it. No fault path: unlike a DML element, nothing here can fail at runtime
    in a way the flow can catch.
    """

    type: Literal["Transform"] = "Transform"
    # Exactly what shape the output takes: an SObject (object_type) or an
    # instance of an Apex-defined class (apex_class). Not validated as
    # mutually exclusive - no confirmed example shows both empty or both set,
    # so nothing here assumes which.
    object_type: Optional[str] = None
    apex_class: Optional[str] = None
    is_collection: bool = False
    scale: Optional[int] = None
    schema_uri: Optional[str] = None
    store_output_automatically: bool = False
    transform_values: List[TransformValue] = Field(default_factory=list)


class Wait(FaultCapable):
    """
    Pause: the flow stops here and Salesforce resumes it later.

    Allowed in exactly one kind of flow, which the org states twice over:
    "Flows of type "Screen Flow" can't include Pause elements", and "A flow
    can't include Pause elements when TriggerType is set to Record-Run After
    Save". So a Pause belongs to a plain autolaunched flow - the exact mirror of
    a scheduled path, which is only allowed on a record-triggered one. When a
    request needs a record change to lead to something days later, the answer is
    usually a scheduled path, not this.

    `next` is inherited but unused: a Pause leaves through its events, or
    through `default_next` when none of them was what happened.
    """

    type: Literal["Wait"] = "Wait"
    wait_events: List[WaitEvent] = Field(default_factory=list)
    default_next: Optional[str] = Field(
        default=None,
        description="Where to go when the flow resumes for any other reason.",
    )
    # Required by the org even when there is no default connector to label:
    # "Required field is missing: defaultConnectorLabel".
    default_label: str = "Anything else"

    @model_validator(mode="after")
    def a_pause_has_a_way_out(self) -> "Wait":
        if self.next:
            raise ValueError(
                f"{self.name}: a Pause does not have a plain `next`. It leaves "
                "through a wait event's own `next`, or through `default_next` "
                "when the flow resumed for some other reason."
            )
        names = [event.name for event in self.wait_events]
        repeated = sorted({name for name in names if names.count(name) > 1})
        if repeated:
            raise ValueError(f"{self.name}: duplicate wait event names: {repeated}")
        return self


Element = Annotated[
    Union[
        ActionCall,
        Assignment,
        CollectionFilter,
        CollectionSort,
        Decision,
        GetRecords,
        RecordCreate,
        RecordUpdate,
        RecordDelete,
        Loop,
        Screen,
        Subflow,
        Transform,
        Wait,
    ],
    Field(discriminator="type"),
]


# --------------------------------------------------------------------------
# Start + Flow
# --------------------------------------------------------------------------


class ScheduledPath(BaseModel):
    """
    A branch of a record-triggered flow that runs later, or separately.

    Two different things wear the same tag, and the org keeps them strictly
    apart - "Label, TimeSource, OffsetUnit, OffsetNumber, RecordField,
    MaxBatchSize cannot be set for ScheduledPath of PathType of
    AsyncAfterCommit":

    - `run_asynchronously` - runs straight after the record is saved, in its own
      transaction. Nothing else may be set. This is the path for a callout that
      cannot run inside the trigger's transaction.
    - everything else - runs at a time: an offset from the trigger, or from a
      date field on the record. A negative offset is "before", which only makes
      sense against a field.

    The org checks less here than it looks. It rejects a bad offset unit, a
    record field that is not a date, and a connector pointing at nothing - but
    it accepts a path with no offset at all, an offset with no unit, and
    `time_source: RecordField` naming no field. Those last three are checked
    below, because a path that says "relative to a field" and names no field
    has no reading that makes sense.

    One it accepts and nothing can catch: a `record_field` that does not exist
    on the object. Salesforce validated `NoSuchField__c` without complaint.
    Knowing it is wrong needs the object's schema, which this tool does not read.
    """

    name: str
    label: Optional[str] = None
    next: Optional[str] = Field(
        default=None, description="The element this path runs when it fires."
    )

    run_asynchronously: bool = Field(
        default=False,
        description="Run immediately after the save, in a separate "
        "transaction, rather than at a scheduled time. Nothing else may be set "
        "alongside it.",
    )

    offset_number: Optional[int] = Field(
        default=None,
        description="How long after the time source. Negative is before, which "
        "only makes sense with time_source 'RecordField'.",
    )
    offset_unit: Optional[Literal["Minutes", "Hours", "Days", "Months"]] = None
    time_source: Optional[Literal["RecordTriggerEvent", "RecordField"]] = Field(
        default=None,
        description="Count from when the record changed, or from a date field "
        "on it.",
    )
    record_field: Optional[str] = Field(
        default=None,
        description="The date or date/time field to count from. Required by, "
        "and only meaningful with, time_source 'RecordField'.",
    )
    max_batch_size: Optional[int] = None

    @field_validator("name")
    @classmethod
    def valid_name(cls, v: str) -> str:
        return _check_api_name(v, "scheduled path name")

    @model_validator(mode="after")
    def one_kind_of_path_or_the_other(self) -> "ScheduledPath":
        if self.run_asynchronously:
            stray = [
                attribute
                for attribute, present in (
                    ("label", self.label),
                    ("offset_number", self.offset_number),
                    ("offset_unit", self.offset_unit),
                    ("time_source", self.time_source),
                    ("record_field", self.record_field),
                    ("max_batch_size", self.max_batch_size),
                )
                if present is not None and present != ""
            ]
            if stray:
                raise ValueError(
                    f"scheduled path {self.name!r}: \"Label, TimeSource, "
                    "OffsetUnit, OffsetNumber, RecordField, MaxBatchSize cannot "
                    "be set for ScheduledPath of PathType of AsyncAfterCommit\". "
                    f"Remove {', '.join(stray)}, or drop run_asynchronously and "
                    "give the path a time instead."
                )
            return self

        if (self.offset_number is None) != (self.offset_unit is None):
            if self.offset_unit is None:
                problem = (
                    f"offset_unit is missing, so {self.offset_number} does not "
                    f"say {self.offset_number} of what"
                )
            else:
                problem = (
                    f"offset_number is missing, so {self.offset_unit.lower()} "
                    "does not say how many"
                )
            raise ValueError(
                f"scheduled path {self.name!r}: an offset needs both a number "
                f"and a unit, and {problem}."
            )

        if self.time_source == "RecordField" and not self.record_field:
            raise ValueError(
                f"scheduled path {self.name!r}: time_source 'RecordField' counts "
                "from a date field on the record, so record_field must name one. "
                "To count from the moment the record changed, use "
                "'RecordTriggerEvent' instead."
            )
        if self.record_field and self.time_source != "RecordField":
            raise ValueError(
                f"scheduled path {self.name!r}: record_field "
                f"{self.record_field!r} is only read when time_source is "
                "'RecordField'. Set that, or drop the field."
            )
        return self


class Schedule(BaseModel):
    """
    When a `trigger_type: 'Scheduled'` flow first runs, and how often.

    Confirmed against a real dev org's checkOnly validation. The schema this
    is drawn from carries more fields than are used here - `end_date`,
    `frequency_number`, `day_of_month_to_run`, `days_of_week_to_run` - and
    `frequency` allows more values than the three below - but the org rejected
    every one of them outright when actually deploying a Scheduled trigger:
    "the End Date field isn't supported", "the Frequency Number field isn't
    supported", "the Frequency field can't be set to 'Monthly'" (also tried:
    Yearly, Hourly, Weekdays, OnActivate - all refused the same way). Those
    belong to something else that reuses this shape; a flow's own schedule
    only ever uses the three fields modelled here.
    """

    start_date: str = Field(description="ISO date, e.g. '2026-08-15'.")
    start_time: str = Field(description="ISO time, e.g. '02:00:00.000Z'.")
    frequency: Literal["Once", "Daily", "Weekly"]


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
        Literal["RecordAfterSave", "RecordBeforeSave", "RecordBeforeDelete",
                "Scheduled", "PlatformEvent"]
    ] = None
    # Only meaningful, and only allowed, when trigger_type is 'Scheduled'.
    schedule: Optional[Schedule] = None
    filters: List[RecordFilter] = Field(default_factory=list)
    filter_logic: str = Field(default="and", description=_FILTER_LOGIC_HELP)
    # A formula-based entry condition, standing in for filters/filter_logic
    # (or alongside them - the org accepts both at once). Confirmed against a
    # real dev org: works on a RecordAfterSave trigger with `object` set;
    # setting it without a record object (e.g. on a Scheduled trigger) blows
    # up with an opaque "An unexpected error occurred" from the org rather
    # than a clean rejection, so it is restricted to record-triggered starts
    # here rather than guessed at further.
    filter_formula: Optional[str] = None
    # "Only when a record is updated to meet the condition requirements".
    # Changes when the flow runs, so it cannot be dropped silently.
    only_when_changed_to_meet_criteria: bool = False
    scheduled_paths: List[ScheduledPath] = Field(
        default_factory=list,
        description="Extra branches that run later, or in their own "
        "transaction. Only on a RecordAfterSave trigger.",
    )
    # Which user context after-save automation runs in. Confirmed against a
    # real dev org: only meaningful on a RecordAfterSave trigger - setting it
    # on RecordBeforeSave or Scheduled deploys fine but the org reports back
    # "the RunAsUser field isn't supported" for that trigger type, i.e. it
    # would be silently ignored at runtime. Rejected here instead.
    flow_run_as_user: Optional[Literal["TriggeringUser", "DefaultWorkflowUser"]] = None

    @model_validator(mode="after")
    def scheduled_trigger_has_a_schedule(self) -> "Start":
        if self.trigger_type == "Scheduled" and not self.schedule:
            raise ValueError(
                "\"You set the flow trigger type to Scheduled, so you must "
                "also set the frequency.\" Set schedule with start_date, "
                "start_time and frequency."
            )
        if self.schedule and self.trigger_type != "Scheduled":
            raise ValueError(
                "a schedule only means something when trigger_type is "
                "'Scheduled'"
            )
        return self

    @model_validator(mode="after")
    def scheduled_paths_belong_on_an_after_save_trigger(self) -> "Start":
        """
        The org's own words, one message per trigger type: "Flows with the
        trigger type RecordBeforeSave can't have scheduled paths." Nothing has
        happened yet in a before-save trigger, so there is no committed record
        to come back to.
        """
        if not self.scheduled_paths:
            return self
        if self.trigger_type != "RecordAfterSave":
            where = self.trigger_type or "a flow with no record trigger"
            raise ValueError(
                f"\"Flows with the trigger type {where} can't have scheduled "
                "paths.\" A scheduled path resumes against a record that is "
                "already saved, so the trigger must be 'RecordAfterSave'."
            )
        names = [path.name for path in self.scheduled_paths]
        repeated = sorted({name for name in names if names.count(name) > 1})
        if repeated:
            raise ValueError(f"duplicate scheduled path names: {repeated}")
        return self

    @model_validator(mode="after")
    def logic_matches_filters(self) -> "Start":
        _check_logic(self.filter_logic, len(self.filters),
                     "the flow's entry conditions", "filter_logic", "filter")
        return self

    @model_validator(mode="after")
    def filter_formula_needs_a_record(self) -> "Start":
        if self.filter_formula and not self.object:
            raise ValueError(
                "a filter formula is evaluated against a triggering record, "
                "so it only means something on a record-triggered start "
                "(set object + trigger_type)"
            )
        return self

    @model_validator(mode="after")
    def run_as_user_needs_an_after_save_trigger(self) -> "Start":
        if self.flow_run_as_user and self.trigger_type != "RecordAfterSave":
            where = self.trigger_type or "a flow with no record trigger"
            raise ValueError(
                f"\"When the TriggerType field is set to '{where}', the "
                "RunAsUser field isn't supported.\" flow_run_as_user only "
                "applies to a RecordAfterSave trigger."
            )
        return self

    @model_validator(mode="after")
    def trigger_consistency(self) -> "Start":
        if self.object and not self.trigger_type:
            raise ValueError("a record-triggered start requires trigger_type")
        if self.trigger_type and self.trigger_type != "Scheduled" and not self.object:
            raise ValueError(f"trigger_type {self.trigger_type} requires an object")
        # A platform event has no create-or-update to distinguish: the event is
        # published, and that is the only thing that ever happens to one. The
        # org takes a recordTriggerType here and ignores it; requiring one would
        # mean asking for an answer to a question the trigger does not pose.
        if (self.object and not self.record_trigger_type
                and self.trigger_type != "PlatformEvent"):
            raise ValueError("a record-triggered start requires record_trigger_type")
        if self.trigger_type == "PlatformEvent" and self.record_trigger_type:
            raise ValueError(
                "a platform event is only ever published, so "
                f"record_trigger_type {self.record_trigger_type!r} has nothing "
                "to describe. Leave it empty."
            )
        return self


class Flow(BaseModel):
    api_name: str
    label: str
    description: Optional[str] = None
    api_version: str = "62.0"
    # "Flow" is Salesforce's name for a screen flow — the one a user runs and
    # watches. "AutoLaunchedFlow" covers both record-triggered and autolaunched.
    process_type: Literal["AutoLaunchedFlow", "Flow"] = "AutoLaunchedFlow"
    # Draft and Active are the two this tool ever writes. Obsolete and
    # InvalidDraft are what Salesforce marks superseded and broken versions
    # with; a flow retrieved from an org can be either, and refusing them would
    # make an old version unreadable rather than unwritable.
    status: Literal["Draft", "Active", "Obsolete", "InvalidDraft"] = "Draft"
    start: Start
    elements: List[Element] = Field(default_factory=list)
    variables: List[Variable] = Field(default_factory=list)
    # Resources, not elements: nothing connects to them, screen fields name them.
    choices: List[Choice] = Field(default_factory=list)
    dynamic_choice_sets: List[DynamicChoiceSet] = Field(default_factory=list)
    text_templates: List[TextTemplate] = Field(default_factory=list)
    constants: List[Constant] = Field(default_factory=list)
    formulas: List[Formula] = Field(default_factory=list)

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
        if isinstance(element, Wait):
            targets.extend(event.next for event in element.wait_events if event.next)
            if element.default_next:
                targets.append(element.default_next)
        fault = getattr(element, "fault_next", None)
        if fault:
            targets.append(fault)
        timeout = getattr(element, "timeout_next", None)
        if timeout:
            targets.append(timeout)
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
        for path in self.start.scheduled_paths:
            lines.append(f"  scheduled path {path.name} -> {path.next or '(ends)'}")
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
            elif isinstance(element, Wait):
                for event in element.wait_events:
                    lines.append(
                        f"  {element.name} on {event.name} -> "
                        f"{event.next or '(ends)'}"
                    )
                lines.append(
                    f"  {element.name} default -> {element.default_next or '(ends)'}"
                )
            else:
                lines.append(f"  {element.name} -> {element.next or '(ends)'}")
            fault = getattr(element, "fault_next", None)
            if fault:
                lines.append(f"  {element.name} on fault -> {fault}")
            timeout = getattr(element, "timeout_next", None)
            if timeout:
                lines.append(f"  {element.name} on timeout -> {timeout}")
        return "\n".join(lines)

    def reachable(self) -> set:
        """
        Names reachable from any entry point.

        A scheduled path is an entry point, not a continuation: the flow stops
        at the end of the immediate run and starts again later at the path's own
        connector. Counting only start.next would report everything a scheduled
        path reaches as unreachable.
        """
        by_name = self.by_name()
        seen: set = set()
        queue = [self.start.next] if self.start.next else []
        queue.extend(
            path.next for path in self.start.scheduled_paths if path.next
        )
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
        for path in self.start.scheduled_paths:
            check(path.next, f"scheduled path {path.name}")
        for el in self.elements:
            if isinstance(el, Wait):
                for event in el.wait_events:
                    check(event.next, f"{el.name}.{event.name}.next")
                check(el.default_next, f"{el.name}.default_next")
            check(el.next, f"{el.name}.next")
            if isinstance(el, Decision):
                for oc in el.outcomes:
                    check(oc.next, f"{el.name}.{oc.name}.next")
            if isinstance(el, Loop):
                check(el.first_element, f"{el.name}.first_element")
            check(getattr(el, "fault_next", None), f"{el.name}.fault_next")
            check(getattr(el, "timeout_next", None), f"{el.name}.timeout_next")

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

        return self

    def warnings(self) -> List[str]:
        """
        Things that are probably a mistake but that Salesforce allows.

        Distinct from a validator on purpose. A validator says the flow cannot
        be represented or cannot deploy; a warning says it will deploy and you
        may not have meant it. Refusing the second kind is how a tool becomes
        unable to open flows that are working in production right now.

        An unreachable element was an error here until a live, Active flow in
        Salesforce's own sample apps turned out to have one - Update_Profile
        with no connector, Assign_Output that nothing reaches. The org deploys
        it happily. Refusing it meant the flow could not be opened at all, which
        is a worse outcome than drawing it with a note attached.

        The model still gets told, so it keeps repairing the case that made this
        check worth having: a forgotten connector on a flow it just wrote.
        """
        notes: List[str] = []
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
                f"\nPaths that currently stop: {dead_ends}. The connector that is "
                "missing almost certainly belongs to one of these - set its `next` "
                "to whatever should run after it."
                if dead_ends
                else ""
            )
            notes.append(
                f"unreachable elements: {sorted(orphans)}. No path from Start "
                "reaches them, so they never run. Here is every connector as it "
                f"stands:\n{self.connector_map()}\n"
                "Set the connector that should lead to each unreachable element. "
                "For a Decision, that is the outcome's own `next` - an outcome "
                "with next=null ends the path instead of continuing to the "
                f"element you meant.{hint}"
            )
        return notes

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
    def a_pause_belongs_in_an_autolaunched_flow(self) -> "Flow":
        """
        Both halves are the org's own words, and between them they leave exactly
        one kind of flow that may pause:

          "Flows of type "Screen Flow" can't include Pause elements."
          "A flow can't include Pause elements when TriggerType is set to
           Record-Run After Save."

        The mirror of scheduled_paths, which are allowed only where a Pause is
        not. Worth saying in the error, because the request behind both is the
        same one - do this later - and the right answer depends on what started
        the flow.
        """
        pauses = sorted(e.name for e in self.elements if isinstance(e, Wait))
        if not pauses:
            return self
        if self.process_type == "Flow":
            raise ValueError(
                f"\"Flows of type \"Screen Flow\" can't include Pause elements.\" "
                f"{pauses} cannot be here. A screen flow runs while someone "
                "waits for it, so there is nobody to come back to."
            )
        if self.start.trigger_type or self.start.object:
            raise ValueError(
                "\"A flow can't include Pause elements when TriggerType is set "
                f"to Record-Run After Save.\" {pauses} cannot be in a "
                "record-triggered flow. To do something later after a record "
                "changes, add a scheduled_path to the start instead - that is "
                "the same idea in the form this kind of flow allows."
            )
        return self

    @model_validator(mode="after")
    def choice_references_resolve(self) -> "Flow":
        """
        Every option a screen offers must be defined. Salesforce reports a
        dangling choice reference as a deploy failure naming only the screen, so
        it is worth catching here where the field itself can be named.
        """
        defined = {c.name for c in self.choices} | {
            cs.name for cs in self.dynamic_choice_sets
        }
        problems: List[str] = []
        for element in self.elements:
            if not isinstance(element, Screen):
                continue
            for screen_field in element.all_fields():
                for reference in screen_field.choice_references:
                    if reference not in defined:
                        problems.append(
                            f"{element.name}.{screen_field.name} offers "
                            f"{reference!r}"
                        )
                preselected = screen_field.default_selected_choice
                if preselected and preselected not in defined:
                    problems.append(
                        f"{element.name}.{screen_field.name} starts with "
                        f"{preselected!r} selected"
                    )
        if problems:
            known = sorted(defined) or "nothing"
            raise ValueError(
                "these screen fields offer options that are not defined: "
                + "; ".join(problems)
                + f". Defined choices and choice sets: {known}. Add a Choice (a "
                "fixed option) or a DynamicChoiceSet (options built from records "
                "or a picklist) for each one, or point the field at one that exists."
            )
        return self

    @model_validator(mode="after")
    def visibility_rules_read_something_real(self) -> "Flow":
        """
        A field shown conditionally reads other resources by name, and nothing
        else checks those names.

        The org validated a rule reading `No_Such_Field` without a word. The
        flow deploys, the condition can never be true, and the field simply
        never appears - which looks exactly like a field somebody decided not
        to show. The same silence as a dangling choice reference, and worse to
        diagnose, because there is nothing on the screen to notice is missing.

        The root before the first dot is what has to exist, so `$Record.Amount`
        needs `$Record` and a screen field's own name needs the field.
        """
        known = {"$Record", "$Record__Prior", "$User", "$Flow", "$Organisation",
                 "$Organization", "$Setup", "$Permission", "$Profile",
                 "$Api", "$System", "$Label"}
        known |= {v.name for v in self.variables}
        known |= {c.name for c in self.choices}
        known |= {c.name for c in self.dynamic_choice_sets}
        known |= {c.name for c in self.constants}
        known |= {f.name for f in self.formulas}
        known |= {t.name for t in self.text_templates}
        known |= {e.name for e in self.elements}
        for element in self.elements:
            if isinstance(element, Screen):
                known |= {f.name for f in element.all_fields()}

        problems: List[str] = []
        for element in self.elements:
            if not isinstance(element, Screen):
                continue
            for screen_field in element.all_fields():
                if not screen_field.visibility:
                    continue
                for condition in screen_field.visibility.conditions:
                    root = condition.left.split(".")[0]
                    if root and root not in known:
                        problems.append(
                            f"{element.name}.{screen_field.name} is shown when "
                            f"{condition.left!r} matches"
                        )
        if problems:
            raise ValueError(
                "these visibility rules read something the flow does not "
                "define: " + "; ".join(problems)
                + ". Salesforce deploys this without complaint and the field "
                "then never appears, which is indistinguishable from one you "
                "meant to hide."
            )
        return self

    @model_validator(mode="after")
    def retrieved_fields_land_somewhere(self) -> "Flow":
        """
        Every field a Get Records assigns out must name a variable that exists.

        The org accepts one that does not - it validated an assignment into
        `v_Nope` without a word, and the field is then read from the record and
        dropped. The same shape of silence as a component output, and checked
        here for the same reason.

        A Loop's own variable is checked too, though the org does catch that one
        ("Value v_Nope in the AssignNextValueTo element doesn't exist in this
        flow"). Catching it here means the model is told before a deploy round
        trip rather than after one.
        """
        variables = {v.name for v in self.variables}
        problems: List[str] = []
        for element in self.elements:
            if isinstance(element, GetRecords):
                for assignment in element.output_assignments:
                    root = assignment.assign_to_reference.split(".")[0]
                    if root not in variables:
                        problems.append(
                            f"{element.name} puts {assignment.field!r} into "
                            f"{assignment.assign_to_reference!r}"
                        )
            if isinstance(element, Loop) and element.assign_next_value_to_reference:
                root = element.assign_next_value_to_reference.split(".")[0]
                if root not in variables:
                    problems.append(
                        f"{element.name} puts each item into "
                        f"{element.assign_next_value_to_reference!r}"
                    )
        if problems:
            known = sorted(variables) or "nothing"
            raise ValueError(
                "these land in variables that do not exist: "
                + "; ".join(problems) + f". Defined variables: {known}."
            )
        return self

    @model_validator(mode="after")
    def component_outputs_land_somewhere(self) -> "Flow":
        """
        Every component output must name a variable that exists.

        This one is enforced here because nothing else enforces it anywhere. The
        org accepts `assignToReference` pointing at a variable that was never
        defined - checkOnly passes, the flow deploys, the component runs, and the
        value it produces is dropped on the floor. The same silence as a dangling
        choice reference, and for the same reason: there is no connector leading
        to that name, so no other check ever looks at it.

        The root before the first dot is what has to exist: `varRecord.Name`
        writes to a field of `varRecord`.
        """
        variables = {v.name for v in self.variables}
        problems: List[str] = []
        for element in self.elements:
            if not isinstance(element, Screen):
                continue
            for screen_field in element.all_fields():
                for parameter in screen_field.output_parameters:
                    root = parameter.assign_to_reference.split(".")[0]
                    if root not in variables:
                        problems.append(
                            f"{element.name}.{screen_field.name} sends its "
                            f"{parameter.name!r} output to "
                            f"{parameter.assign_to_reference!r}"
                        )
        if problems:
            known = sorted(variables) or "nothing"
            raise ValueError(
                "these component outputs are assigned to variables that do not "
                "exist: " + "; ".join(problems) + f". Defined variables: {known}. "
                "Salesforce deploys this without complaint and then discards the "
                "value, so add the variable or point the output at one that exists."
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
                for screen_field in element.all_fields():
                    claim(screen_field.name, f"a field on screen {element.name}")
        for variable in self.variables:
            claim(variable.name, "a variable")
        for choice in self.choices:
            claim(choice.name, "a choice")
        for choice_set in self.dynamic_choice_sets:
            claim(choice_set.name, "a choice set")
        for template in self.text_templates:
            claim(template.name, "a text template")
        for constant in self.constants:
            claim(constant.name, "a constant")
        for formula in self.formulas:
            claim(formula.name, "a formula")

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
