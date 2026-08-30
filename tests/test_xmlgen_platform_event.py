"""
Golden checks on the Platform Event compiler: a __e's .object content omits
sharingModel/nameField (neither applies, same as a __mdt type), always
carries eventType=HighVolume (the only value Salesforce still accepts - not
even part of the IR), and carries publishBehavior (which neither a regular
CustomObject nor a __mdt type ever does).
"""

import xml.etree.ElementTree as ET

from flowtool.ir_platform_event import PlatformEvent, PlatformEventField
from flowtool.xmlgen_platform_event import METADATA_NS, generate_platform_event

NS = {"m": METADATA_NS}
TEXT_FIELD = PlatformEventField(api_name="Notes", label="Notes", type="Text")


class TestGeneratePlatformEvent:
    def test_no_sharing_model_or_name_field(self):
        event = PlatformEvent(
            api_name="Order_Placed", label="Order Placed", plural_label="Order Placed",
            fields=[TEXT_FIELD],
        )
        xml = generate_platform_event(event)
        root = ET.fromstring(xml)
        assert root.find("m:sharingModel", NS) is None
        assert root.find("m:nameField", NS) is None

    def test_event_type_is_always_high_volume(self):
        event = PlatformEvent(
            api_name="Order_Placed", label="Order Placed", plural_label="Order Placed",
            fields=[TEXT_FIELD],
        )
        xml = generate_platform_event(event)
        root = ET.fromstring(xml)
        assert root.find("m:eventType", NS).text == "HighVolume"

    def test_publish_behavior_is_present(self):
        event = PlatformEvent(
            api_name="Order_Placed", label="Order Placed", plural_label="Order Placed",
            publish_behavior="PublishImmediately", fields=[TEXT_FIELD],
        )
        xml = generate_platform_event(event)
        root = ET.fromstring(xml)
        assert root.find("m:publishBehavior", NS).text == "PublishImmediately"

    def test_fields_are_embedded_sorted_by_api_name(self):
        f1 = PlatformEventField(api_name="Zeta", label="Zeta", type="Text")
        f2 = PlatformEventField(api_name="Alpha", label="Alpha", type="Text")
        event = PlatformEvent(
            api_name="Ordering", label="Ordering", plural_label="Orderings", fields=[f1, f2],
        )
        xml = generate_platform_event(event)
        root = ET.fromstring(xml)
        names = [f.find("m:fullName", NS).text for f in root.findall("m:fields", NS)]
        assert names == ["Alpha__c", "Zeta__c"]

    def test_number_field_carries_precision_and_scale(self):
        num = PlatformEventField(api_name="Amount", label="Amount", type="Number", precision=16, scale=2)
        event = PlatformEvent(
            api_name="Order_Placed", label="Order Placed", plural_label="Order Placed",
            fields=[num],
        )
        xml = generate_platform_event(event)
        root = ET.fromstring(xml)
        field_el = root.find("m:fields", NS)
        assert field_el.find("m:precision", NS).text == "16"
        assert field_el.find("m:scale", NS).text == "2"
        assert field_el.find("m:type", NS).text == "Number"

    # Both cases below mirror a real checkOnly deploy failure this session -
    # missing defaultValue/visibleLines was rejected by a live dev org.
    def test_checkbox_field_carries_default_value(self):
        checkbox = PlatformEventField(api_name="Is_Rush", label="Is Rush", type="Checkbox")
        event = PlatformEvent(
            api_name="Order_Placed", label="Order Placed", plural_label="Order Placed",
            fields=[checkbox],
        )
        xml = generate_platform_event(event)
        root = ET.fromstring(xml)
        field_el = root.find("m:fields", NS)
        assert field_el.find("m:defaultValue", NS).text == "false"

    def test_long_text_area_field_carries_length_and_visible_lines(self):
        notes = PlatformEventField(api_name="Notes", label="Notes", type="LongTextArea")
        event = PlatformEvent(
            api_name="Order_Placed", label="Order Placed", plural_label="Order Placed",
            fields=[notes],
        )
        xml = generate_platform_event(event)
        root = ET.fromstring(xml)
        field_el = root.find("m:fields", NS)
        assert field_el.find("m:length", NS).text == "32768"
        assert field_el.find("m:visibleLines", NS).text == "3"
