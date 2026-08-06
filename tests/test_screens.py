"""
Screen flows, stage 1: text and typed input boxes.

Two things are being pinned down here. The first is the round trip - a screen
that comes out of an org has to go back in unchanged, including the chrome flags
nobody thinks about until Pause disappears from a flow that had it.

The second is the refusal. A screen carries far more than this build models:
pickers, radio groups, LWC components, visibility rules. Every one of those has
the same children as a plain input field, so nothing structural would notice
them - a RadioButtons field read as an InputField would draw as a text box and
deploy as one, silently replacing the picker. They are refused by name instead.
"""

import xml.etree.ElementTree as ET

import pytest
from pydantic import ValidationError

from flowtool.ir import (
    Assignment,
    AssignmentItem,
    Flow,
    RecordUpdate,
    Screen,
    ScreenField,
    Start,
    Value,
    Variable,
)
from flowtool.mermaid import to_markdown, to_mermaid
from flowtool.parse import UnsupportedFlow, parse_flow
from flowtool.xmlgen import METADATA_NS, generate

NS = {"m": METADATA_NS}


def text_field(name="Intro", body="Welcome") -> ScreenField:
    return ScreenField(name=name, field_type="DisplayText", field_text=body)


def input_field(name="Customer_Email", data_type="String", required=True) -> ScreenField:
    return ScreenField(
        name=name, field_type="InputField", field_text=name.replace("_", " "),
        data_type=data_type, is_required=required,
    )


def screen_flow(*elements, **kwargs) -> Flow:
    return Flow(
        api_name="Ask_The_User", label="Ask the user",
        process_type="Flow",
        start=Start(next=elements[0].name),
        elements=list(elements),
        **kwargs,
    )


def one_screen(**screen_kwargs) -> Flow:
    screen_kwargs.setdefault("fields", [text_field(), input_field()])
    return screen_flow(Screen(name="Ask", label="Ask", **screen_kwargs))


# --------------------------------------------------------------------------
# The round trip
# --------------------------------------------------------------------------


# The same assertion the other element types are held to, including its one
# deliberate exemption: element order is an artefact of the XML grouping by tag,
# not something the flow means.
from test_roundtrip import assert_survives  # noqa: E402


class TestRoundTrip:
    def test_a_screen_with_text_and_an_input(self):
        assert_survives(one_screen())

    def test_a_screen_that_only_shows_text(self):
        assert_survives(one_screen(fields=[text_field()]))

    @pytest.mark.parametrize(
        "data_type", ["String", "Number", "Currency", "Date", "DateTime", "Boolean"]
    )
    def test_every_input_data_type(self, data_type):
        assert_survives(one_screen(
            fields=[input_field(name="Answer", data_type=data_type)]
        ))

    def test_a_large_text_area(self):
        assert_survives(one_screen(fields=[
            ScreenField(name="Notes", field_type="LargeTextArea",
                        field_text="Notes", is_required=False)
        ]))

    def test_field_order_is_preserved(self):
        """The order of `fields` is the order the user reads them in."""
        flow = one_screen(fields=[
            text_field("Step_One", "First"),
            input_field("Name_Field"),
            text_field("Step_Two", "Then"),
            input_field("Age_Field", data_type="Number", required=False),
        ])
        returned = parse_flow(generate(flow), api_name=flow.api_name)
        assert [f.name for f in returned.elements[0].fields] == [
            "Step_One", "Name_Field", "Step_Two", "Age_Field"
        ]

    @pytest.mark.parametrize("flag", [
        "allow_back", "allow_finish", "allow_pause", "show_header", "show_footer",
    ])
    def test_turning_off_any_chrome_flag_survives(self, flag):
        """
        These default to true. Reading them as absent and writing them back as
        true would quietly restore a Pause button an admin removed on purpose.
        """
        assert_survives(one_screen(**{flag: False}))

    def test_a_screen_in_a_longer_flow(self):
        assert_survives(screen_flow(
            Screen(name="Ask", label="Ask", fields=[input_field()], next="Save"),
            Assignment(
                name="Save", label="Save",
                items=[AssignmentItem(
                    to_reference="v_Email",
                    value=Value(element_reference="Customer_Email"),
                )],
            ),
            variables=[Variable(name="v_Email", data_type="String")],
        ))

    def test_a_screen_carries_its_description(self):
        assert_survives(one_screen(description="Asks the customer for an email."))


# --------------------------------------------------------------------------
# What the compiler emits
# --------------------------------------------------------------------------


class TestGeneratedXml:
    def test_the_process_type_says_screen_flow(self):
        root = ET.fromstring(generate(one_screen()))
        assert root.find("m:processType", NS).text == "Flow"

    def test_display_text_carries_no_data_type_and_no_required(self):
        root = ET.fromstring(generate(one_screen(fields=[text_field()])))
        field = root.find("m:screens/m:fields", NS)
        assert field.find("m:fieldType", NS).text == "DisplayText"
        assert field.find("m:dataType", NS) is None
        assert field.find("m:isRequired", NS) is None

    def test_an_input_carries_both(self):
        root = ET.fromstring(generate(one_screen(fields=[input_field()])))
        field = root.find("m:screens/m:fields", NS)
        assert field.find("m:dataType", NS).text == "String"
        assert field.find("m:isRequired", NS).text == "true"

    def test_the_screen_allowlists_cover_what_the_compiler_writes(self):
        """
        The same invariant test_roundtrip asserts for the other elements, at both
        levels a screen has. Anything written but not allowed back in would break
        the round trip the first time it was used.
        """
        from flowtool.parse import _ELEMENT_CHILDREN, _SCREEN_FIELD_CHILDREN

        root = ET.fromstring(generate(one_screen(
            description="note",
            fields=[text_field(), input_field(),
                    ScreenField(name="Notes", field_type="LargeTextArea",
                                field_text="Notes")],
        )))
        problems = []
        for node in root.findall("m:screens", NS):
            for child in node:
                tag = child.tag.split("}")[-1]
                if tag not in _ELEMENT_CHILDREN["screens"]:
                    problems.append(f"screens.{tag} is written but not readable")
            for field in node.findall("m:fields", NS):
                for child in field:
                    tag = child.tag.split("}")[-1]
                    if tag not in _SCREEN_FIELD_CHILDREN:
                        problems.append(f"screens.fields.{tag} is written but not readable")
        assert not problems, problems


# --------------------------------------------------------------------------
# What the IR refuses to build
# --------------------------------------------------------------------------


class TestTheIrRefuses:
    def test_a_screen_needs_a_screen_flow(self):
        with pytest.raises(ValidationError, match="need process_type 'Flow'"):
            Flow(
                api_name="Wrong", label="Wrong",
                start=Start(next="Ask"),
                elements=[Screen(name="Ask", label="Ask", fields=[input_field()])],
            )

    def test_a_screen_flow_cannot_be_record_triggered(self):
        with pytest.raises(ValidationError, match="launched by a user"):
            Flow(
                api_name="Wrong", label="Wrong",
                process_type="Flow",
                start=Start(next="Ask", object="Account",
                            record_trigger_type="Create",
                            trigger_type="RecordAfterSave"),
                elements=[Screen(name="Ask", label="Ask", fields=[input_field()])],
            )

    def test_an_input_field_needs_a_data_type(self):
        with pytest.raises(ValidationError, match="needs a data_type"):
            ScreenField(name="X", field_type="InputField", field_text="X")

    def test_display_text_carries_no_data_type(self):
        with pytest.raises(ValidationError, match="carries no"):
            ScreenField(name="X", field_type="DisplayText", field_text="X",
                        data_type="String")

    def test_display_text_cannot_be_required(self):
        with pytest.raises(ValidationError, match="cannot be required"):
            ScreenField(name="X", field_type="DisplayText", field_text="X",
                        is_required=True)

    def test_a_screen_field_name_must_be_an_api_name(self):
        with pytest.raises(ValidationError, match="screen field name"):
            ScreenField(name="Customer Email", field_type="LargeTextArea",
                        field_text="Email")

    def test_a_field_type_this_build_does_not_have_is_not_representable(self):
        with pytest.raises(ValidationError):
            ScreenField(name="Pick", field_type="RadioButtons", field_text="Pick")


class TestOneNamespace:
    """
    A screen input is read by its own name - `{!Customer_Email}` - so it shares
    the namespace with elements and variables and the reference has to be
    unambiguous.
    """

    def test_a_field_cannot_share_a_variable_name(self):
        with pytest.raises(ValidationError, match="share one namespace"):
            screen_flow(
                Screen(name="Ask", label="Ask", fields=[input_field("Email")]),
                variables=[Variable(name="Email", data_type="String")],
            )

    def test_a_field_cannot_share_an_element_name(self):
        with pytest.raises(ValidationError, match="share one namespace"):
            screen_flow(
                Screen(name="Ask", label="Ask", fields=[input_field("Save")],
                       next="Save"),
                RecordUpdate(name="Save", label="Save", input_reference="v_Rec"),
            )

    def test_two_fields_on_different_screens_cannot_share_a_name(self):
        with pytest.raises(ValidationError, match="share one namespace"):
            screen_flow(
                Screen(name="First", label="First", fields=[input_field("Email")],
                       next="Second"),
                Screen(name="Second", label="Second", fields=[input_field("Email")]),
            )

    def test_the_message_names_both_owners(self):
        with pytest.raises(ValidationError) as caught:
            screen_flow(
                Screen(name="Ask", label="Ask", fields=[input_field("Email")]),
                variables=[Variable(name="Email", data_type="String")],
            )
        message = str(caught.value)
        assert "'Email'" in message
        assert "a field on screen Ask" in message
        assert "a variable" in message


# --------------------------------------------------------------------------
# What the parser refuses to read
# --------------------------------------------------------------------------


def org_xml(fields: str, screen_extra: str = "") -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Flow xmlns="http://soap.sforce.com/2006/04/metadata">'
        "<apiVersion>62.0</apiVersion><label>Ask</label>"
        "<processType>Flow</processType><status>Draft</status>"
        f"<screens><name>Ask</name><label>Ask</label>{screen_extra}{fields}</screens>"
        "<start><connector><targetReference>Ask</targetReference></connector></start>"
        "</Flow>"
    )


PLAIN_FIELD = (
    "<fields><name>Email</name><dataType>String</dataType>"
    "<fieldText>Email</fieldText><fieldType>InputField</fieldType>"
    "<isRequired>true</isRequired></fields>"
)


class TestTheParserRefuses:
    def test_a_plain_screen_flow_from_an_org_parses(self):
        flow = parse_flow(org_xml(PLAIN_FIELD), api_name="Ask")
        assert flow.process_type == "Flow"
        screen = flow.elements[0]
        assert [f.name for f in screen.fields] == ["Email"]
        assert screen.fields[0].data_type == "String"

    @pytest.mark.parametrize("kind", [
        "RadioButtons", "DropdownBox", "MultiSelectCheckboxes", "ComponentInstance",
        "PasswordField", "RegionContainer",
    ])
    def test_a_field_type_this_build_lacks_is_refused_by_name(self, kind):
        """
        These have exactly the same children as an InputField, so nothing
        structural notices them. Without this check a picker would be read as a
        text box, drawn as one, and deployed as one.
        """
        xml = org_xml(
            f"<fields><name>Pick</name><fieldText>Pick</fieldText>"
            f"<fieldType>{kind}</fieldType></fields>"
        )
        with pytest.raises(UnsupportedFlow) as caught:
            parse_flow(xml, api_name="Ask")
        assert f"screen_field:{kind}" in caught.value.codes
        assert "Ask.Pick" in str(caught.value)

    @pytest.mark.parametrize("tag,expected", [
        ("choiceReferences", "a choice picker"),
        ("extensionName", "a custom LWC or Aura component"),
        ("visibilityRule", "conditional visibility"),
        ("defaultValue", "a prefilled default"),
        ("validationRule", "a validation rule"),
        ("helpText", "help text"),
    ])
    def test_extras_on_a_field_are_refused(self, tag, expected):
        xml = org_xml(PLAIN_FIELD.replace("</fields>", f"<{tag}>x</{tag}></fields>"))
        with pytest.raises(UnsupportedFlow) as caught:
            parse_flow(xml, api_name="Ask")
        assert expected in str(caught.value)
        assert "Ask.Email" in str(caught.value), "the refusal must name the field"

    @pytest.mark.parametrize("tag", [
        "pausedText", "nextOrFinishButtonLabel", "backButtonLabel", "helpText",
    ])
    def test_extras_on_the_screen_are_refused(self, tag):
        with pytest.raises(UnsupportedFlow) as caught:
            parse_flow(org_xml(PLAIN_FIELD, screen_extra=f"<{tag}>x</{tag}>"),
                       api_name="Ask")
        assert "Ask" in str(caught.value)

    def test_choices_and_choice_sets_are_still_refused(self):
        """Stage 2. A build with only stage 1 must say so rather than approximate."""
        for tag in ("choices", "dynamicChoiceSets"):
            xml = org_xml(PLAIN_FIELD).replace(
                "<start>", f"<{tag}><name>C</name></{tag}><start>"
            )
            with pytest.raises(UnsupportedFlow):
                parse_flow(xml, api_name="Ask")

    def test_a_screen_in_an_autolaunched_flow_is_reported_not_crashed(self):
        """
        Salesforce would not have deployed this, so it means the org has
        something the IR reads differently. It is a gap, not a traceback.
        """
        xml = org_xml(PLAIN_FIELD).replace(
            "<processType>Flow</processType>",
            "<processType>AutoLaunchedFlow</processType>",
        )
        with pytest.raises(UnsupportedFlow) as caught:
            parse_flow(xml, api_name="Ask")
        assert "ir_mismatch" in caught.value.codes


# --------------------------------------------------------------------------
# What the user is shown before approving
# --------------------------------------------------------------------------


class TestWhatTheUserSees:
    def test_a_screen_is_drawn_as_its_own_shape(self):
        diagram = to_mermaid(one_screen())
        assert 'Ask{{"' in diagram, "a screen must not look like any other element"

    def test_the_caption_names_what_is_asked_for(self):
        diagram = to_mermaid(one_screen())
        assert "Asks for Customer_Email" in diagram

    def test_a_text_only_screen_says_so(self):
        assert "Shows text" in to_mermaid(one_screen(fields=[text_field()]))

    def test_the_start_says_a_user_runs_it(self):
        """
        A screen flow drawn as "Autolaunched" would tell the approver the
        opposite of how it runs.
        """
        diagram = to_mermaid(one_screen())
        assert "Run by a user" in diagram
        assert "Autolaunched" not in diagram

    def test_the_documentation_lists_the_fields(self):
        markdown = to_markdown(one_screen())
        assert "Screen flow" in markdown
        assert "`Customer_Email` (input, String, required)" in markdown
        assert "`Intro` (text)" in markdown
