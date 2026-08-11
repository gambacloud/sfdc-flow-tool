"""
Screen flows, stage 2: options to pick from.

Choices and choice sets are resources, not elements. Nothing connects to them,
so the reachability check that guards every other reference says nothing about
them - a screen field naming an option that was never defined is structurally
fine and shows the user an empty list. That reference is checked here instead,
and it is the main thing this stage adds beyond reading the XML.

A choice set draws its options from records or from a picklist. Those two carry
completely different fields, and a set that mixed them would be read as one
Salesforce cannot build, so the IR holds one shape or the other and never both.
"""

import xml.etree.ElementTree as ET

import pytest
from pydantic import ValidationError

from flowtool.ir import (
    Choice,
    DynamicChoiceSet,
    Flow,
    RecordFilter,
    Screen,
    ScreenField,
    Start,
    Value,
    Variable,
)
from flowtool.mermaid import to_markdown
from flowtool.parse import UnsupportedFlow, parse_flow
from flowtool.xmlgen import METADATA_NS, generate
from test_roundtrip import assert_survives

NS = {"m": METADATA_NS}


def picker(name="Colour", field_type="RadioButtons", data_type="String",
           references=("Red", "Blue")) -> ScreenField:
    return ScreenField(
        name=name, field_type=field_type, field_text="Pick one",
        data_type=data_type, is_required=True, choice_references=list(references),
    )


def red_and_blue():
    return [
        Choice(name="Red", choice_text="Red"),
        Choice(name="Blue", choice_text="Blue"),
    ]


def flow_with(*fields, choices=None, choice_sets=None, **kwargs) -> Flow:
    return Flow(
        api_name="Pick_One", label="Pick one",
        process_type="Flow",
        start=Start(next="Ask"),
        elements=[Screen(name="Ask", label="Ask", fields=list(fields))],
        choices=red_and_blue() if choices is None else choices,
        dynamic_choice_sets=choice_sets or [],
        **kwargs,
    )


ACCOUNT_SET = DynamicChoiceSet(
    name="Account_Options", object="Account",
    display_field="Name", value_field="Id",
)
INDUSTRY_SET = DynamicChoiceSet(
    name="Industry_Options", data_type="Picklist",
    picklist_object="Account", picklist_field="Industry",
)


# --------------------------------------------------------------------------
# The round trip
# --------------------------------------------------------------------------


class TestRoundTrip:
    @pytest.mark.parametrize("field_type,data_type", [
        ("RadioButtons", "String"),
        ("DropdownBox", "String"),
        ("DropdownBox", "Number"),
        ("MultiSelectCheckboxes", "String"),
        ("MultiSelectPicklist", "String"),
    ])
    def test_every_picker_type(self, field_type, data_type):
        assert_survives(flow_with(picker(field_type=field_type, data_type=data_type)))

    def test_a_choice_that_stores_something_other_than_its_text(self):
        assert_survives(flow_with(
            picker(references=("Yes_Please",)),
            choices=[Choice(name="Yes_Please", choice_text="Yes please",
                            value=Value(boolean_value=True), data_type="Boolean")],
        ))

    @pytest.mark.parametrize("data_type", [
        "String", "Number", "Currency", "Date", "DateTime", "Boolean",
    ])
    def test_every_choice_data_type(self, data_type):
        assert_survives(flow_with(
            picker(references=("Only",)),
            choices=[Choice(name="Only", choice_text="Only", data_type=data_type)],
        ))

    def test_a_record_choice_set(self):
        assert_survives(flow_with(
            picker(references=("Account_Options",)),
            choices=[], choice_sets=[ACCOUNT_SET],
        ))

    def test_a_record_choice_set_with_everything_on_it(self):
        assert_survives(flow_with(
            picker(references=("Account_Options",)),
            choices=[],
            choice_sets=[DynamicChoiceSet(
                name="Account_Options", object="Account",
                display_field="Name", value_field="Id",
                filters=[RecordFilter(field="Rating", operator="EqualTo",
                                      value=Value(string_value="Hot"))],
                filter_logic="and", sort_field="Name", sort_order="Desc", limit=25,
            )],
        ))

    def test_a_picklist_choice_set(self):
        assert_survives(flow_with(
            picker(references=("Industry_Options",)),
            choices=[], choice_sets=[INDUSTRY_SET],
        ))

    def test_choices_and_a_choice_set_on_the_same_field(self):
        """Salesforce allows mixing fixed options with generated ones."""
        assert_survives(flow_with(
            picker(references=("Red", "Blue", "Account_Options")),
            choice_sets=[ACCOUNT_SET],
        ))

    def test_reference_order_is_preserved(self):
        """The order of choice_references is the order the user sees them in."""
        flow = flow_with(picker(references=("Blue", "Red")))
        returned = parse_flow(generate(flow), api_name=flow.api_name)
        assert returned.elements[0].fields[0].choice_references == ["Blue", "Red"]

    def test_a_flow_with_no_choices_at_all_still_round_trips(self):
        assert_survives(flow_with(
            ScreenField(name="Note", field_type="DisplayText", field_text="Hi"),
            choices=[],
        ))


# --------------------------------------------------------------------------
# What the compiler emits
# --------------------------------------------------------------------------


class TestGeneratedXml:
    def test_choices_are_resources_not_elements(self):
        """No coordinates, no connector - nothing flows through a choice."""
        node = ET.fromstring(generate(flow_with(picker()))).find("m:choices", NS)
        assert node.find("m:locationX", NS) is None
        assert node.find("m:connector", NS) is None
        assert node.find("m:choiceText", NS).text == "Red"

    def test_a_field_names_its_choices(self):
        root = ET.fromstring(generate(flow_with(picker())))
        field = root.find("m:screens/m:fields", NS)
        assert [r.text for r in field.findall("m:choiceReferences", NS)] == [
            "Red", "Blue"
        ]

    def test_the_root_order_matches_what_salesforce_emits(self):
        """
        Alphabetical, which interleaves the resources among the elements. The
        Metadata API is picky about ordering in places, so this is not cosmetic.
        """
        root = ET.fromstring(generate(flow_with(
            picker(references=("Red", "Blue", "Account_Options")),
            choice_sets=[ACCOUNT_SET],
        )))
        tags = [child.tag.split("}")[-1] for child in root]
        assert tags.index("choices") < tags.index("dynamicChoiceSets")
        assert tags.index("dynamicChoiceSets") < tags.index("screens")

    def test_the_resource_allowlists_cover_what_the_compiler_writes(self):
        from flowtool.parse import _CHOICE_CHILDREN, _CHOICE_SET_CHILDREN

        root = ET.fromstring(generate(flow_with(
            picker(references=("Red", "Blue", "Account_Options", "Industry_Options")),
            choices=[Choice(name="Red", choice_text="Red",
                            value=Value(string_value="R")),
                     Choice(name="Blue", choice_text="Blue")],
            choice_sets=[
                DynamicChoiceSet(
                    name="Account_Options", object="Account", display_field="Name",
                    value_field="Id", sort_field="Name", sort_order="Asc", limit=5,
                    filters=[RecordFilter(field="Rating", operator="IsNull",
                                          value=Value(boolean_value=False))],
                ),
                INDUSTRY_SET,
            ],
        )))
        problems = []
        for tag, allowed in (("choices", _CHOICE_CHILDREN),
                             ("dynamicChoiceSets", _CHOICE_SET_CHILDREN)):
            for node in root.findall(f"m:{tag}", NS):
                for child in node:
                    name = child.tag.split("}")[-1]
                    if name not in allowed:
                        problems.append(f"{tag}.{name} is written but not readable")
        assert not problems, problems


# --------------------------------------------------------------------------
# The reference nothing else checks
# --------------------------------------------------------------------------


class TestUndefinedOptions:
    def test_a_field_offering_an_undefined_option_is_rejected(self):
        with pytest.raises(ValidationError, match="not defined"):
            flow_with(picker(references=("Red", "Green")))

    def test_the_message_names_the_field_and_what_does_exist(self):
        with pytest.raises(ValidationError) as caught:
            flow_with(picker(references=("Green",)))
        message = str(caught.value)
        assert "Ask.Colour offers 'Green'" in message
        assert "'Blue', 'Red'" in message, "must say what it could have meant"

    def test_a_choice_set_satisfies_the_reference_too(self):
        flow_with(picker(references=("Account_Options",)),
                  choices=[], choice_sets=[ACCOUNT_SET])

    def test_a_picker_with_no_options_is_rejected(self):
        """An empty list is not a question - the user has nothing to click."""
        with pytest.raises(ValidationError, match="at least one entry"):
            ScreenField(name="Colour", field_type="RadioButtons",
                        field_text="Pick", data_type="String")

    def test_a_text_box_cannot_carry_options(self):
        with pytest.raises(ValidationError, match="nowhere to show options"):
            ScreenField(name="Colour", field_type="InputField", field_text="Pick",
                        data_type="String", choice_references=["Red"])


class TestOneNamespace:
    def test_a_choice_cannot_share_a_variable_name(self):
        with pytest.raises(ValidationError, match="share one namespace"):
            flow_with(picker(), variables=[Variable(name="Red", data_type="String")])

    def test_a_choice_and_a_choice_set_cannot_share_a_name(self):
        with pytest.raises(ValidationError, match="share one namespace"):
            flow_with(
                picker(references=("Red",)),
                choices=[Choice(name="Red", choice_text="Red")],
                choice_sets=[DynamicChoiceSet(name="Red", object="Account",
                                              display_field="Name", value_field="Id")],
            )

    def test_a_choice_cannot_share_a_screen_field_name(self):
        with pytest.raises(ValidationError, match="share one namespace"):
            flow_with(
                picker(name="Colour", references=("Colour",)),
                choices=[Choice(name="Colour", choice_text="Colour")],
            )


# --------------------------------------------------------------------------
# Where a choice set gets its options
# --------------------------------------------------------------------------


class TestChoiceSetModes:
    def test_records_and_a_picklist_cannot_be_mixed(self):
        with pytest.raises(ValidationError, match="not more than one"):
            DynamicChoiceSet(name="Mixed", object="Account", display_field="Name",
                             value_field="Id", picklist_object="Account",
                             picklist_field="Industry")

    def test_a_record_set_needs_all_three_parts(self):
        with pytest.raises(ValidationError) as caught:
            DynamicChoiceSet(name="Partial", object="Account")
        assert "display_field" in str(caught.value)
        assert "value_field" in str(caught.value)

    def test_a_picklist_set_needs_both_parts(self):
        with pytest.raises(ValidationError, match="picklist_object and picklist_field"):
            DynamicChoiceSet(name="Partial", picklist_field="Industry")

    def test_a_set_with_no_source_at_all_is_rejected(self):
        with pytest.raises(ValidationError, match="needs a live query"):
            DynamicChoiceSet(name="Empty")


class TestDataTypes:
    """
    Both of these were assumed wrong first and corrected by the org under
    checkOnly, which is the only authority on them.
    """

    @pytest.mark.parametrize("field_type", [
        "RadioButtons", "DropdownBox", "MultiSelectCheckboxes", "MultiSelectPicklist",
    ])
    @pytest.mark.parametrize("bad", ["Picklist", "Multipicklist"])
    def test_a_screen_field_is_never_a_picklist_type(self, field_type, bad):
        """
        "The data type of 'Topics' can't be 'Multipicklist'." A screen field
        holds the type of one option - a multi-select joins its answers into a
        String. Picklist types belong to a choice set, not to a field.
        """
        with pytest.raises(ValidationError):
            ScreenField(name="Colours", field_type=field_type, field_text="Pick",
                        data_type=bad, choice_references=["Red"])

    @pytest.mark.parametrize("field_type", [
        "RadioButtons", "DropdownBox", "MultiSelectCheckboxes", "MultiSelectPicklist",
    ])
    def test_every_picker_takes_the_type_of_one_option(self, field_type):
        ScreenField(name="Colour", field_type=field_type, field_text="Pick",
                    data_type="String", choice_references=["Red"])

    @pytest.mark.parametrize("bad", ["String", "Number", "Boolean"])
    def test_a_picklist_choice_set_must_say_picklist(self, bad):
        """"The data type of 'Inds' can't be 'String'" - the org's own words."""
        with pytest.raises(ValidationError, match="data_type 'Picklist'"):
            DynamicChoiceSet(name="Inds", data_type=bad, picklist_object="Account",
                             picklist_field="Industry")

    @pytest.mark.parametrize("ok", ["Picklist", "Multipicklist"])
    def test_both_picklist_types_are_allowed(self, ok):
        # Multipicklist is valid, but only against a picklist field that is
        # itself multi-select - which only the org can know.
        DynamicChoiceSet(name="Inds", data_type=ok, picklist_object="Account",
                         picklist_field="Industry")

    def test_a_record_choice_set_keeps_the_type_of_the_stored_field(self):
        DynamicChoiceSet(name="Accounts", data_type="String", object="Account",
                         display_field="Name", value_field="Id")


# --------------------------------------------------------------------------
# Reading them back out of an org
# --------------------------------------------------------------------------


def org_xml(body: str, field_extra: str = "") -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Flow xmlns="http://soap.sforce.com/2006/04/metadata">'
        "<apiVersion>62.0</apiVersion><label>Ask</label>"
        "<processType>Flow</processType><status>Draft</status>"
        f"{body}"
        "<screens><name>Ask</name><label>Ask</label>"
        "<fields><name>Colour</name><choiceReferences>Red</choiceReferences>"
        "<dataType>String</dataType><fieldText>Pick</fieldText>"
        f"<fieldType>RadioButtons</fieldType><isRequired>true</isRequired>"
        f"{field_extra}</fields></screens>"
        "<start><connector><targetReference>Ask</targetReference></connector></start>"
        "</Flow>"
    )


RED = "<choices><name>Red</name><choiceText>Red</choiceText><dataType>String</dataType></choices>"


class TestParsing:
    def test_a_picker_from_an_org_parses(self):
        flow = parse_flow(org_xml(RED), api_name="Ask")
        assert [c.name for c in flow.choices] == ["Red"]
        assert flow.elements[0].fields[0].choice_references == ["Red"]

    def test_a_choice_that_lets_the_user_type_their_own_is_refused(self):
        """
        userInput turns a choice into a free-text box. Reading it as a plain
        option would draw a picker that does not match, and drop the box.
        """
        xml = org_xml(RED.replace(
            "</choices>", "<userInput><isRequired>true</isRequired></userInput></choices>"
        ))
        with pytest.raises(UnsupportedFlow) as caught:
            parse_flow(xml, api_name="Ask")
        assert "lets the user type their own answer" in str(caught.value)
        assert "child:userInput" in caught.value.codes

    def test_fields_copied_out_of_a_chosen_record_are_refused(self):
        xml = org_xml(
            "<dynamicChoiceSets><name>Red</name><dataType>String</dataType>"
            "<object>Account</object><displayField>Name</displayField>"
            "<valueField>Id</valueField>"
            "<outputAssignments><assignToReference>v</assignToReference>"
            "<field>Rating</field></outputAssignments></dynamicChoiceSets>"
        )
        with pytest.raises(UnsupportedFlow) as caught:
            parse_flow(xml, api_name="Ask")
        assert "child:outputAssignments" in caught.value.codes

    def test_options_built_from_a_collection_parse(self):
        # collectionReference/displayField/valueField: confirmed against the
        # org's own live Metadata API schema (describeValueType on
        # FlowDynamicChoiceSet) - real, but not found in any public sample flow.
        xml = org_xml(
            "<dynamicChoiceSets><name>Red</name><dataType>String</dataType>"
            "<collectionReference>v_Items</collectionReference>"
            "<displayField>Name</displayField><valueField>Id</valueField>"
            "</dynamicChoiceSets>"
        )
        flow = parse_flow(xml, api_name="Ask")
        choice_set = flow.dynamic_choice_sets[0]
        assert choice_set.collection_reference == "v_Items"
        assert choice_set.display_field == "Name"
        assert choice_set.value_field == "Id"

    def test_a_collection_choice_set_survives_a_round_trip(self):
        assert_survives(flow_with(
            picker(references=("Items",)),
            choices=[],
            choice_sets=[DynamicChoiceSet(
                name="Items", object=None, collection_reference="v_Items",
                display_field="Name", value_field="Id",
            )],
        ))

    def test_a_collection_choice_set_needs_display_and_value_fields(self):
        with pytest.raises(ValidationError) as caught:
            DynamicChoiceSet(name="Items", collection_reference="v_Items")
        assert "display_field" in str(caught.value)
        assert "value_field" in str(caught.value)

    def test_a_collection_reference_cannot_be_mixed_with_a_live_query(self):
        with pytest.raises(ValidationError, match="not more than one"):
            DynamicChoiceSet(
                name="Items", object="Account", collection_reference="v_Items",
                display_field="Name", value_field="Id",
            )

    def test_a_dangling_reference_in_an_org_flow_is_reported_not_crashed(self):
        """The screen names an option the flow never defines."""
        with pytest.raises(UnsupportedFlow) as caught:
            parse_flow(org_xml(""), api_name="Ask")
        assert "ir_mismatch" in caught.value.codes
        assert "not defined" in str(caught.value)


class TestWhatTheUserSees:
    def test_the_documentation_lists_what_a_picker_offers(self):
        markdown = to_markdown(flow_with(picker()))
        assert "## Options" in markdown
        assert '| `Red` | Choice | "Red" |' in markdown
        assert "`Colour` (radio buttons, String, required) from `Red`, `Blue`" in markdown

    def test_a_record_choice_set_says_where_its_options_come_from(self):
        markdown = to_markdown(flow_with(
            picker(references=("Account_Options",)), choices=[],
            choice_sets=[DynamicChoiceSet(
                name="Account_Options", object="Account", display_field="Name",
                value_field="Id", limit=5,
                filters=[RecordFilter(field="Rating", operator="EqualTo",
                                      value=Value(string_value="Hot"))],
            )],
        ))
        assert "one per `Account` record" in markdown
        assert "Rating = \"Hot\"" in markdown
        assert "first 5" in markdown

    def test_a_picklist_choice_set_says_which_picklist(self):
        markdown = to_markdown(flow_with(
            picker(references=("Industry_Options",)),
            choices=[], choice_sets=[INDUSTRY_SET],
        ))
        assert "the values of `Account.Industry`" in markdown

    def test_a_stored_value_that_differs_from_the_text_is_shown(self):
        markdown = to_markdown(flow_with(
            picker(references=("Yes_Please",)),
            choices=[Choice(name="Yes_Please", choice_text="Yes please",
                            data_type="Boolean", value=Value(boolean_value=True))],
        ))
        assert "stores true" in markdown
