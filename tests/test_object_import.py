"""Opening an existing Custom Object / Custom Field from the org: parse into the IR, refuse what it cannot hold."""

import pytest
from fastapi.testclient import TestClient

import server
from flowtool.object_import import UnsupportedObject, parse_custom_field, parse_custom_object
from flowtool.xmlgen_object import generate_object
from tests.test_llm import ScriptedProvider
from tests.test_server import poll

NS = 'xmlns="http://soap.sforce.com/2006/04/metadata"'


def fld(name="Score__c", body=None, kind="Number"):
    inner = body if body is not None else (
        "<precision>5</precision><scale>1</scale><required>true</required><unique>false</unique>"
    )
    return f"""<fields>
        <fullName>{name}</fullName>
        {'' if 'externalId' in inner else '<externalId>false</externalId>'}
        <label>{name.removesuffix('__c').replace('_', ' ')}</label>
        <trackTrending>false</trackTrending>
        {inner}
        <type>{kind}</type>
    </fields>"""


def object_xml(fields="", name_type="Text", sharing="ReadWrite"):
    fmt = "<displayFormat>INV-{0000}</displayFormat>" if name_type == "AutoNumber" else ""
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<CustomObject {NS}>
    <actionOverrides><actionName>Accept</actionName><type>Default</type></actionOverrides>
    <deploymentStatus>Deployed</deploymentStatus>
    <description>Customer invoices</description>
    <enableHistory>true</enableHistory>
    <enableReports>true</enableReports>
    {fields}
    <label>Invoice</label>
    <nameField>{fmt}<label>Invoice Number</label><type>{name_type}</type></nameField>
    <pluralLabel>Invoices</pluralLabel>
    <sharingModel>{sharing}</sharingModel>
</CustomObject>"""


def field_xml(body, name="Score__c", kind="Number"):
    return f'<?xml version="1.0"?><CustomObject {NS}>{fld(name, body, kind)}</CustomObject>'


def one(body, kind, name="X__c"):
    return parse_custom_field(field_xml(body, name, kind), "Invoice__c", name)


def test_an_object_opens_with_its_custom_fields_and_ignores_standard_ones():
    fields = fld() + fld("Notes__c", "<length>2000</length><visibleLines>4</visibleLines>", "LongTextArea") + \
        "<fields><fullName>Owner</fullName><label>Owner</label><type>Lookup</type></fields>"
    opened = parse_custom_object(object_xml(fields), "Invoice__c")
    assert opened.object.label == "Invoice" and opened.object.plural_label == "Invoices"
    assert opened.object.record_name == "Invoice Number" and opened.object.description == "Customer invoices"
    assert [f.api_name for f in opened.fields] == ["Notes__c", "Score__c"]
    assert opened.skipped == []
    score = opened.fields[1]
    assert (score.type, score.precision, score.scale, score.required) == ("Number", 5, 1, True)


def test_an_object_round_trips_through_the_generator():
    opened = parse_custom_object(object_xml(fld()), "Invoice__c")
    again = parse_custom_object(generate_object(opened.object, opened.fields), "Invoice__c")
    assert again.object == opened.object and again.fields == opened.fields


def test_autonumber_names_keep_their_format():
    opened = parse_custom_object(object_xml(name_type="AutoNumber"), "Invoice__c")
    assert opened.object.record_name_type == "AutoNumber" and opened.object.record_name_display_format == "INV-{0000}"


def test_a_field_the_ir_cannot_hold_is_named_and_left_out_of_an_opened_object():
    currency = fld("Amount__c", "<precision>18</precision><scale>2</scale>", "Currency")
    opened = parse_custom_object(object_xml(fld() + currency), "Invoice__c")
    assert [f.api_name for f in opened.fields] == ["Score__c"]
    assert len(opened.skipped) == 1 and "Amount__c" in opened.skipped[0] and "Currency" in opened.skipped[0]


@pytest.mark.parametrize("api, xml, reason", [
    ("Account", object_xml(), "not a custom object"),
    ("Invoice__c", object_xml(name_type="Formula"), "name field"),
    ("Invoice__c", object_xml(sharing="Bogus"), "cannot be opened"),
])
def test_an_object_that_does_not_fit_is_refused(api, xml, reason):
    with pytest.raises(UnsupportedObject, match=reason):
        parse_custom_object(xml, api)


def test_the_supported_field_types_are_read():
    pick = ("<valueSet><restricted>true</restricted><valueSetDefinition><sorted>false</sorted>"
            "<value><fullName>Low</fullName><default>true</default><label>Low</label></value>"
            "<value><fullName>High</fullName><default>false</default><label>High</label></value>"
            "</valueSetDefinition></valueSet>")
    p = one(pick, "Picklist")
    assert (p.picklist_values, p.default_value, p.restricted) == (["Low", "High"], "Low", True)
    assert one("<length>80</length><required>false</required><unique>true</unique><caseSensitive>false</caseSensitive>", "Text").unique
    assert one("<defaultValue>true</defaultValue>", "Checkbox").default_value == "true"
    lookup = one("<referenceTo>Account</referenceTo><relationshipLabel>Invoices</relationshipLabel>"
                 "<relationshipName>Invoices</relationshipName><deleteConstraint>SetNull</deleteConstraint>"
                 "<required>false</required>", "Lookup")
    assert lookup.reference_to == "Account" and lookup.relationship_name == "Invoices"
    md = one("<referenceTo>Account</referenceTo><relationshipName>Lines</relationshipName>"
             "<reparentableMasterDetail>false</reparentableMasterDetail>"
             "<writeRequiresMasterRead>false</writeRequiresMasterRead>", "MasterDetail")
    assert md.type == "MasterDetail" and md.required is False
    f = one("<formula>Score__c * 2</formula><precision>18</precision><scale>2</scale>"
            "<formulaTreatBlanksAs>BlankAsZero</formulaTreatBlanksAs>", "Number")
    assert (f.type, f.formula_returns, f.scale) == ("Formula", "Number", 2)
    assert one("<formula>Name</formula>", "Text").formula_returns == "Text"


@pytest.mark.parametrize("body, kind, reason", [
    ("", "Currency", "Currency field"),
    ("", "MultiselectPicklist", "MultiselectPicklist field"),
    ("<formula>TODAY()</formula>", "Date", "Date formula"),
    ("<formula>1</formula><formulaTreatBlanksAs>BlankAsBlank</formulaTreatBlanksAs><precision>18</precision><scale>0</scale>",
     "Number", "treats blanks"),
    ("<valueSet><valueSetName>Colors</valueSetName></valueSet>", "Picklist", "global value set"),
    ("<controllingField>A__c</controllingField><valueSet><valueSetDefinition><value><fullName>A</fullName></value>"
     "</valueSetDefinition></valueSet>", "Picklist", "dependent picklist"),
    ("<valueSet><valueSetDefinition><sorted>true</sorted><value><fullName>A</fullName></value></valueSetDefinition></valueSet>",
     "Picklist", "sorts"),
    ("<valueSet><valueSetDefinition><value><fullName>A</fullName><isActive>false</isActive></value></valueSetDefinition></valueSet>",
     "Picklist", "inactive"),
    ("<length>10</length><externalId>true</externalId>", "Text", "external id"),
    ("<length>10</length><trackHistory>true</trackHistory>", "Text", "field history"),
    ("<length>10</length><inlineHelpText>hi</inlineHelpText>", "Text", "help text"),
    ("<length>10</length><unique>true</unique><caseSensitive>true</caseSensitive>", "Text", "case sensitive"),
    ("<referenceTo>Account</referenceTo><deleteConstraint>Cascade</deleteConstraint>", "Lookup", "delete constraint"),
    ("<referenceTo>Account</referenceTo><lookupFilter><active>true</active></lookupFilter>", "Lookup", "lookup filter"),
    ("<referenceTo>Account</referenceTo><reparentableMasterDetail>true</reparentableMasterDetail>", "MasterDetail", "reparenting"),
])
def test_what_the_ir_cannot_hold_is_refused_with_the_reason(body, kind, reason):
    with pytest.raises(UnsupportedObject, match=reason):
        one(body, kind)


def test_a_missing_field_is_reported():
    with pytest.raises(UnsupportedObject, match="not found"):
        parse_custom_field(field_xml("<length>5</length>", "A__c", "Text"), "Invoice__c", "B__c")


@pytest.fixture
def client(monkeypatch):
    server.PLAN_SESSIONS.clear()
    monkeypatch.setattr(server, "credentials", lambda org, instance_url=None, access_token=None: ("https://x", "tok"))
    monkeypatch.setattr(server, "build_provider", lambda *_a, **_k: ScriptedProvider())
    with TestClient(server.app) as c:
        yield c


def test_opening_an_object_gives_a_plan_of_the_object_and_its_fields(client, monkeypatch):
    opened = parse_custom_object(object_xml(fld() + fld("Amount__c", "", "Currency")), "Invoice__c")

    async def fake_retrieve(url, token, api_name):
        assert api_name == "Invoice__c"
        return opened

    monkeypatch.setattr(server, "_retrieve_custom_object", fake_retrieve)
    started = client.post("/api/import/start", json={"api_name": "Invoice__c", "kind": "custom_object"})
    assert started.status_code == 200, started.text
    data = poll(client, "/api/import/status", job_id=started.json()["job_id"]).json()

    assert data["opened_as"] == "plan"
    assert [(s["artifact_type"], s["api_name"]) for s in data["steps"]] == [
        ("object", "Invoice__c"), ("field", "Score__c"),
    ]
    session = server.PLAN_SESSIONS[data["session_id"]]
    assert all(s.messages for s in session.steps)
    assert "Amount__c" in session.plan.reasoning
    files, types = server._bundle_files_and_types(session.steps, None, None)
    assert types == {"CustomObject": ["Invoice__c"]}
    assert "<fullName>Score__c</fullName>" in files["objects/Invoice__c.object"]


def test_opening_a_field_gives_a_one_step_plan_deployed_as_a_delta(client, monkeypatch):
    field = one("<length>80</length><required>false</required>", "Text", "Notes__c")

    async def fake_retrieve(url, token, api_name):
        assert api_name == "Invoice__c.Notes__c"
        return field

    monkeypatch.setattr(server, "_retrieve_custom_field", fake_retrieve)
    started = client.post("/api/import/start", json={"api_name": "Invoice__c.Notes__c", "kind": "custom_field"})
    data = poll(client, "/api/import/status", job_id=started.json()["job_id"]).json()
    assert data["opened_as"] == "plan" and [s["artifact_type"] for s in data["steps"]] == ["field"]
    session = server.PLAN_SESSIONS[data["session_id"]]
    files, types = server._bundle_files_and_types(session.steps, None, None)
    assert "objects/Invoice__c.object" in files


def test_the_lists_come_from_the_orgs_metadata(client, monkeypatch):
    class FakeClient:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False

        async def list_metadata(self, type_name):
            if type_name == "CustomObject":
                names = [("Account", None), ("Zed__c", None), ("Inv__c", None), ("pkg__Mgd__c", "pkg"), ("Ev__e", None)]
            else:
                names = [("Inv__c.B__c", None), ("Account.A__c", None), ("Account.Name", None), ("Inv__c.M__c", "pkg")]
            return [{"full_name": n, "namespace_prefix": p} for n, p in names]

    monkeypatch.setattr(server, "MetadataClient", FakeClient)
    assert client.post("/api/custom-objects", json={}).json() == {"objects": [{"api_name": "Inv__c"}, {"api_name": "Zed__c"}]}
    assert client.post("/api/custom-fields", json={}).json() == {
        "fields": [{"api_name": "Account.A__c"}, {"api_name": "Inv__c.B__c"}]}


RECORD_TYPES = """<recordTypes><fullName>Gold</fullName><active>true</active><label>Gold</label>
        <picklistValues><picklist>Mood__c</picklist><values><fullName>Happy</fullName><default>false</default></values></picklistValues>
    </recordTypes>"""


def test_record_types_are_reported_and_picklists_in_them_are_called_out():
    pick = ("<valueSet><valueSetDefinition><sorted>false</sorted><value><fullName>Happy</fullName>"
            "<default>false</default><label>Happy</label></value></valueSetDefinition></valueSet>")
    xml = object_xml(fld("Mood__c", pick, "Picklist") + fld()).replace("</CustomObject>", RECORD_TYPES + "</CustomObject>")
    opened = parse_custom_object(xml, "Invoice__c")
    assert len(opened.notes) == 2
    assert "Gold" in opened.notes[0] and "Mood__c" in opened.notes[1]
    assert parse_custom_object(object_xml(fld()), "Invoice__c").notes == []


def many_fields(n):
    return "".join(fld(f"F{i}__c", "<length>10</length><required>false</required>", "Text") for i in range(n))


def test_a_large_object_asks_which_fields_to_open_and_then_builds_the_plan(client, monkeypatch):
    opened = parse_custom_object(object_xml(many_fields(20), ).replace("</CustomObject>", RECORD_TYPES + "</CustomObject>"), "Invoice__c")

    async def fake_retrieve(url, token, api_name):
        return opened

    monkeypatch.setattr(server, "_retrieve_custom_object", fake_retrieve)
    started = client.post("/api/import/start", json={"api_name": "Invoice__c", "kind": "custom_object"})
    data = poll(client, "/api/import/status", job_id=started.json()["job_id"]).json()
    assert data["opened_as"] == "choose_fields" and len(data["fields"]) == 20 and data["notes"]
    assert not server.PLAN_SESSIONS

    plan = client.post("/api/import/object-fields", json={"pick_id": data["pick_id"], "fields": ["F3__c", "F7__c"]})
    assert plan.status_code == 200, plan.text
    body = plan.json()
    assert [s["api_name"] for s in body["steps"]] == ["Invoice__c", "F3__c", "F7__c"]
    assert "18 other custom field(s)" in server.PLAN_SESSIONS[body["session_id"]].plan.reasoning
    assert "record type" in server.PLAN_SESSIONS[body["session_id"]].plan.reasoning
    # The choice is single-use.
    assert client.post("/api/import/object-fields", json={"pick_id": data["pick_id"], "fields": []}).status_code == 404


def test_a_small_object_opens_straight_away_and_a_picklist_field_carries_the_record_type_note(client, monkeypatch):
    pick = ("<valueSet><valueSetDefinition><sorted>false</sorted><value><fullName>A</fullName>"
            "<default>false</default><label>A</label></value></valueSetDefinition></valueSet>")
    field = one(pick, "Picklist", "Mood__c")

    async def fake_retrieve(url, token, api_name):
        return field

    monkeypatch.setattr(server, "_retrieve_custom_field", fake_retrieve)
    started = client.post("/api/import/start", json={"api_name": "Invoice__c.Mood__c", "kind": "custom_field"})
    data = poll(client, "/api/import/status", job_id=started.json()["job_id"]).json()
    assert "record types" in server.PLAN_SESSIONS[data["session_id"]].plan.reasoning
