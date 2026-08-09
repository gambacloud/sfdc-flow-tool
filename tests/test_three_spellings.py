"""
The last three corpus blockers, and the silent one found while doing them.

None of the three is a new capability. Each is an older or more explicit way of
saying something the IR already supported, which is why they blocked *imports*
rather than authoring:

    outputAssignments           put each field in its own variable, instead of
                                keeping the record and reading its fields
    assignNextValueToReference  name the loop's current item, instead of reading
                                it as the loop's own name
    flowTransactionModel        say which transaction an action runs in

The fourth was not on any list. `assignNullValuesIfNoRecordsFound` was in the
allowlist, so a flow carrying it parsed cleanly - and never read, while the
compiler wrote `false` unconditionally. A flow that said true came back saying
false: not a dropped tag but a changed answer, on the flag that decides what a
lookup finding nothing leaves behind. Fifth time this project has found that
shape of bug, and the first time it changed a value rather than removing one.
"""

import pytest
from pydantic import ValidationError

from flowtool.ir import (
    ActionCall,
    Assignment,
    AssignmentItem,
    Flow,
    GetRecords,
    InputAssignment,
    Loop,
    OutputAssignment,
    Start,
    Value,
    Variable,
)
from flowtool.parse import parse_flow
from flowtool.xmlgen import generate

NAME = Variable(name="v_Name", data_type="String")
RATING = Variable(name="v_Rating", data_type="String")
ACCOUNTS = Variable(name="v_Accounts", data_type="SObject",
                    object_type="Account", is_collection=True)
ONE = Variable(name="v_One", data_type="SObject", object_type="Account")


def flow(*elements, **kwargs) -> Flow:
    kwargs.setdefault("start", Start(next=elements[0].name))
    return Flow(api_name="Three", label="Three", elements=list(elements),
                **kwargs)


def survives(f: Flow) -> bool:
    before = f.model_dump()
    after = parse_flow(generate(f), api_name=f.api_name).model_dump()
    if {k: v for k, v in before.items() if k != "elements"} != {
            k: v for k, v in after.items() if k != "elements"}:
        return False
    return ({e["name"]: e for e in before["elements"]}
            == {e["name"]: e for e in after["elements"]})


def assigning(**kwargs) -> GetRecords:
    fields = dict(name="Get", label="Get", object="Account",
                  store_output_automatically=False,
                  output_assignments=[OutputAssignment(
                      field="Name", assign_to_reference="v_Name")])
    fields.update(kwargs)
    return GetRecords(**fields)


class TestFieldsIntoVariables:
    def test_it_survives(self):
        assert survives(flow(assigning(), variables=[NAME]))

    def test_several_fields_keep_their_order(self):
        f = flow(assigning(output_assignments=[
            OutputAssignment(field="Name", assign_to_reference="v_Name"),
            OutputAssignment(field="Rating", assign_to_reference="v_Rating"),
        ]), variables=[NAME, RATING])
        back = parse_flow(generate(f), api_name=f.api_name).elements[0]
        assert [a.field for a in back.output_assignments] == ["Name", "Rating"]

    @pytest.mark.parametrize("other,quoted", [
        ({"store_output_automatically": True}, "storeOutputAutomatically"),
        ({"output_reference": "v_One"}, "sObjectOutputReference"),
    ])
    def test_it_excludes_the_other_two_ways(self, other, quoted):
        """
        Three answers to one question now. The org states both pairs it
        refuses, and the wording is what someone will search for.
        """
        with pytest.raises(ValidationError, match=quoted):
            assigning(**other)

    def test_a_variable_that_does_not_exist_is_refused(self):
        """
        The org accepts this one. It validated an assignment into `v_Nope`
        without a word, and the field is then read and dropped.
        """
        with pytest.raises(ValidationError, match="do not exist"):
            flow(assigning(output_assignments=[OutputAssignment(
                field="Name", assign_to_reference="v_Nope")]))

    def test_a_field_that_does_not_exist_is_not_second_guessed(self):
        """
        Also accepted by the org, and unknowable here: telling a real field
        from a wrong one needs the object's schema, which this tool does not
        read.
        """
        assert flow(assigning(output_assignments=[OutputAssignment(
            field="No_Such_Field__c", assign_to_reference="v_Name")]),
            variables=[NAME]).elements


class TestTheLoopsOwnVariable:
    def test_it_survives(self):
        assert survives(flow(
            Loop(name="Each", label="Each", collection_reference="v_Accounts",
                 assign_next_value_to_reference="v_One", first_element="Body",
                 next=None),
            Assignment(name="Body", label="Body", next="Each", items=[
                AssignmentItem(to_reference="v_Name",
                               value=Value(element_reference="v_One.Name"))]),
            variables=[ACCOUNTS, ONE, NAME]))

    def test_a_loop_without_one_still_works(self):
        """Reading the item as the loop's own name is the modern default."""
        assert survives(flow(
            Loop(name="Each", label="Each", collection_reference="v_Accounts",
                 next=None),
            variables=[ACCOUNTS]))

    def test_a_variable_that_does_not_exist_is_refused(self):
        """
        The org catches this one - "Value v_Nope in the AssignNextValueTo
        element doesn't exist in this flow" - so catching it here only means
        the model hears about it before a deploy round trip rather than after.
        """
        with pytest.raises(ValidationError, match="do not exist"):
            flow(Loop(name="Each", label="Each",
                      collection_reference="v_Accounts",
                      assign_next_value_to_reference="v_Nope"),
                 variables=[ACCOUNTS])


class TestTheTransactionModel:
    @pytest.mark.parametrize("model", [
        "CurrentTransaction", "NewTransaction", "Automatic",
    ])
    def test_every_value_survives(self, model):
        assert survives(flow(ActionCall(
            name="Notify", label="Notify", action_name="emailSimple",
            action_type="emailSimple", flow_transaction_model=model)))

    def test_a_value_outside_the_enum_is_refused(self):
        with pytest.raises(ValidationError):
            ActionCall(name="Notify", label="Notify", action_name="x",
                       action_type="x", flow_transaction_model="Whenever")

    def test_which_actions_allow_which_is_left_to_the_org(self):
        """
        The org answers per action - "The action 'EMAILSIMPLE' only supports
        'CurrentTransaction'" - and there is no list of that anywhere this tool
        can read.
        """
        assert ActionCall(name="N", label="N", action_name="emailSimple",
                          action_type="emailSimple",
                          flow_transaction_model="NewTransaction")

    def test_an_action_without_one_writes_nothing(self):
        f = flow(ActionCall(name="Notify", label="Notify", action_name="x",
                            action_type="x"))
        assert "flowTransactionModel" not in generate(f)

    def test_the_document_says_which_transaction(self):
        from flowtool.mermaid import to_markdown

        markdown = to_markdown(flow(ActionCall(
            name="Notify", label="Notify", action_name="x", action_type="x",
            flow_transaction_model="NewTransaction")), include_diagram=False)
        assert "in its own transaction" in markdown

    def test_the_diagram_node_is_still_a_node(self):
        """
        This sentence was first written into `_node`, which returns the mermaid
        shape - so an action with a transaction model emitted prose where the
        diagram expected `Name[/"Label"\\]`. Nothing failed; the diagram simply
        would not have drawn.
        """
        from flowtool.mermaid import to_mermaid

        diagram = to_mermaid(flow(ActionCall(
            name="Notify", label="Notify", action_name="x", action_type="x",
            flow_transaction_model="NewTransaction")))
        assert 'Notify[/"Notify"\\]' in diagram
        assert "in its own transaction" not in diagram


class TestTheFlagThatWasBeingChanged:
    """
    Allowlisted, never read, and written back as `false` regardless. A flow
    that said true parsed cleanly and deployed saying false.
    """

    def test_it_survives_the_round_trip(self):
        f = flow(GetRecords(name="Get", label="Get", object="Account",
                            assign_null_values_if_no_records_found=True))
        back = parse_flow(generate(f), api_name=f.api_name).elements[0]
        assert back.assign_null_values_if_no_records_found is True

    def test_false_is_still_the_default(self):
        f = flow(GetRecords(name="Get", label="Get", object="Account"))
        assert "<assignNullValuesIfNoRecordsFound>false" in generate(f)

    def test_a_flow_from_an_org_keeps_what_it_said(self):
        xml = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Flow xmlns="http://soap.sforce.com/2006/04/metadata">'
            "<apiVersion>62.0</apiVersion><label>X</label>"
            "<processType>AutoLaunchedFlow</processType><status>Draft</status>"
            "<recordLookups><name>Get</name><label>Get</label>"
            "<object>Account</object>"
            "<assignNullValuesIfNoRecordsFound>true"
            "</assignNullValuesIfNoRecordsFound>"
            "<outputAssignments><assignToReference>v_Name</assignToReference>"
            "<field>Name</field></outputAssignments>"
            "</recordLookups>"
            "<start><connector><targetReference>Get</targetReference></connector>"
            "</start>"
            "<variables><name>v_Name</name><dataType>String</dataType>"
            "<isCollection>false</isCollection><isInput>false</isInput>"
            "<isOutput>false</isOutput></variables></Flow>"
        )
        parsed = parse_flow(xml, api_name="X")
        assert parsed.elements[0].assign_null_values_if_no_records_found is True
        assert "<assignNullValuesIfNoRecordsFound>true" in generate(parsed)
