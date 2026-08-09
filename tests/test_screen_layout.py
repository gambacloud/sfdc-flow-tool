"""
Screens that look like real forms: sections, columns, visibility, validation,
help text.

This is the change that made ScreenField a tree. A section holds columns and a
column holds fields, and every flow-level check written before this walked
`fields` exactly one level deep - so a field inside a column would have had its
name unchecked for collisions, its choices unresolved and its component outputs
unchecked for somewhere to land. Silently, and on the fields most likely to be
in a screen anyone actually built. TestNestedFieldsAreNotSecondClass is that.

The org draws the boundaries here more sharply than usual, and states them:

    A RegionContainer screen field can't be a child of a Region screen field.
    The "Column_1" Region screen field requires a width input parameter.
    The screen field of type DisplayText doesn't support validation rules.
    Required field is missing: errorMessage

What it does not check is the one that matters most: a visibility rule reading
a field that does not exist deploys without a word, and the field then never
appears - which looks exactly like a field somebody chose not to show.
"""

import pytest
from pydantic import ValidationError

from flowtool.ir import (
    Choice,
    ComponentOutput,
    Condition,
    Flow,
    InputAssignment,
    Screen,
    ScreenField,
    Start,
    ValidationRule,
    Value,
    Variable,
    VisibilityRule,
)
from flowtool.mermaid import to_markdown
from flowtool.parse import UnsupportedFlow, parse_flow
from flowtool.xmlgen import generate


def quantity(name="Quantity", **kwargs) -> ScreenField:
    fields = dict(name=name, field_type="InputField", field_text="How many?",
                  data_type="Number")
    fields.update(kwargs)
    return ScreenField(**fields)


def column(name, width=12, *fields) -> ScreenField:
    return ScreenField(
        name=name, field_type="Region", fields=list(fields),
        input_parameters=[InputAssignment(name="width",
                                          value=Value(string_value=str(width)))])


def section(name, *columns, **kwargs) -> ScreenField:
    fields = dict(name=name, field_type="RegionContainer",
                  region_container_type="SectionWithoutHeader",
                  fields=list(columns))
    fields.update(kwargs)
    return ScreenField(**fields)


COLOUR = ScreenField(name="Colour", field_type="RadioButtons",
                     field_text="Pick a colour", data_type="String",
                     choice_references=["Red"])
RED = Choice(name="Red", choice_text="Red")
SHOW_IF_RED = VisibilityRule(conditions=[
    Condition(left="Colour", operator="EqualTo", right=Value(string_value="Red"))])


def screen_flow(*fields, **kwargs) -> Flow:
    return Flow(
        api_name="Ask_Flow", label="Ask Flow", process_type="Flow",
        start=Start(next="Ask"),
        elements=[Screen(name="Ask", label="Ask", fields=list(fields))],
        **kwargs,
    )


def survives(f: Flow) -> bool:
    before = f.model_dump()
    after = parse_flow(generate(f), api_name=f.api_name).model_dump()
    if {k: v for k, v in before.items() if k != "elements"} != {
            k: v for k, v in after.items() if k != "elements"}:
        return False
    return ({e["name"]: e for e in before["elements"]}
            == {e["name"]: e for e in after["elements"]})


class TestRoundTrip:
    @pytest.mark.parametrize("field", [
        quantity(help_text="<p>Type a number.</p>"),
        quantity(validation=ValidationRule(error_message="Must be positive.",
                                           formula_expression="{!Quantity} > 0")),
        section("Section_1", column("Column_1", 12, quantity())),
        section("Section_1", column("Column_1", 6, quantity(name="Left")),
                column("Column_2", 6, quantity(name="Right"))),
        section("Section_1", column("Column_1", 12, quantity()),
                region_container_type="SectionWithHeader", field_text="Details"),
        section("Section_1", column("Column_1", 12)),
    ])
    def test_it_survives(self, field):
        assert survives(screen_flow(field))

    def test_visibility_survives(self):
        assert survives(screen_flow(COLOUR, quantity(visibility=SHOW_IF_RED),
                                    choices=[RED]))

    def test_a_field_inside_a_column_keeps_everything(self):
        """
        The whole point of the recursion. A field in a column has to be read
        and written exactly like one outside it.
        """
        inner = quantity(name="Left", help_text="<p>h</p>",
                         visibility=SHOW_IF_RED,
                         validation=ValidationRule(error_message="No.",
                                                   formula_expression="true"))
        flow = screen_flow(COLOUR, section("S", column("C", 12, inner)),
                           choices=[RED])
        returned = parse_flow(generate(flow), api_name=flow.api_name)
        back = returned.elements[0].fields[1].fields[0].fields[0]
        assert back.name == "Left"
        assert back.help_text == "<p>h</p>"
        assert back.visibility.conditions[0].left == "Colour"
        assert back.validation.error_message == "No."

    def test_column_order_is_kept(self):
        flow = screen_flow(section(
            "S", column("First", 4), column("Second", 4), column("Third", 4)))
        returned = parse_flow(generate(flow), api_name=flow.api_name)
        assert [c.name for c in returned.elements[0].fields[0].fields] == [
            "First", "Second", "Third"]


class TestTheOrgsRules:
    """Each of these is refused with the org's own sentence in the message."""

    def test_a_section_cannot_hold_a_field_directly(self):
        with pytest.raises(ValidationError, match="a section holds columns"):
            section("S", quantity())

    def test_a_section_cannot_go_inside_a_column(self):
        with pytest.raises(ValidationError, match="can't be a child of a Region"):
            column("C", 12, section("Inner", column("Deeper", 12)))

    def test_a_column_needs_a_width(self):
        with pytest.raises(ValidationError, match="requires a width"):
            ScreenField(name="Column_1", field_type="Region")

    def test_a_section_needs_its_type(self):
        with pytest.raises(ValidationError, match="region_container_type"):
            ScreenField(name="Section_1", field_type="RegionContainer")

    def test_a_header_needs_heading_text(self):
        with pytest.raises(ValidationError, match="fieldText"):
            ScreenField(name="Section_1", field_type="RegionContainer",
                        region_container_type="SectionWithHeader")

    def test_display_text_cannot_be_validated(self):
        with pytest.raises(ValidationError, match="doesn't support validation"):
            ScreenField(name="Intro", field_type="DisplayText",
                        field_text="<p>Hi</p>",
                        validation=ValidationRule(error_message="No.",
                                                  formula_expression="true"))

    def test_an_ordinary_field_cannot_hold_fields(self):
        with pytest.raises(ValidationError, match="holds a value, not other"):
            quantity(fields=[quantity(name="Inner")])

    def test_a_validation_rule_needs_both_halves(self):
        with pytest.raises(ValidationError):
            ValidationRule(formula_expression="true")
        with pytest.raises(ValidationError):
            ValidationRule(error_message="No.")


class TestVisibilityReferences:
    """
    The org accepts a rule reading a field that does not exist. Verified: the
    flow deploys, the condition is never true, and the field never appears.
    """

    def test_reading_something_undefined_is_refused(self):
        with pytest.raises(ValidationError, match="does not define"):
            screen_flow(quantity(visibility=VisibilityRule(conditions=[
                Condition(left="No_Such_Field", operator="EqualTo",
                          right=Value(string_value="x"))])))

    def test_reading_another_screen_field_is_fine(self):
        assert screen_flow(COLOUR, quantity(visibility=SHOW_IF_RED),
                           choices=[RED]).elements

    def test_reading_a_variable_is_fine(self):
        flow = screen_flow(
            quantity(visibility=VisibilityRule(conditions=[
                Condition(left="v_Mode", operator="EqualTo",
                          right=Value(string_value="full"))])),
            variables=[Variable(name="v_Mode", data_type="String")])
        assert flow.elements

    def test_reading_a_global_is_fine(self):
        """`$Record` and friends are not defined by the flow and never will be."""
        flow = screen_flow(quantity(visibility=VisibilityRule(conditions=[
            Condition(left="$User.Id", operator="IsNull",
                      right=Value(boolean_value=False))])))
        assert flow.elements

    def test_a_field_inside_a_column_counts_as_defined(self):
        """
        The check walks the tree on both sides - the rule may live in a column
        and the field it reads may live in another one.
        """
        flow = screen_flow(
            section("S",
                    column("C1", 6, COLOUR),
                    column("C2", 6, quantity(visibility=SHOW_IF_RED))),
            choices=[RED])
        assert flow.elements

    def test_out_of_range_custom_logic_is_caught(self):
        with pytest.raises(ValidationError, match="only 1"):
            VisibilityRule(condition_logic="1 AND 2", conditions=[
                Condition(left="Colour", operator="EqualTo",
                          right=Value(string_value="Red"))])


class TestNestedFieldsAreNotSecondClass:
    """
    Every flow-level check walked `fields` one level deep before sections
    existed. A field inside a column had to start being reached by all of them
    at once, and each of these is one that would otherwise have gone quiet.
    """

    def test_a_nested_name_still_has_to_be_unique(self):
        with pytest.raises(ValidationError, match="can be used once") as caught:
            screen_flow(
                section("S", column("C", 12, quantity(name="Total"))),
                variables=[Variable(name="Total", data_type="Number")])
        # It found the field three levels down and said where it was.
        assert "a field on screen Ask" in str(caught.value)

    def test_a_nested_picker_still_has_its_choices_resolved(self):
        with pytest.raises(ValidationError, match="not defined"):
            screen_flow(section("S", column("C", 12, ScreenField(
                name="Colour", field_type="RadioButtons", field_text="Pick",
                data_type="String", choice_references=["Undefined"]))))

    def test_a_nested_component_output_still_needs_a_variable(self):
        with pytest.raises(ValidationError, match="do not exist"):
            screen_flow(section("S", column("C", 12, ScreenField(
                name="picker", field_type="ComponentInstance",
                extension_name="c:thing",
                output_parameters=[ComponentOutput(
                    name="value", assign_to_reference="v_Missing")]))))

    def test_all_fields_finds_every_level(self):
        flow = screen_flow(COLOUR, section(
            "S", column("C1", 6, quantity(name="Left")),
            column("C2", 6, quantity(name="Right"))), choices=[RED])
        assert {f.name for f in flow.elements[0].all_fields()} == {
            "Colour", "S", "C1", "C2", "Left", "Right"}


class TestTheParser:
    def org_xml(self, fields: str, extra: str = "") -> str:
        return (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Flow xmlns="http://soap.sforce.com/2006/04/metadata">'
            "<apiVersion>62.0</apiVersion><label>X</label>"
            "<processType>Flow</processType><status>Draft</status>"
            f"<screens><name>Ask</name><label>Ask</label>{fields}</screens>"
            "<start><connector><targetReference>Ask</targetReference></connector>"
            f"</start>{extra}</Flow>"
        )

    SECTION = (
        "<fields><name>Section_1</name>"
        "<fields><name>Column_1</name><fieldType>Region</fieldType>"
        "<inputParameters><name>width</name>"
        "<value><stringValue>12</stringValue></value></inputParameters>"
        "<fields><name>Quantity</name><dataType>Number</dataType>"
        "<fieldText>How many?</fieldText><fieldType>InputField</fieldType>"
        "<isRequired>false</isRequired></fields>"
        "</fields>"
        "<fieldType>RegionContainer</fieldType><isRequired>false</isRequired>"
        "<regionContainerType>SectionWithoutHeader</regionContainerType>"
        "</fields>"
    )

    def test_a_section_from_an_org_parses(self):
        flow = parse_flow(self.org_xml(self.SECTION), api_name="X")
        outer = flow.elements[0].fields[0]
        assert outer.field_type == "RegionContainer"
        assert outer.fields[0].field_type == "Region"
        assert outer.fields[0].fields[0].name == "Quantity"

    def test_an_unknown_child_of_a_nested_field_is_refused(self):
        """
        The allowlist follows the nesting too. A field inside a column is where
        an unmodelled attribute is least likely to be noticed and just as
        likely to be lost on the next deploy.
        """
        with pytest.raises(UnsupportedFlow) as caught:
            parse_flow(self.org_xml(self.SECTION.replace(
                "<isRequired>false</isRequired></fields>"
                "</fields><fieldType>RegionContainer</fieldType>",
                "<isRequired>false</isRequired><somethingNew>x</somethingNew>"
                "</fields></fields><fieldType>RegionContainer</fieldType>",
            )), api_name="X")
        assert "Quantity" in str(caught.value)


class TestWhatTheUserSees:
    def test_the_layout_is_indented(self):
        markdown = to_markdown(screen_flow(section(
            "S", column("C", 12, quantity()))), include_diagram=False)
        assert "(section)" in markdown
        assert "(column, width 12)" in markdown

    def test_a_visibility_rule_is_spelled_out(self):
        markdown = to_markdown(
            screen_flow(COLOUR, quantity(visibility=SHOW_IF_RED), choices=[RED]),
            include_diagram=False)
        assert 'shown when Colour = "Red"' in markdown

    def test_a_validation_rule_shows_both_halves(self):
        markdown = to_markdown(screen_flow(quantity(
            validation=ValidationRule(error_message="Must be positive.",
                                      formula_expression="{!Quantity} > 0"))),
            include_diagram=False)
        assert "{!Quantity} > 0" in markdown
        assert "Must be positive." in markdown

    def test_help_text_is_mentioned(self):
        markdown = to_markdown(screen_flow(quantity(help_text="<p>h</p>")),
                               include_diagram=False)
        assert "has help text" in markdown

    def test_a_column_does_not_read_as_taking_arguments(self):
        """Its width is already in the label; repeating it looked like an input."""
        markdown = to_markdown(screen_flow(section(
            "S", column("C", 6, quantity()))), include_diagram=False)
        assert "in: width" not in markdown
