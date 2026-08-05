"""
Golden checks on the compiler. Each assertion here corresponds to something
Salesforce rejects, so a regression is caught without touching an org.
"""

import xml.etree.ElementTree as ET

from flowtool.ir import (
    Condition,
    Decision,
    FieldValue,
    Flow,
    GetRecords,
    Outcome,
    RecordFilter,
    RecordUpdate,
    Start,
    Value,
)
from flowtool.xmlgen import METADATA_NS, compute_layout, generate

NS = {"m": METADATA_NS}


def sample_flow() -> Flow:
    return Flow(
        api_name="Sample_Flow",
        label="Sample Flow",
        start=Start(
            object="Opportunity",
            record_trigger_type="CreateAndUpdate",
            trigger_type="RecordAfterSave",
            next="Get_Account",
        ),
        elements=[
            GetRecords(
                name="Get_Account",
                label="Get Account",
                object="Account",
                filters=[
                    RecordFilter(
                        field="Id",
                        operator="EqualTo",
                        value=Value(element_reference="$Record.AccountId"),
                    )
                ],
                next="Is_Big",
            ),
            Decision(
                name="Is_Big",
                label="Is Big?",
                outcomes=[
                    Outcome(
                        name="Big",
                        label="Big",
                        conditions=[
                            Condition(
                                left="$Record.Amount",
                                operator="GreaterThan",
                                right=Value(number_value=10000),
                            )
                        ],
                        next="Mark_Hot",
                    )
                ],
                next=None,
            ),
            RecordUpdate(
                name="Mark_Hot",
                label="Mark Hot",
                object="Account",
                fields=[FieldValue(field="Rating", value=Value(string_value="Hot"))],
                next=None,
            ),
        ],
    )


def parse(flow: Flow) -> ET.Element:
    return ET.fromstring(generate(flow))


class TestRequiredFields:
    def test_root_carries_deploy_required_fields(self):
        root = parse(sample_flow())
        for tag in ("apiVersion", "label", "processType", "status", "interviewLabel"):
            assert root.find(f"m:{tag}", NS) is not None, f"missing <{tag}>"

    def test_process_type_is_the_value_salesforce_accepts(self):
        # "Autolaunched" is not a valid processType; "AutoLaunchedFlow" is.
        root = parse(sample_flow())
        assert root.find("m:processType", NS).text == "AutoLaunchedFlow"

    def test_record_operations_name_their_object(self):
        root = parse(sample_flow())
        for node in root.findall("m:recordUpdates", NS) + root.findall("m:recordLookups", NS):
            obj = node.find("m:object", NS)
            assert obj is not None and obj.text, "empty <object> fails validation"


class TestConnectors:
    def test_every_target_resolves(self):
        root = parse(sample_flow())
        names = {
            node.find("m:name", NS).text
            for tag in ("assignments", "decisions", "loops", "recordCreates",
                        "recordDeletes", "recordLookups", "recordUpdates", "subflows")
            for node in root.findall(f"m:{tag}", NS)
        }
        targets = [t.text for t in root.iter(f"{{{METADATA_NS}}}targetReference")]
        assert targets, "expected at least one connector"
        assert not set(targets) - names, f"dangling: {set(targets) - names}"

    def test_ended_path_emits_no_connector(self):
        root = parse(sample_flow())
        mark_hot = next(
            n for n in root.findall("m:recordUpdates", NS)
            if n.find("m:name", NS).text == "Mark_Hot"
        )
        assert mark_hot.find("m:connector", NS) is None

    def test_decision_default_with_no_target_emits_no_default_connector(self):
        root = parse(sample_flow())
        decision = root.find("m:decisions", NS)
        assert decision.find("m:defaultConnector", NS) is None


class TestLayout:
    def test_elements_get_distinct_coordinates(self):
        flow = sample_flow()
        coords = compute_layout(flow)
        assert len(set(coords.values())) == len(flow.elements), "elements overlap on the canvas"

    def test_depth_increases_along_the_path(self):
        coords = compute_layout(sample_flow())
        assert coords["Get_Account"][1] < coords["Is_Big"][1] < coords["Mark_Hot"][1]


class TestValues:
    def test_typed_values_use_the_right_tag(self):
        root = parse(sample_flow())
        condition = root.find("m:decisions/m:rules/m:conditions", NS)
        assert condition.find("m:rightValue/m:numberValue", NS).text == "10000"
        assert condition.find("m:leftValueReference", NS).text == "$Record.Amount"

    def test_element_reference_is_not_emitted_as_a_string(self):
        root = parse(sample_flow())
        value = root.find("m:recordLookups/m:filters/m:value", NS)
        assert value.find("m:elementReference", NS) is not None
        assert value.find("m:stringValue", NS) is None
