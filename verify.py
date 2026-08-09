"""
Every metadata shape this tool emits, checked against a real org.

    python verify.py --org dev

The test suite proves the IR agrees with itself. It cannot prove the XML is
right, because the only authority on that is Salesforce - so each shape here was
settled by asking the org, and this file is where those answers live now. Before
it existed they lived in throwaway scripts in a temp directory, which meant the
same questions got asked again a month later.

Two kinds of case, and the second is the more valuable:

  shapes  Built through the IR, round-tripped, then sent to the org under
          checkOnly. A failure means what we generate is not what Salesforce
          takes.

  guards  Things the IR refuses. Each one records what the org does with the
          same flow - and most of the time the answer is "deploys it happily",
          which is exactly why the check has to live in the IR. These need no
          org and run anyway.

Read-only. checkOnly means Salesforce validates and discards; nothing is
created, updated or deployed, and no flow of yours is touched. The cases use
standard objects and standard components only, so they work in any org.

    python verify.py                  # guards only, no org needed
    python verify.py --org dev
    python verify.py --org dev --only component
    python verify.py --list
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from dataclasses import dataclass, field
from typing import Callable, List

from flowtool.ir import (
    ActionCall,
    Assignment,
    AssignmentItem,
    Choice,
    ComponentOutput,
    Condition,
    Constant,
    Decision,
    DynamicChoiceSet,
    FieldValue,
    Flow,
    Formula,
    GetRecords,
    InputAssignment,
    Loop,
    Outcome,
    OutputAssignment,
    RecordCreate,
    RecordDelete,
    RecordFilter,
    RecordUpdate,
    ScheduledPath,
    Screen,
    ScreenField,
    Start,
    TextTemplate,
    ValidationRule,
    Value,
    Variable,
    VisibilityRule,
    Wait,
    WaitEvent,
)
from flowtool.parse import parse_flow
from flowtool.xmlgen import generate


@dataclass
class Shape:
    """A flow the org must accept."""

    group: str
    name: str
    flow: Flow
    note: str = ""


@dataclass
class Guard:
    """
    Something the IR refuses. `org` says what Salesforce does with it, which is
    the whole point of writing it down.
    """

    group: str
    name: str
    build: Callable[[], object]
    org: str
    expect: str = ""


# --------------------------------------------------------------------------
# Building blocks
# --------------------------------------------------------------------------

HOT = Value(string_value="Hot")


def flow(api_name: str, *elements, **kwargs) -> Flow:
    kwargs.setdefault("start", Start(next=elements[0].name))
    return Flow(
        api_name=f"Flow_Tool_Verify_{api_name}",
        label=f"Flow Tool Verify {api_name}",
        elements=list(elements),
        **kwargs,
    )


def triggered(api_name: str, *elements, **kwargs) -> Flow:
    kwargs.setdefault("start", Start(
        next=elements[0].name, object="Opportunity",
        record_trigger_type="CreateAndUpdate", trigger_type="RecordAfterSave",
    ))
    return flow(api_name, *elements, **kwargs)


def screen_flow(api_name: str, *elements, **kwargs) -> Flow:
    kwargs.setdefault("process_type", "Flow")
    kwargs.setdefault("start", Start(next=elements[0].name))
    return flow(api_name, *elements, **kwargs)


def mark_hot(name: str = "Mark") -> RecordUpdate:
    return RecordUpdate(name=name, label=name, input_reference="$Record",
                        fields=[FieldValue(field="Rating", value=HOT)])


def slider(**kwargs) -> ScreenField:
    fields = dict(
        name="howMany", field_type="ComponentInstance",
        extension_name="flowruntime:slider", is_required=True,
        input_parameters=[
            InputAssignment(name="label", value=Value(string_value="How many?")),
            InputAssignment(name="max", value=Value(number_value=250)),
        ],
        store_output_automatically=True,
    )
    fields.update(kwargs)
    return ScreenField(**fields)


def column(name: str, width: int, *fields) -> ScreenField:
    """A column. Its width is an input parameter because that is how Salesforce
    spells it - the same tag a component uses for its own inputs."""
    return ScreenField(
        name=name, field_type="Region", fields=list(fields),
        input_parameters=[InputAssignment(name="width",
                                          value=Value(string_value=str(width)))])


def section(name: str, *columns, **kwargs) -> ScreenField:
    fields = dict(name=name, field_type="RegionContainer",
                  region_container_type="SectionWithoutHeader",
                  fields=list(columns))
    fields.update(kwargs)
    return ScreenField(**fields)


def number_field(name: str, **kwargs) -> ScreenField:
    fields = dict(name=name, field_type="InputField", field_text=name,
                  data_type="Number")
    fields.update(kwargs)
    return ScreenField(**fields)


COLOUR = ScreenField(name="Colour", field_type="RadioButtons",
                     field_text="Pick a colour", data_type="String",
                     choice_references=["Red"])
RED = Choice(name="Red", choice_text="Red")
SHOW_IF_RED = VisibilityRule(conditions=[
    Condition(left="Colour", operator="EqualTo",
              right=Value(string_value="Red"))])

ACCOUNTS = Variable(name="v_Accounts", data_type="SObject",
                    object_type="Account", is_collection=True)
COUNT = Variable(name="v_Count", data_type="Number", scale=0)
TEXT = Variable(name="v_Text", data_type="String")
WHEN = Variable(name="v_When", data_type="DateTime")
THE_ID = Variable(name="v_Id", data_type="String", is_input=True)
ONE_ACCOUNT = Variable(name="v_One", data_type="SObject",
                       object_type="Account")


# --------------------------------------------------------------------------
# Shapes
# --------------------------------------------------------------------------

SHAPES: List[Shape] = [
    # ---- Records -----------------------------------------------------------
    Shape("records", "get records into a variable", flow(
        "Get_Into_Var",
        GetRecords(name="Get", label="Get", object="Account",
                   first_record_only=False, store_output_automatically=False,
                   output_reference="v_Accounts"),
        variables=[ACCOUNTS],
    ), "manual storage and automatic storage are exclusive"),
    Shape("records", "get records with a named field list", flow(
        "Get_Fields",
        GetRecords(name="Get", label="Get", object="Account",
                   queried_fields=["Id", "Name", "Rating"],
                   filters=[RecordFilter(field="Rating", operator="EqualTo",
                                         value=HOT)],
                   sort_field="Name", sort_order="Asc"),
    )),
    Shape("records", "create and keep the new Id", flow(
        "Create_Assign_Id",
        RecordCreate(name="Make", label="Make", object="Task",
                     fields=[FieldValue(field="Subject",
                                        value=Value(string_value="Follow up"))],
                     assign_record_id_to_reference="v_Text"),
        variables=[TEXT],
    ), "assignRecordIdToReference excludes storeOutputAutomatically"),
    Shape("records", "update the triggering record", triggered(
        "Update_Trigger", mark_hot(),
    )),
    Shape("records", "delete by criteria", flow(
        "Delete_By_Criteria",
        RecordDelete(name="Remove", label="Remove", object="Task",
                     filters=[RecordFilter(field="IsClosed", operator="EqualTo",
                                           value=Value(boolean_value=True))]),
    )),
    Shape("records", "fields assigned into their own variables", flow(
        "Get_Assign_Fields",
        GetRecords(name="Get", label="Get", object="Account",
                   store_output_automatically=False, output_assignments=[
                       OutputAssignment(field="Name",
                                        assign_to_reference="v_Text")]),
        variables=[TEXT],
    ), "the third way of handing records back, exclusive with both others"),
    Shape("records", "null the variables when nothing is found", flow(
        "Get_Null_Values",
        GetRecords(name="Get", label="Get", object="Account",
                   assign_null_values_if_no_records_found=True),
    ), "was written back as false regardless, whatever the flow said"),
    Shape("records", "a fault path", flow(
        "Fault_Path",
        GetRecords(name="Get", label="Get", object="Account", next=None,
                   fault_next="Note"),
        Assignment(name="Note", label="Note", items=[
            AssignmentItem(to_reference="v_Text",
                           value=Value(string_value="failed"))]),
        variables=[TEXT],
    )),

    # ---- Assignment operators ---------------------------------------------
    Shape("assignments", "count a collection", flow(
        "Assign_Count",
        Assignment(name="Set", label="Set", items=[
            AssignmentItem(to_reference="v_Count", operator="AssignCount",
                           value=Value(element_reference="v_Accounts"))]),
        variables=[COUNT, ACCOUNTS],
    ), "AssignCount was missing until a live flow used it"),
    Shape("assignments", "blank a variable", flow(
        "Assign_Empty",
        Assignment(name="Set", label="Set", items=[
            AssignmentItem(to_reference="v_Text", value=Value(string_value=""))]),
        variables=[TEXT],
    ), "<stringValue /> is an empty string, not a missing value"),
    Shape("assignments", "add to and remove from a collection", flow(
        "Assign_Collection",
        Assignment(name="Set", label="Set", items=[
            AssignmentItem(to_reference="v_Texts", operator="AddItem",
                           value=Value(string_value="x")),
            AssignmentItem(to_reference="v_Texts", operator="RemoveFirst",
                           value=Value(string_value="x"))]),
        variables=[Variable(name="v_Texts", data_type="String",
                            is_collection=True)],
    )),

    # ---- Condition logic ---------------------------------------------------
    Shape("logic", "a custom decision expression", flow(
        "Logic_Custom",
        Decision(name="Check", label="Check", outcomes=[
            Outcome(name="Yes", label="Yes", condition_logic="1 OR (2 AND 3)",
                    conditions=[
                        Condition(left="v_Count", operator="GreaterThan",
                                  right=Value(number_value=n))
                        for n in (1, 2, 3)])]),
        variables=[COUNT],
    ), "the org accepts any string here, including 'banana'"),
    Shape("logic", "a custom filter expression", flow(
        "Logic_Filter",
        GetRecords(name="Get", label="Get", object="Account",
                   filter_logic="1 OR 2",
                   filters=[
                       RecordFilter(field="Rating", operator="EqualTo", value=HOT),
                       RecordFilter(field="Industry", operator="EqualTo",
                                    value=Value(string_value="Banking"))]),
    )),

    # ---- Resources ---------------------------------------------------------
    Shape("resources", "constants and a formula", flow(
        "Resources",
        Assignment(name="Set", label="Set", items=[
            AssignmentItem(to_reference="v_Text",
                           value=Value(element_reference="f_Greeting"))]),
        variables=[TEXT],
        constants=[Constant(name="c_Prefix", data_type="String",
                            value=Value(string_value="Hello "))],
        formulas=[Formula(name="f_Greeting", data_type="String",
                          expression="{!c_Prefix} & 'world'")],
    ), "nothing validates a formula expression, here or in the org"),
    Shape("resources", "a text template with blank lines", flow(
        "Templates",
        Assignment(name="Set", label="Set", items=[
            AssignmentItem(to_reference="v_Text",
                           value=Value(element_reference="t_Body"))]),
        variables=[TEXT],
        text_templates=[TextTemplate(
            name="t_Body", is_viewed_as_plain_text=True,
            text="First line.\n\nSecond paragraph.")],
    ), "a blank line is content; the old pretty-printer ate them"),

    # ---- Screens -----------------------------------------------------------
    Shape("screens", "text, input and long text", screen_flow(
        "Screen_Basics",
        Screen(name="Ask", label="Ask", fields=[
            ScreenField(name="Intro", field_type="DisplayText",
                        field_text="<p>Welcome</p>"),
            ScreenField(name="Customer_Email", field_type="InputField",
                        field_text="Email", data_type="String",
                        is_required=True),
            ScreenField(name="Notes", field_type="LargeTextArea",
                        field_text="Anything else?"),
        ]),
    )),
    Shape("screens", "a default value and a scale", screen_flow(
        "Screen_Defaults",
        Screen(name="Ask", label="Ask", fields=[
            ScreenField(name="Quantity", field_type="InputField",
                        field_text="How many?", data_type="Number", scale=0,
                        default_value=Value(number_value=1)),
        ]),
    )),

    Shape("screens", "help text and a validation rule", screen_flow(
        "Screen_Rules",
        Screen(name="Ask", label="Ask", fields=[number_field(
            "Quantity", help_text="<p>Type a number.</p>",
            validation=ValidationRule(error_message="Must be more than zero.",
                                      formula_expression="{!Quantity} > 0"))]),
    ), "the org takes a nonsense formula here without a word"),
    Shape("screens", "a field shown only sometimes", screen_flow(
        "Screen_Visibility",
        Screen(name="Ask", label="Ask",
               fields=[COLOUR, number_field("Quantity", visibility=SHOW_IF_RED)]),
        choices=[RED],
    ), "a rule reading a field that does not exist also deploys"),
    Shape("screens", "two columns in a section", screen_flow(
        "Screen_Section",
        Screen(name="Ask", label="Ask", fields=[section(
            "Section_1",
            column("Column_1", 6, number_field("Left")),
            column("Column_2", 6, number_field("Right")))]),
    )),
    Shape("screens", "a section with a heading, holding a conditional field",
          screen_flow(
              "Screen_Section_Header",
              Screen(name="Ask", label="Ask", fields=[COLOUR, section(
                  "Section_1",
                  column("Column_1", 12,
                         number_field("Quantity", visibility=SHOW_IF_RED)),
                  region_container_type="SectionWithHeader",
                  field_text="Details")]),
              choices=[RED],
          ), "sections nest exactly two deep and no further"),

    # ---- Choices -----------------------------------------------------------
    Shape("choices", "radio buttons from fixed choices", screen_flow(
        "Choice_Radio",
        Screen(name="Ask", label="Ask", fields=[
            ScreenField(name="Colour", field_type="RadioButtons",
                        field_text="Pick a colour", data_type="String",
                        is_required=True, choice_references=["Red", "Blue"],
                        default_selected_choice="Red")]),
        choices=[Choice(name="Red", choice_text="Red"),
                 Choice(name="Blue", choice_text="Blue")],
    )),
    Shape("choices", "a multi-select stores a String", screen_flow(
        "Choice_Multi",
        Screen(name="Ask", label="Ask", fields=[
            ScreenField(name="Topics", field_type="MultiSelectCheckboxes",
                        field_text="Which apply?", data_type="String",
                        choice_references=["Billing"])]),
        choices=[Choice(name="Billing", choice_text="Billing")],
    ), "the org rejects Multipicklist here, which is what it looks like"),
    Shape("choices", "options built from records", screen_flow(
        "Choice_Records",
        Screen(name="Ask", label="Ask", fields=[
            ScreenField(name="Which_Account", field_type="DropdownBox",
                        field_text="Pick an account", data_type="String",
                        choice_references=["Hot_Accounts"])]),
        dynamic_choice_sets=[DynamicChoiceSet(
            name="Hot_Accounts", object="Account", display_field="Name",
            value_field="Id", sort_field="Name", sort_order="Asc", limit=20,
            filters=[RecordFilter(field="Rating", operator="EqualTo",
                                  value=HOT)])],
    )),
    Shape("choices", "options built from a picklist", screen_flow(
        "Choice_Picklist",
        Screen(name="Ask", label="Ask", fields=[
            ScreenField(name="Which_Industry", field_type="DropdownBox",
                        field_text="Pick an industry", data_type="String",
                        choice_references=["Industries"])]),
        dynamic_choice_sets=[DynamicChoiceSet(
            name="Industries", data_type="Picklist",
            picklist_object="Account", picklist_field="Industry")],
    ), "a picklist choice set's own data type must be Picklist"),

    # ---- Screen components -------------------------------------------------
    Shape("components", "a component storing its outputs automatically",
          screen_flow("Component_Auto",
                      Screen(name="Ask", label="Ask", fields=[slider()]))),
    Shape("components", "a component assigning an output", screen_flow(
        "Component_Assign",
        Screen(name="Ask", label="Ask", fields=[slider(
            store_output_automatically=False,
            output_parameters=[ComponentOutput(name="value",
                                               assign_to_reference="v_Count")])]),
        variables=[COUNT],
    )),
    Shape("components", "a component that keeps values on revisit", screen_flow(
        "Component_Revisit",
        Screen(name="Ask", label="Ask",
               fields=[slider(inputs_on_revisit="UseStoredValues")]),
    )),

    # ---- Scheduled paths ---------------------------------------------------
    Shape("paths", "three days after the trigger", triggered(
        "Path_After", mark_hot(), mark_hot("Later"),
        start=Start(next="Mark", object="Opportunity",
                    record_trigger_type="CreateAndUpdate",
                    trigger_type="RecordAfterSave",
                    scheduled_paths=[ScheduledPath(
                        name="Chase", label="Chase it", next="Later",
                        offset_number=3, offset_unit="Days",
                        time_source="RecordTriggerEvent")]),
    )),
    Shape("paths", "one day before a date field", triggered(
        "Path_Before", mark_hot(), mark_hot("Later"),
        start=Start(next="Mark", object="Opportunity",
                    record_trigger_type="CreateAndUpdate",
                    trigger_type="RecordAfterSave",
                    scheduled_paths=[ScheduledPath(
                        name="Before_Close", next="Later", offset_number=-1,
                        offset_unit="Days", record_field="CloseDate",
                        time_source="RecordField")]),
    ), "a negative offset only makes sense against a field"),
    Shape("paths", "run asynchronously", triggered(
        "Path_Async", mark_hot(), mark_hot("Later"),
        start=Start(next="Mark", object="Opportunity",
                    record_trigger_type="CreateAndUpdate",
                    trigger_type="RecordAfterSave",
                    scheduled_paths=[ScheduledPath(
                        name="Async", next="Later", run_asynchronously=True)]),
    ), "an async path may carry nothing else at all"),

    # ---- Pause -------------------------------------------------------------
    Shape("pause", "resume at a time", flow(
        "Pause_Alarm",
        Wait(name="Hold", label="Hold", default_next="After", wait_events=[
            WaitEvent(name="Resume", label="When due", next="After",
                      input_parameters=[InputAssignment(
                          name="AlarmTime",
                          value=Value(element_reference="v_When"))])]),
        Assignment(name="After", label="After", items=[
            AssignmentItem(to_reference="v_Text",
                           value=Value(string_value="done"))]),
        variables=[WHEN, TEXT],
    ), "only a plain autolaunched flow may pause"),
    Shape("pause", "resume from a date on a record", flow(
        "Pause_DateRef",
        Wait(name="Hold", label="Hold", default_next="After", wait_events=[
            WaitEvent(name="From_Close", label="3 days from close",
                      event_type="DateRefAlarmEvent", next="After",
                      input_parameters=[
                          InputAssignment(name="SalesforceObject",
                                          value=Value(string_value="Opportunity")),
                          InputAssignment(name="BaseDateTimeFieldName",
                                          value=Value(string_value="CloseDate")),
                          InputAssignment(name="RecordId",
                                          value=Value(element_reference="v_Id")),
                          InputAssignment(name="TimeOffset",
                                          value=Value(number_value=3)),
                          InputAssignment(name="TimeOffsetUnit",
                                          value=Value(string_value="Days")),
                      ])]),
        Assignment(name="After", label="After", items=[
            AssignmentItem(to_reference="v_Text",
                           value=Value(string_value="done"))]),
        variables=[THE_ID, TEXT],
    )),

    # ---- Other elements ----------------------------------------------------
    Shape("elements", "a loop", flow(
        "Loop_Over",
        Loop(name="Each", label="Each", collection_reference="v_Accounts",
             first_element="Body", next=None),
        Assignment(name="Body", label="Body", next="Each", items=[
            AssignmentItem(to_reference="v_Count", operator="Add",
                           value=Value(number_value=1))]),
        variables=[ACCOUNTS, COUNT],
    )),
    # emailSimple rather than an email alert: an alert is a separate piece of
    # metadata whose name differs per org, so that case failed in any org but
    # the one it was written in - and a check that always fails is a check
    # everyone learns to skip. Send Email is standard everywhere.
    Shape("elements", "a loop with its own variable", flow(
        "Loop_Named_Var",
        Loop(name="Each", label="Each", collection_reference="v_Accounts",
             assign_next_value_to_reference="v_One", first_element="Body",
             next=None),
        Assignment(name="Body", label="Body", next="Each", items=[
            AssignmentItem(to_reference="v_Text",
                           value=Value(element_reference="v_One.Name"))]),
        variables=[ACCOUNTS, ONE_ACCOUNT, TEXT],
    ), "the older loop style, and still the only way to change the item"),

    Shape("elements", "a Send Email action", triggered(
        "Action_Email",
        ActionCall(name="Notify", label="Notify", action_name="emailSimple",
                   action_type="emailSimple", input_parameters=[
                       InputAssignment(name="emailSubject",
                                       value=Value(string_value="Heads up")),
                       InputAssignment(name="emailBody",
                                       value=Value(string_value="A deal moved.")),
                       InputAssignment(
                           name="emailAddresses",
                           value=Value(element_reference="$Record.Account.Owner.Email")),
                   ]),
    )),
]


# --------------------------------------------------------------------------
# Guards - what the IR refuses, and what the org would have done
# --------------------------------------------------------------------------

GUARDS: List[Guard] = [
    Guard("logic", "a condition number past the end",
          lambda: Outcome(name="Yes", label="Yes", condition_logic="1 AND 4",
                          conditions=[Condition(left="v", operator="EqualTo",
                                                right=Value(number_value=n))
                                      for n in (1, 2, 3)]),
          org="deploys, and evaluates the wrong branch"),
    Guard("logic", "an unclosed bracket",
          lambda: Outcome(name="Yes", label="Yes", condition_logic="1 OR (2 AND 3",
                          conditions=[Condition(left="v", operator="EqualTo",
                                                right=Value(number_value=n))
                                      for n in (1, 2, 3)]),
          org="deploys - it accepted the literal string 'banana' too"),

    Guard("components", "an output assigned to a variable that does not exist",
          lambda: screen_flow("X", Screen(name="Ask", label="Ask", fields=[
              slider(store_output_automatically=False,
                     output_parameters=[ComponentOutput(
                         name="value", assign_to_reference="v_Nope")])])),
          org="deploys, runs the component, and discards the value"),
    Guard("components", "both ways of returning a value at once",
          lambda: slider(store_output_automatically=True,
                         output_parameters=[ComponentOutput(
                             name="value", assign_to_reference="v_Count")]),
          org="rejects it, quoted verbatim in the IR's message"),

    Guard("records", "a field assigned into a variable that does not exist",
          lambda: flow("X", GetRecords(
              name="Get", label="Get", object="Account",
              store_output_automatically=False, output_assignments=[
                  OutputAssignment(field="Name",
                                   assign_to_reference="v_Nope")])),
          org="deploys, reads the field, and drops it"),
    Guard("records", "assigning fields and keeping the record too",
          lambda: GetRecords(name="Get", label="Get", object="Account",
                             store_output_automatically=True,
                             output_assignments=[OutputAssignment(
                                 field="Name", assign_to_reference="v_Text")]),
          org="rejects it, quoted verbatim in the IR's message"),
    Guard("records", "create asking for the Id twice over",
          lambda: RecordCreate(name="Make", label="Make", object="Task",
                               fields=[FieldValue(
                                   field="Subject",
                                   value=Value(string_value="x"))],
                               assign_record_id_to_reference="v_Text",
                               store_output_automatically=True),
          org="rejects it, quoted verbatim in the IR's message"),

    Guard("paths", "a path counting from a field it does not name",
          lambda: ScheduledPath(name="P", next="Later", offset_number=2,
                                offset_unit="Days", time_source="RecordField"),
          org="deploys with nothing to count from"),
    Guard("paths", "an offset with no unit",
          lambda: ScheduledPath(name="P", next="Later", offset_number=2,
                                time_source="RecordTriggerEvent"),
          org="deploys; 2 of what is anyone's guess"),
    Guard("paths", "a scheduled path on a before-save trigger",
          lambda: Start(next="Mark", object="Opportunity",
                        record_trigger_type="Update",
                        trigger_type="RecordBeforeSave",
                        scheduled_paths=[ScheduledPath(
                            name="P", next="Later", offset_number=1,
                            offset_unit="Days",
                            time_source="RecordTriggerEvent")]),
          org="rejects it - nothing is committed yet to come back to"),

    Guard("pause", "a Pause with no time to resume at",
          lambda: WaitEvent(name="R", next="After"),
          org="deploys, and the flow never resumes"),
    Guard("pause", "a Pause whose parameter is misspelled",
          lambda: WaitEvent(name="R", next="After", input_parameters=[
              InputAssignment(name="AlarmTimeX",
                              value=Value(element_reference="v_When"))]),
          org="deploys - it took AlarmTimeX without a word"),
    Guard("pause", "a Pause in a record-triggered flow",
          lambda: triggered("X", Wait(name="Hold", label="Hold")),
          org="rejects it, and the IR points at scheduled paths instead"),
    Guard("pause", "a Pause in a screen flow",
          lambda: screen_flow("X", Wait(name="Hold", label="Hold")),
          org="rejects it"),

    Guard("screens", "visibility reading a field that does not exist",
          lambda: screen_flow("X", Screen(name="Ask", label="Ask", fields=[
              number_field("Quantity", visibility=VisibilityRule(conditions=[
                  Condition(left="No_Such_Field", operator="EqualTo",
                            right=Value(string_value="Red"))]))])),
          org="deploys, and the field then never appears"),
    Guard("screens", "a column with no width",
          lambda: ScreenField(name="Column_1", field_type="Region"),
          org="rejects it, quoted verbatim in the IR's message"),
    Guard("screens", "a section inside a column",
          lambda: column("Column_1", 12, section("Inner", column("C", 12))),
          org="rejects it - sections nest exactly two deep"),
    Guard("screens", "a validation rule on a DisplayText",
          lambda: ScreenField(name="Intro", field_type="DisplayText",
                              field_text="<p>Hi</p>",
                              validation=ValidationRule(
                                  error_message="No.",
                                  formula_expression="true")),
          org="rejects it"),
    Guard("screens", "a screen in a flow nobody is watching",
          lambda: flow("X", Screen(name="Ask", label="Ask")),
          org="rejects it"),
    Guard("screens", "a picker offering an option nothing defines",
          lambda: screen_flow("X", Screen(name="Ask", label="Ask", fields=[
              ScreenField(name="Colour", field_type="RadioButtons",
                          field_text="Pick", data_type="String",
                          choice_references=["Undefined_Choice"])])),
          org="reports it naming only the screen"),

    Guard("flow", "a connector pointing at nothing",
          lambda: flow("X", mark_hot("Mark"),
                       start=Start(next="No_Such_Element")),
          org="rejects it"),
    Guard("flow", "two things sharing one name",
          lambda: screen_flow(
              "X",
              Screen(name="Ask", label="Ask", fields=[
                  ScreenField(name="Total", field_type="InputField",
                              field_text="Total", data_type="Number")]),
              variables=[Variable(name="Total", data_type="Number")]),
          org="rejects it - {!Total} can only mean one thing"),
]


# --------------------------------------------------------------------------
# Running
# --------------------------------------------------------------------------


def survives_round_trip(f: Flow) -> bool:
    """
    Compared by element name rather than list order. The XML groups elements by
    tag, so the order that comes back is an artefact of the format - the same
    exemption `tests/test_roundtrip.py` makes.
    """
    before = f.model_dump()
    after = parse_flow(generate(f), api_name=f.api_name).model_dump()
    if {k: v for k, v in before.items() if k != "elements"} != {
            k: v for k, v in after.items() if k != "elements"}:
        return False
    return ({e["name"]: e for e in before["elements"]}
            == {e["name"]: e for e in after["elements"]})


def run_guards(only: str) -> int:
    failures = 0
    group = ""
    for guard in GUARDS:
        if only and only not in f"{guard.group} {guard.name}".lower():
            continue
        if guard.group != group:
            group = guard.group
            print(f"\n  {group}")
        try:
            guard.build()
        except Exception:
            print(f"    ok    {guard.name}")
            print(f"            the org {guard.org}")
        else:
            failures += 1
            print(f"    FAIL  {guard.name} - the IR no longer refuses this")
    return failures


async def run_shapes(org_alias: str, only: str) -> int:
    from flowtool.orgs import SfCliError, get_org
    from flowtool.sfdc import validate_flow

    try:
        org = get_org(org_alias)
    except SfCliError as problem:
        print(f"\n  cannot reach an org: {problem}")
        return 1

    print(f"\n  org: {org.alias or org.username}")
    chosen = [
        shape for shape in SHAPES
        if not only or only in f"{shape.group} {shape.name}".lower()
    ]

    # Each case is a full round trip to Salesforce, and one at a time this took
    # over a minute and a half - long enough that nobody runs it. A handful at
    # once rather than all of them: these are somebody's real org, and a burst
    # of thirty validations is not a neighbourly thing to do to it.
    limit = asyncio.Semaphore(5)

    async def check(shape: Shape):
        trip = survives_round_trip(shape.flow)
        async with limit:
            result = await validate_flow(
                org.instance_url, org.access_token, shape.flow.api_name,
                generate(shape.flow), check_only=True,
            )
        return shape, trip, result

    # Results are printed in the order they are written, not the order they
    # finish, so the output reads the same every run.
    done = await asyncio.gather(*(check(shape) for shape in chosen))

    failures = 0
    group = ""
    for shape, trip, result in done:
        if shape.group != group:
            group = shape.group
            print(f"\n  {group}")
        ok = result.success and trip
        failures += 0 if ok else 1
        print(f"    {'ok  ' if ok else 'FAIL'}  {shape.name}")
        if shape.note:
            print(f"            {shape.note}")
        if not trip:
            print("            ROUND TRIP LOST SOMETHING")
        for problem in result.failures[:2]:
            print(f"            {problem}")
        if result.error_message and not result.failures:
            print(f"            {result.error_message[:150]}")
    return failures


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Check every metadata shape this tool emits against an org."
    )
    parser.add_argument("--org", metavar="ALIAS", nargs="?", const="",
                        help="validate the shapes against this org (checkOnly)")
    parser.add_argument("--only", default="",
                        help="run only cases whose group or name contains this")
    parser.add_argument("--list", action="store_true",
                        help="list the cases and exit")
    args = parser.parse_args()
    only = args.only.lower()

    if args.list:
        for shape in SHAPES:
            print(f"  shape  {shape.group:12} {shape.name}")
        for guard in GUARDS:
            print(f"  guard  {guard.group:12} {guard.name}")
        return 0

    print(__doc__.strip().splitlines()[0])

    print("\nGuards - the IR must refuse these:")
    failures = run_guards(only)

    if args.org is None:
        print("\nShapes not checked: pass --org ALIAS to validate against an org.")
    else:
        print("\nShapes - the org must accept these:")
        failures += asyncio.run(run_shapes(args.org, only))

    print()
    if failures:
        print(f"{failures} failed.")
        return 1
    print("All good.")
    return 0


if __name__ == "__main__":
    from flowtool.config import load_env
    from pathlib import Path

    load_env(Path(__file__).parent)
    sys.exit(main())
