"""
Screen flows, stage 3: LWC and Aura components on a screen.

A ComponentInstance is the field type with the least of its own behaviour and
the most riding on it. The flow does not know what the component does; all it
knows is the component's name, what it passes in, and where it puts what comes
back. Those three are exactly what has to survive a round trip, because they are
the only part a reviewer can check and the only part a redeploy can destroy.

Every shape asserted here was put through checkOnly against a real org first,
including the ones asserted to be refused. Where a comment quotes Salesforce,
that is the org's own wording.
"""

import xml.etree.ElementTree as ET

import pytest
from pydantic import ValidationError

from flowtool.ir import (
    ComponentOutput,
    Flow,
    InputAssignment,
    Screen,
    ScreenField,
    Start,
    Value,
    Variable,
)
from flowtool.mermaid import to_markdown
from flowtool.parse import UnsupportedFlow, parse_flow
from flowtool.xmlgen import METADATA_NS, generate

NS = {"m": METADATA_NS}


def slider(**kwargs) -> ScreenField:
    kwargs.setdefault("store_output_automatically", True)
    return ScreenField(
        name="howMany",
        field_type="ComponentInstance",
        extension_name="flowruntime:slider",
        is_required=True,
        input_parameters=[
            InputAssignment(name="label", value=Value(string_value="How many?")),
            InputAssignment(name="max", value=Value(number_value=250)),
        ],
        **kwargs,
    )


def screen_flow(field: ScreenField, variables=()) -> Flow:
    return Flow(
        api_name="Ask_Flow",
        label="Ask Flow",
        process_type="Flow",
        start=Start(next="Ask"),
        elements=[Screen(name="Ask", label="Ask", fields=[field])],
        variables=list(variables),
    )


ANSWER = Variable(name="varAnswer", data_type="Number")


class TestRoundTrip:
    def test_a_component_storing_its_output_automatically(self):
        before = screen_flow(slider())
        after = parse_flow(generate(before), api_name=before.api_name)
        assert after.model_dump() == before.model_dump()

    def test_a_component_assigning_its_outputs(self):
        before = screen_flow(
            slider(
                store_output_automatically=False,
                output_parameters=[
                    ComponentOutput(name="value", assign_to_reference="varAnswer")
                ],
            ),
            variables=[ANSWER],
        )
        after = parse_flow(generate(before), api_name=before.api_name)
        assert after.model_dump() == before.model_dump()

    @pytest.mark.parametrize("behaviour", ["UseStoredValues", "ResetValues"])
    def test_revisit_behaviour_survives(self, behaviour):
        """
        Only UseStoredValues appears in any flow examined, but the org accepts
        both, so both are modelled. Dropping this on the way out would silently
        change what a returning user sees.
        """
        before = screen_flow(slider(inputs_on_revisit=behaviour))
        after = parse_flow(generate(before), api_name=before.api_name)
        assert after.elements[0].fields[0].inputs_on_revisit == behaviour

    def test_a_component_with_no_inputs(self):
        before = screen_flow(
            ScreenField(
                name="picker",
                field_type="ComponentInstance",
                extension_name="c:somePicker",
                store_output_automatically=True,
            )
        )
        after = parse_flow(generate(before), api_name=before.api_name)
        assert after.model_dump() == before.model_dump()

    def test_input_parameter_order_is_kept(self):
        """
        Order is not meaningful to Salesforce here, but a reviewer reads the
        parameters in the order the approval document lists them, and a diff
        against the org version is unreadable if they shuffle.
        """
        names = ["label", "max"]
        after = parse_flow(generate(screen_flow(slider())), api_name="Ask_Flow")
        assert [p.name for p in after.elements[0].fields[0].input_parameters] == names


class TestGeneratedXml:
    def test_the_field_carries_the_component_name(self):
        root = ET.fromstring(generate(screen_flow(slider())))
        field = root.find(".//m:screens/m:fields", NS)
        assert field.find("m:extensionName", NS).text == "flowruntime:slider"
        assert field.find("m:fieldType", NS).text == "ComponentInstance"

    def test_no_field_text_is_written(self):
        """
        A component labels itself. Writing an empty <fieldText> would be a
        change against the org version of an imported flow for no reason.
        """
        root = ET.fromstring(generate(screen_flow(slider())))
        field = root.find(".//m:screens/m:fields", NS)
        assert field.find("m:fieldText", NS) is None

    def test_input_parameters_carry_their_values(self):
        root = ET.fromstring(generate(screen_flow(slider())))
        params = root.findall(".//m:screens/m:fields/m:inputParameters", NS)
        assert [p.find("m:name", NS).text for p in params] == ["label", "max"]
        assert params[0].find("m:value/m:stringValue", NS).text == "How many?"

    def test_output_parameters_name_their_target(self):
        flow = screen_flow(
            slider(
                store_output_automatically=False,
                output_parameters=[
                    ComponentOutput(name="value", assign_to_reference="varAnswer")
                ],
            ),
            variables=[ANSWER],
        )
        root = ET.fromstring(generate(flow))
        out = root.find(".//m:screens/m:fields/m:outputParameters", NS)
        assert out.find("m:name", NS).text == "value"
        assert out.find("m:assignToReference", NS).text == "varAnswer"

    def test_store_output_automatically_is_omitted_when_false(self):
        flow = screen_flow(
            slider(
                store_output_automatically=False,
                output_parameters=[
                    ComponentOutput(name="value", assign_to_reference="varAnswer")
                ],
            ),
            variables=[ANSWER],
        )
        root = ET.fromstring(generate(flow))
        field = root.find(".//m:screens/m:fields", NS)
        assert field.find("m:storeOutputAutomatically", NS) is None


class TestTheIrRefuses:
    def test_a_component_needs_a_name(self):
        with pytest.raises(ValidationError, match="needs extension_name"):
            ScreenField(name="picker", field_type="ComponentInstance")

    @pytest.mark.parametrize("bad", ["myComponent", ":myComponent", "c:"])
    def test_a_component_name_needs_a_namespace(self, bad):
        with pytest.raises(ValidationError, match="needs a namespace"):
            ScreenField(name="picker", field_type="ComponentInstance",
                        extension_name=bad)

    def test_both_ways_of_returning_a_value_is_refused(self):
        """
        The org's own words: "You can't use the storeOutputAutomatically field
        with the outputParameters field." Each alone deploys; together it fails.
        """
        with pytest.raises(ValidationError, match="storeOutputAutomatically"):
            slider(
                store_output_automatically=True,
                output_parameters=[
                    ComponentOutput(name="value", assign_to_reference="varAnswer")
                ],
            )

    @pytest.mark.parametrize("attribute,value", [
        ("extension_name", "c:thing"),
        ("input_parameters", [InputAssignment(name="x", value=Value(string_value="y"))]),
        ("output_parameters", [ComponentOutput(name="x", assign_to_reference="v")]),
        ("store_output_automatically", True),
        ("inputs_on_revisit", "UseStoredValues"),
    ])
    def test_component_attributes_on_an_ordinary_field_are_refused(
        self, attribute, value
    ):
        """
        These five have no meaning on a text box. Accepting them there would
        write metadata Salesforce reads as a component and a reviewer reads as
        an input.
        """
        with pytest.raises(ValidationError, match="not one"):
            ScreenField(name="Email", field_type="InputField", field_text="Email",
                        data_type="String", **{attribute: value})

    def test_an_ordinary_field_still_needs_its_label(self):
        with pytest.raises(ValidationError, match="field_text"):
            ScreenField(name="Email", field_type="InputField", data_type="String")

    def test_a_component_needs_no_label(self):
        field = ScreenField(name="picker", field_type="ComponentInstance",
                            extension_name="c:picker")
        assert field.field_text is None

    def test_a_component_needs_no_data_type(self):
        """
        Neither required nor forbidden: the component declares its own types,
        but the org accepts a data_type here, and refusing what the org accepts
        would make a deployable flow unopenable.
        """
        assert ScreenField(name="a", field_type="ComponentInstance",
                           extension_name="c:x").data_type is None
        assert ScreenField(name="b", field_type="ComponentInstance",
                           extension_name="c:x", data_type="Number").data_type == "Number"

    def test_a_component_cannot_carry_choices(self):
        with pytest.raises(ValidationError, match="nowhere to"):
            ScreenField(name="picker", field_type="ComponentInstance",
                        extension_name="c:picker", choice_references=["Red"])


class TestOutputsLandSomewhere:
    """
    The org accepts an assignToReference naming a variable that does not exist:
    checkOnly passes, the flow deploys, and the value is dropped. Verified
    against a real org - this is the one thing here that nothing else catches.
    """

    def test_a_dangling_output_target_is_refused(self):
        with pytest.raises(ValidationError, match="do not exist"):
            screen_flow(
                slider(
                    store_output_automatically=False,
                    output_parameters=[
                        ComponentOutput(name="value",
                                        assign_to_reference="varNoSuchThing")
                    ],
                )
            )

    def test_the_message_lists_what_is_defined(self):
        with pytest.raises(ValidationError, match="varAnswer"):
            screen_flow(
                slider(
                    store_output_automatically=False,
                    output_parameters=[
                        ComponentOutput(name="value", assign_to_reference="varTypo")
                    ],
                ),
                variables=[ANSWER],
            )

    def test_a_field_of_a_record_variable_is_allowed(self):
        """
        `varRecord.Name` writes to a field of varRecord, so the root before the
        dot is what has to exist.
        """
        flow = screen_flow(
            slider(
                store_output_automatically=False,
                output_parameters=[
                    ComponentOutput(name="value",
                                    assign_to_reference="varRecord.Name")
                ],
            ),
            variables=[Variable(name="varRecord", data_type="SObject",
                                object_type="Account")],
        )
        assert flow.elements[0].fields[0].output_parameters[0].name == "value"

    def test_storing_automatically_needs_no_variable(self):
        assert screen_flow(slider()).variables == []


class TestTheParserReads:
    ORG_FIELD = (
        "<fields><name>howMany</name>"
        "<extensionName>flowruntime:slider</extensionName>"
        "<fieldType>ComponentInstance</fieldType>"
        "<inputParameters><name>label</name>"
        "<value><stringValue>How many?</stringValue></value></inputParameters>"
        "<inputsOnNextNavToAssocScrn>UseStoredValues</inputsOnNextNavToAssocScrn>"
        "<isRequired>true</isRequired>"
        "<storeOutputAutomatically>true</storeOutputAutomatically>"
        "</fields>"
    )

    def org_xml(self, fields: str, extra: str = "") -> str:
        return (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Flow xmlns="http://soap.sforce.com/2006/04/metadata">'
            "<apiVersion>62.0</apiVersion><label>Ask</label>"
            "<processType>Flow</processType><status>Draft</status>"
            f"<screens><name>Ask</name><label>Ask</label>{fields}</screens>"
            "<start><connector><targetReference>Ask</targetReference></connector>"
            f"</start>{extra}</Flow>"
        )

    def test_a_component_from_an_org_parses(self):
        flow = parse_flow(self.org_xml(self.ORG_FIELD), api_name="Ask")
        field = flow.elements[0].fields[0]
        assert field.field_type == "ComponentInstance"
        assert field.extension_name == "flowruntime:slider"
        assert field.store_output_automatically is True
        assert field.inputs_on_revisit == "UseStoredValues"
        assert [p.name for p in field.input_parameters] == ["label"]

    def test_a_component_without_revisit_behaviour_does_not_invent_one(self):
        """
        Unset is not UseStoredValues. Writing a default back would be answering
        a question the flow never asked.
        """
        stripped = self.ORG_FIELD.replace(
            "<inputsOnNextNavToAssocScrn>UseStoredValues"
            "</inputsOnNextNavToAssocScrn>", ""
        )
        flow = parse_flow(self.org_xml(stripped), api_name="Ask")
        assert flow.elements[0].fields[0].inputs_on_revisit is None
        assert "inputsOnNextNavToAssocScrn" not in generate(flow)

    def test_a_component_has_no_field_text_on_the_way_back_out(self):
        flow = parse_flow(self.org_xml(self.ORG_FIELD), api_name="Ask")
        assert flow.elements[0].fields[0].field_text is None
        assert "fieldText" not in generate(flow)

    def test_output_parameters_from_an_org_parse(self):
        field = (
            "<fields><name>howMany</name>"
            "<extensionName>c:thing</extensionName>"
            "<fieldType>ComponentInstance</fieldType>"
            "<outputParameters><assignToReference>varAnswer</assignToReference>"
            "<name>value</name></outputParameters></fields>"
        )
        variable = (
            "<variables><name>varAnswer</name><dataType>Number</dataType>"
            "<isCollection>false</isCollection><isInput>false</isInput>"
            "<isOutput>false</isOutput></variables>"
        )
        flow = parse_flow(self.org_xml(field, variable), api_name="Ask")
        output = flow.elements[0].fields[0].output_parameters[0]
        assert (output.name, output.assign_to_reference) == ("value", "varAnswer")

    def test_a_screen_field_kind_still_unmodelled_is_refused_by_name(self):
        """
        The point of the allowlist has not changed: an unmodelled field type has
        the same children as anything else, so it is refused by name rather than
        read as a component that happens to have no extensionName.

        RegionContainer used to be the example here and is modelled now, so this
        uses one that still is not.
        """
        field = (
            "<fields><name>Secret</name><fieldText>Secret</fieldText>"
            "<fieldType>PasswordField</fieldType></fields>"
        )
        with pytest.raises(UnsupportedFlow) as caught:
            parse_flow(self.org_xml(field), api_name="Ask")
        assert "screen_field:PasswordField" in caught.value.codes


class TestWhatTheUserSees:
    def test_the_document_names_the_component(self):
        markdown = to_markdown(screen_flow(slider()))
        assert "flowruntime:slider" in markdown

    def test_the_document_shows_what_goes_in_and_comes_out(self):
        flow = screen_flow(
            slider(
                store_output_automatically=False,
                output_parameters=[
                    ComponentOutput(name="value", assign_to_reference="varAnswer")
                ],
            ),
            variables=[ANSWER],
        )
        markdown = to_markdown(flow)
        assert "label = " in markdown
        assert "varAnswer" in markdown

    def test_automatic_storage_is_shown_as_how_to_read_it(self):
        """
        A reviewer cannot check an output they cannot name, and with automatic
        storage the name is not written anywhere in the flow.
        """
        assert "{!howMany." in to_markdown(screen_flow(slider()))
