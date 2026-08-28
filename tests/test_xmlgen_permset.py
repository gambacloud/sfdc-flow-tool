import xml.etree.ElementTree as ET

from flowtool.ir_permset import FieldGrant, ObjectGrant, PermissionSetGrant
from flowtool.xmlgen_permset import METADATA_NS, generate_permission_set, merge_permission_set_xml

NS = {"m": METADATA_NS}


class TestGeneratePermissionSet:
    def test_fresh_document_shape(self):
        grant = PermissionSetGrant(
            label="Invoice Access",
            field_grants=[
                FieldGrant(field_api_name="Amount__c", object_api_name="Invoice__c"),
            ],
            object_grants=[ObjectGrant(object_api_name="Invoice__c")],
        )
        xml = generate_permission_set(grant, api_name="Invoice_Access")
        assert xml.startswith('<?xml version="1.0" encoding="UTF-8"?>')

        root = ET.fromstring(xml)
        assert root.tag == f"{{{METADATA_NS}}}PermissionSet"
        assert root.findtext("m:label", "", NS) == "Invoice Access"

        fp = root.find("m:fieldPermissions", NS)
        assert fp.findtext("m:field", "", NS) == "Invoice__c.Amount__c"
        assert fp.findtext("m:editable", "", NS) == "true"
        assert fp.findtext("m:readable", "", NS) == "true"

        op = root.find("m:objectPermissions", NS)
        assert op.findtext("m:object", "", NS) == "Invoice__c"
        assert op.findtext("m:allowRead", "", NS) == "true"
        assert op.findtext("m:allowCreate", "", NS) == "true"
        assert op.findtext("m:allowEdit", "", NS) == "true"
        assert op.findtext("m:allowDelete", "", NS) == "false"

    def test_read_only_field_grant_renders_editable_false(self):
        grant = PermissionSetGrant(
            label="Read Only",
            field_grants=[
                FieldGrant(field_api_name="Score__c", object_api_name="Account", editable=False),
            ],
        )
        xml = generate_permission_set(grant, api_name="Read_Only")
        root = ET.fromstring(xml)
        fp = root.find("m:fieldPermissions", NS)
        assert fp.findtext("m:editable", "", NS) == "false"
        assert fp.findtext("m:readable", "", NS) == "true"


_EXISTING_DOC = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    f'<PermissionSet xmlns="{METADATA_NS}">\n'
    "    <label>Existing Set</label>\n"
    "    <hasActivationRequired>false</hasActivationRequired>\n"
    "    <fieldPermissions>\n"
    "        <editable>false</editable>\n"
    "        <field>Account.OtherField__c</field>\n"
    "        <readable>true</readable>\n"
    "    </fieldPermissions>\n"
    "    <objectPermissions>\n"
    "        <allowCreate>false</allowCreate>\n"
    "        <allowDelete>false</allowDelete>\n"
    "        <allowEdit>false</allowEdit>\n"
    "        <allowRead>true</allowRead>\n"
    "        <modifyAllRecords>false</modifyAllRecords>\n"
    "        <object>OtherObject__c</object>\n"
    "        <viewAllRecords>false</viewAllRecords>\n"
    "    </objectPermissions>\n"
    "</PermissionSet>\n"
)


class TestMergePermissionSetXml:
    def test_leaves_unrelated_existing_blocks_untouched(self):
        grant = PermissionSetGrant(
            label="ignored on merge",
            field_grants=[FieldGrant(field_api_name="Amount__c", object_api_name="Invoice__c")],
        )
        merged = merge_permission_set_xml(_EXISTING_DOC, grant)
        root = ET.fromstring(merged)

        fields = {fp.findtext("m:field", "", NS) for fp in root.findall("m:fieldPermissions", NS)}
        assert "Account.OtherField__c" in fields
        other = next(
            fp for fp in root.findall("m:fieldPermissions", NS)
            if fp.findtext("m:field", "", NS) == "Account.OtherField__c"
        )
        assert other.findtext("m:editable", "", NS) == "false", "untouched, not upgraded"

        obj = root.find("m:objectPermissions", NS)
        assert obj.findtext("m:object", "", NS) == "OtherObject__c"
        assert obj.findtext("m:allowCreate", "", NS) == "false", "untouched"

    def test_adds_a_new_field_grant(self):
        grant = PermissionSetGrant(
            label="ignored on merge",
            field_grants=[FieldGrant(field_api_name="Amount__c", object_api_name="Invoice__c")],
        )
        merged = merge_permission_set_xml(_EXISTING_DOC, grant)
        root = ET.fromstring(merged)
        fields = {fp.findtext("m:field", "", NS) for fp in root.findall("m:fieldPermissions", NS)}
        assert fields == {"Account.OtherField__c", "Invoice__c.Amount__c"}

    def test_upgrades_a_matching_read_only_field_to_editable_without_duplicating(self):
        grant = PermissionSetGrant(
            label="ignored on merge",
            field_grants=[
                FieldGrant(field_api_name="OtherField__c", object_api_name="Account", editable=True),
            ],
        )
        merged = merge_permission_set_xml(_EXISTING_DOC, grant)
        root = ET.fromstring(merged)
        matches = [
            fp for fp in root.findall("m:fieldPermissions", NS)
            if fp.findtext("m:field", "", NS) == "Account.OtherField__c"
        ]
        assert len(matches) == 1, "must not duplicate an existing entry"
        assert matches[0].findtext("m:editable", "", NS) == "true", "must upgrade, not leave read-only"

    def test_adds_a_new_object_grant_without_duplicating_the_existing_one(self):
        grant = PermissionSetGrant(
            label="ignored on merge",
            object_grants=[ObjectGrant(object_api_name="Invoice__c")],
        )
        merged = merge_permission_set_xml(_EXISTING_DOC, grant)
        root = ET.fromstring(merged)
        objects = {op.findtext("m:object", "", NS) for op in root.findall("m:objectPermissions", NS)}
        assert objects == {"OtherObject__c", "Invoice__c"}
