"""
The HTTP surface, with the approval gate tested server-side.

A gate that lives only in the browser is not a gate: anyone can call the
endpoint directly. These assert the server refuses on its own.
"""

import base64

import pytest
from fastapi.testclient import TestClient

import server
from flowtool.llm import FlowGenerator, Usage
from flowtool.sfdc import ComponentProblem, DeployResult, RetrieveError
from tests.test_llm import VALID, ScriptedProvider

ACTIVE = dict(VALID, status="Active", api_version="60.0")


@pytest.fixture
def client(monkeypatch):
    server.SESSIONS.clear()
    monkeypatch.setattr(
        server, "credentials", lambda org, instance_url=None, access_token=None: ("https://x", "tok")
    )
    return TestClient(server.app)


@pytest.fixture
def scripted(monkeypatch):
    """Make /api/design use a scripted model instead of a real provider."""
    def install(*payloads):
        provider = ScriptedProvider(*payloads)
        monkeypatch.setattr(server, "build_provider", lambda *_a, **_k: provider)
        return provider

    return install


def poll(client, url, **params):
    """
    validate/deploy/import now hand back a job immediately and finish in the
    background, so the tests poll the way the browser does instead of getting
    a result on the first request. The fakes below have no real waiting, so
    this settles within a call or two - the retry loop is just insurance
    against event-loop timing, not a real wait.
    """
    for _ in range(50):
        response = client.get(url, params=params)
        if response.status_code != 200 or response.json().get("done"):
            return response
    raise AssertionError(f"{url} never completed")


def design(client, **body):
    started = client.post("/api/design/start", json={"request": "build it", **body})
    assert started.status_code == 200, started.text
    job_id = started.json()["job_id"]
    response = poll(client, "/api/design/status", job_id=job_id)
    assert response.status_code == 200, response.text
    return response.json()


def refine(client, session_id, instruction="change it"):
    started = client.post(
        "/api/refine/start", json={"session_id": session_id, "instruction": instruction}
    )
    if started.status_code != 200:
        return started
    return poll(client, "/api/refine/status", session_id=session_id)


def repair(client, session_id):
    started = client.post("/api/repair/start", json={"session_id": session_id})
    if started.status_code != 200:
        return started
    return poll(client, "/api/repair/status", session_id=session_id)


def explain(client, session_id, question=None):
    body = {"session_id": session_id}
    if question is not None:
        body["question"] = question
    started = client.post("/api/explain/start", json=body)
    if started.status_code != 200:
        return started
    return poll(client, "/api/explain/status", session_id=session_id)


def stub_validate(monkeypatch, *results):
    """Queue DeployResults for successive validate/deploy calls."""
    queue = list(results)
    calls = []

    async def fake(instance_url, token, name, xml, api_version="62.0", check_only=True):
        calls.append(check_only)
        return queue.pop(0) if queue else DeployResult("1", "Succeeded", True)

    monkeypatch.setattr(server, "validate_flow", fake)
    return calls


OK = DeployResult("1", "Succeeded", True)
FAILED = DeployResult(
    "2", "Failed", False,
    failures=[ComponentProblem("Won_Deal_Flow", "nothing is connected to Start", "Error")],
)


class TestDesign:
    def test_returns_a_diagram_and_the_artifacts(self, client, scripted):
        scripted(VALID)
        data = design(client)
        assert data["version"] == 1
        assert data["approved"] is False
        assert "flowchart TD" in data["mermaid"]
        assert data["ir"]["api_name"] == "GC_Won_Deal_Flow"

    def test_empty_request_is_rejected(self, client, scripted):
        scripted(VALID)
        assert client.post(
            "/api/design/start", json={"request": "  "}
        ).status_code == 400

    def test_artifacts_are_downloadable(self, client, scripted):
        scripted(VALID)
        session_id = design(client)["session_id"]
        xml = client.get(f"/api/session/{session_id}/xml")
        assert xml.status_code == 200
        assert "<processType>AutoLaunchedFlow</processType>" in xml.text
        assert client.get(f"/api/session/{session_id}/markdown").status_code == 200
        assert client.get(f"/api/session/{session_id}/nope").status_code == 404


class TestApprovalGate:
    def test_validate_refuses_without_approval(self, client, scripted, monkeypatch):
        scripted(VALID)
        calls = stub_validate(monkeypatch, OK)
        session_id = design(client)["session_id"]

        response = client.post("/api/validate/start", json={"session_id": session_id})
        assert response.status_code == 403
        assert not calls, "contacted the org without approval"

    def test_deploy_refuses_without_approval(self, client, scripted, monkeypatch):
        scripted(VALID)
        calls = stub_validate(monkeypatch, OK)
        session_id = design(client)["session_id"]

        response = client.post(
            "/api/deploy/start", json={"session_id": session_id, "confirm": True}
        )
        assert response.status_code == 403
        assert not calls

    def test_deploy_refuses_without_confirmation(self, client, scripted, monkeypatch):
        scripted(VALID)
        calls = stub_validate(monkeypatch, OK, OK)
        session_id = design(client)["session_id"]
        client.post("/api/approve", json={"session_id": session_id, "version": 1})

        response = client.post("/api/deploy/start", json={"session_id": session_id})
        assert response.status_code == 400
        assert not calls

    def test_approving_a_stale_version_is_rejected(self, client, scripted):
        provider = scripted(VALID, VALID)
        session_id = design(client)["session_id"]
        refine(client, session_id)

        # v1 is what the browser last rendered; the flow is now v2.
        response = client.post(
            "/api/approve", json={"session_id": session_id, "version": 1}
        )
        assert response.status_code == 409
        assert "changed" in response.json()["detail"]

    def test_a_refinement_revokes_approval(self, client, scripted, monkeypatch):
        scripted(VALID, VALID)
        calls = stub_validate(monkeypatch, OK)
        session_id = design(client)["session_id"]
        client.post("/api/approve", json={"session_id": session_id, "version": 1})

        refined = refine(client, session_id).json()
        assert refined["approved"] is False

        assert client.post(
            "/api/validate/start", json={"session_id": session_id}
        ).status_code == 403
        assert not calls

    def test_unknown_session_is_a_404(self, client):
        assert client.post(
            "/api/validate/start", json={"session_id": "nope"}
        ).status_code == 404


class TestRepairLoop:
    def test_failures_come_back_and_can_be_repaired(self, client, scripted, monkeypatch):
        provider = scripted(VALID, VALID)
        stub_validate(monkeypatch, FAILED)
        session_id = design(client)["session_id"]
        client.post("/api/approve", json={"session_id": session_id, "version": 1})

        client.post("/api/validate/start", json={"session_id": session_id})
        result = poll(
            client, "/api/validate/status", session_id=session_id
        ).json()
        assert result["success"] is False
        assert "nothing is connected to Start" in result["failures"][0]

        repaired = repair(client, session_id).json()
        assert repaired["version"] == 2
        assert repaired["approved"] is False, "a repaired flow needs re-approval"

        # The model was told what Salesforce actually said.
        assert "nothing is connected to Start" in provider.calls[1][-1].content

    def test_repair_without_a_validation_is_rejected(self, client, scripted):
        scripted(VALID)
        session_id = design(client)["session_id"]
        assert client.post(
            "/api/repair/start", json={"session_id": session_id}
        ).status_code == 400


class TestDeploymentPolicy:
    def test_model_requested_active_is_overridden(self, client, scripted):
        scripted(ACTIVE)
        assert design(client)["status"] == "Draft"

    def test_activate_opts_in(self, client, scripted):
        scripted(VALID)
        assert design(client, activate=True)["status"] == "Active"

    def test_api_version_comes_from_the_request(self, client, scripted):
        scripted(ACTIVE)
        assert design(client, api_version="67.0")["api_version"] == "67.0"

    def test_a_repair_cannot_smuggle_active_back_in(self, client, scripted, monkeypatch):
        scripted(VALID, ACTIVE)
        stub_validate(monkeypatch, FAILED)
        session_id = design(client)["session_id"]
        client.post("/api/approve", json={"session_id": session_id, "version": 1})
        client.post("/api/validate/start", json={"session_id": session_id})
        poll(client, "/api/validate/status", session_id=session_id)

        repaired = repair(client, session_id).json()
        assert repaired["status"] == "Draft"


SAMPLE_XML = """<?xml version="1.0" encoding="UTF-8"?>
<Flow xmlns="http://soap.sforce.com/2006/04/metadata">
    <apiVersion>60.0</apiVersion>
    <recordUpdates>
        <name>Mark_Hot</name><label>Mark hot</label>
        <locationX>176</locationX><locationY>150</locationY>
        <inputAssignments><field>Rating</field>
            <value><stringValue>Hot</stringValue></value></inputAssignments>
        <object>Account</object>
    </recordUpdates>
    <label>Existing Flow</label>
    <processType>AutoLaunchedFlow</processType>
    <start>
        <locationX>176</locationX><locationY>0</locationY>
        <connector><targetReference>Mark_Hot</targetReference></connector>
        <object>Opportunity</object>
        <recordTriggerType>Update</recordTriggerType>
        <triggerType>RecordAfterSave</triggerType>
    </start>
    <status>Active</status>
</Flow>"""

UNSUPPORTED_XML = SAMPLE_XML.replace(
    "<label>Existing Flow</label>",
    "<recordRollbacks><name>Undo</name></recordRollbacks><label>Existing Flow</label>",
)


def stub_org(monkeypatch, xml=SAMPLE_XML, flows=None):
    async def fake_retrieve(instance_url, token, api_name, api_version="62.0"):
        return xml

    async def fake_list(instance_url, token, api_version="62.0"):
        from flowtool.sfdc import FlowSummary

        return flows if flows is not None else [
            FlowSummary("Existing_Flow", "Existing Flow", True, None, "2026-01-01")
        ]

    monkeypatch.setattr(server, "retrieve_flow", fake_retrieve)
    monkeypatch.setattr(server, "list_flows", fake_list)


class TestBrowseTheOrg:
    def test_lists_flows(self, client, monkeypatch):
        stub_org(monkeypatch)
        data = client.get("/api/flows").json()
        assert data["flows"][0]["api_name"] == "Existing_Flow"
        assert data["flows"][0]["active"] is True

    def test_imports_a_flow_as_a_diagram(self, client, scripted, monkeypatch):
        scripted(VALID)
        stub_org(monkeypatch)
        job_id = client.post(
            "/api/import/start", json={"api_name": "Existing_Flow"}
        ).json()["job_id"]
        data = poll(client, "/api/import/status", job_id=job_id).json()

        assert data["imported"] is True
        assert data["api_name"] == "Existing_Flow"
        assert "Mark_Hot" in data["mermaid"]
        assert data["approved"] is False, "an imported flow still needs approving"

    def test_import_does_not_call_the_model(self, client, scripted, monkeypatch):
        provider = scripted()  # no payloads queued: any call would IndexError
        stub_org(monkeypatch)
        job_id = client.post(
            "/api/import/start", json={"api_name": "Existing_Flow"}
        ).json()["job_id"]
        poll(client, "/api/import/status", job_id=job_id)
        assert provider.calls == [], "importing should be pure parsing"

    def test_an_imported_active_flow_stays_active(self, client, scripted, monkeypatch):
        scripted(VALID)
        stub_org(monkeypatch)
        job_id = client.post(
            "/api/import/start", json={"api_name": "Existing_Flow"}
        ).json()["job_id"]
        data = poll(client, "/api/import/status", job_id=job_id).json()
        # Opening a live flow to read it must not quietly propose deactivating it.
        assert data["status"] == "Active"

    def test_a_flow_we_cannot_model_is_refused_not_approximated(
        self, client, scripted, monkeypatch
    ):
        scripted(VALID)
        stub_org(monkeypatch, xml=UNSUPPORTED_XML)
        job_id = client.post(
            "/api/import/start", json={"api_name": "Existing_Flow"}
        ).json()["job_id"]
        response = poll(client, "/api/import/status", job_id=job_id)
        assert response.status_code == 422
        assert "rollback elements" in response.json()["detail"]

    def test_an_imported_flow_can_be_refined(self, client, scripted, monkeypatch):
        provider = scripted(VALID)
        stub_org(monkeypatch)
        job_id = client.post(
            "/api/import/start", json={"api_name": "Existing_Flow"}
        ).json()["job_id"]
        session_id = poll(client, "/api/import/status", job_id=job_id).json()["session_id"]

        refined = refine(client, session_id, "also set the description").json()
        assert refined["version"] == 2

        # The model saw the existing flow before the instruction, so it edits
        # rather than designing a replacement.
        conversation = provider.calls[0]
        assert "Existing_Flow" in conversation[0].content
        assert "Keep everything about it the same" in conversation[0].content
        assert "Mark_Hot" in conversation[1].content, "the IR must precede the instruction"
        assert conversation[-1].content == "also set the description"


class TestExplain:
    def test_returns_prose_about_the_flow(self, client, scripted, monkeypatch):
        provider = scripted(VALID)
        provider.text = "It marks accounts hot."
        monkeypatch.setattr(
            type(provider), "complete_text",
            lambda self, system, messages: "It marks accounts hot.", raising=False,
        )
        session_id = design(client)["session_id"]
        data = explain(client, session_id).json()
        assert data["explanation"] == "It marks accounts hot."

    def test_the_model_is_given_the_ir_not_the_xml(self, client, scripted, monkeypatch):
        provider = scripted(VALID)
        seen = {}

        def fake_text(self, system, messages):
            seen["content"] = messages[-1].content
            return "ok"

        monkeypatch.setattr(type(provider), "complete_text", fake_text, raising=False)
        session_id = design(client)["session_id"]
        explain(client, session_id)
        assert '"api_name"' in seen["content"], "expected the IR"
        assert "<Flow" not in seen["content"], "the XML is bigger and adds nothing"


class FakeTextProvider:
    """Just enough of a Provider to answer a kb-chat question."""

    def __init__(self, answer="an answer"):
        self.answer = answer
        self.usage = Usage()
        self.seen = None

    def complete_text(self, system, messages):
        self.seen = (system, messages)
        self.usage.add(input_tokens=10, output_tokens=5)
        return self.answer


class TestOrgSummary:
    def test_retrieves_a_zip_and_hands_back_base64(self, client, monkeypatch):
        async def fake(instance_url, session_id, api_version="62.0"):
            return b"PK\x03\x04fake-zip-bytes"

        monkeypatch.setattr(server, "retrieve_org_summary_zip", fake)
        started = client.post("/api/org-summary/start", json={})
        assert started.status_code == 200, started.text
        data = poll(client, "/api/org-summary/status", job_id=started.json()["job_id"]).json()
        assert data["done"] is True
        assert base64.b64decode(data["zip_base64"]) == b"PK\x03\x04fake-zip-bytes"

    def test_a_retrieve_failure_is_a_clean_400_not_a_500(self, client, monkeypatch):
        async def fake(instance_url, session_id, api_version="62.0"):
            raise RetrieveError("no access to Metadata API")

        monkeypatch.setattr(server, "retrieve_org_summary_zip", fake)
        started = client.post("/api/org-summary/start", json={})
        response = poll(client, "/api/org-summary/status", job_id=started.json()["job_id"])
        assert response.status_code == 400
        assert "no access" in response.text


class TestKbChat:
    def test_answers_a_question_against_the_supplied_markdown(self, client, monkeypatch):
        provider = FakeTextProvider("The org has 3 objects.")
        monkeypatch.setattr(server, "build_provider", lambda *_a, **_k: provider)
        started = client.post("/api/kb-chat/start", json={
            "markdown": "# KB\n\nSome org metadata.",
            "question": "How many objects?",
        })
        assert started.status_code == 200, started.text
        data = poll(client, "/api/kb-chat/status", job_id=started.json()["job_id"]).json()
        assert data["answer"] == "The org has 3 objects."
        assert data["usage"]["input_tokens"] == 10

        system, messages = provider.seen
        assert "knowledge-base" in system.lower()
        assert "How many objects?" in messages[-1].content
        assert "Some org metadata." in messages[-1].content

    def test_an_empty_question_is_refused(self, client):
        response = client.post("/api/kb-chat/start", json={"markdown": "x", "question": "   "})
        assert response.status_code == 400


class TestDeploy:
    def test_approved_and_confirmed_deploys_for_real(self, client, scripted, monkeypatch):
        scripted(VALID)
        calls = stub_validate(monkeypatch, OK, OK)
        session_id = design(client)["session_id"]
        client.post("/api/approve", json={"session_id": session_id, "version": 1})
        client.post("/api/validate/start", json={"session_id": session_id})
        poll(client, "/api/validate/status", session_id=session_id)

        client.post(
            "/api/deploy/start", json={"session_id": session_id, "confirm": True}
        )
        result = poll(client, "/api/deploy/status", session_id=session_id).json()
        assert result["success"] is True
        assert calls == [True, False], "expected checkOnly then a real deploy"


class TestModelPicker:
    """
    A picker whose choice never reaches the request is decoration, and a baked-in
    list goes stale silently - a retired model would only show up as a failure
    several seconds into a design.
    """

    def test_models_come_from_the_provider(self, client, monkeypatch):
        class Lister:
            model = "gemini-3.6-flash"

            def list_models(self):
                return ["gemini-3.6-flash", "gemini-3.5-flash"]

        monkeypatch.setattr(server, "build_provider", lambda *_a, **_k: Lister())
        data = client.post("/api/models", json={"provider": "gemini"}).json()
        assert data["models"] == ["gemini-3.6-flash", "gemini-3.5-flash"]
        assert data["default"] == "gemini-3.6-flash"

    def test_a_provider_that_cannot_list_is_not_an_error(self):
        class Silent:
            model = "whatever"

        with TestClient(server.app) as anonymous:
            server_build = server.build_provider
            try:
                server.build_provider = lambda *_a, **_k: Silent()
                data = anonymous.post("/api/models", json={}).json()
            finally:
                server.build_provider = server_build
        assert data["models"] == []
        assert data["default"] == "whatever"

    def test_a_bad_key_is_reported_not_swallowed(self, client, monkeypatch):
        class Angry:
            model = "m"

            def list_models(self):
                raise RuntimeError("API key not valid")

        monkeypatch.setattr(server, "build_provider", lambda *_a, **_k: Angry())
        response = client.post("/api/models", json={"provider": "gemini"})
        assert response.status_code == 400
        assert "API key not valid" in response.json()["detail"]

    def test_the_chosen_model_reaches_the_provider(self, client, monkeypatch):
        seen = {}

        def spy(name, model, effort, api_key=None):
            seen["model"] = model
            return ScriptedProvider(VALID)

        monkeypatch.setattr(server, "build_provider", spy)
        design(client, model="gemini-3.5-flash")
        assert seen["model"] == "gemini-3.5-flash", "the picker must not be decoration"

    def test_no_choice_means_the_provider_default(self, client, monkeypatch):
        seen = {}

        def spy(name, model, effort, api_key=None):
            seen["model"] = model
            return ScriptedProvider(VALID)

        monkeypatch.setattr(server, "build_provider", spy)
        design(client)
        assert seen["model"] is None


@pytest.fixture
def no_cli(monkeypatch):
    """
    /api/config shells out to the sf CLI to list orgs, which costs seconds and
    drags a real tool into a suite that promises to need neither an org nor a
    network. The orgs are not what these tests are about.
    """
    import flowtool.orgs

    monkeypatch.setattr(flowtool.orgs, "list_orgs", lambda: ["dev"])


class TestProviderDefault:
    """
    The Options panel lists every provider the build knows, not only the ones
    with a key - so the first entry is whichever the server defines first, and
    the page used to open on it regardless. Picking Gemini and being told "No
    Anthropic credentials found" was that: the dropdown still said anthropic.
    The browser now follows default_provider, so it has to mean what it says.
    """

    def test_the_default_provider_is_one_that_has_a_key(self, client, monkeypatch, no_cli):
        monkeypatch.setattr(server, "available_providers", lambda: ["gemini"])
        config = client.get("/api/config").json()
        assert config["default_provider"] == "gemini"
        assert config["default_provider"] in config["providers"]
        assert config["all_providers"][0] != config["default_provider"], (
            "the regression only shows when the first listed provider is not "
            "the usable one"
        )

    def test_every_known_provider_is_still_offered(self, client, monkeypatch, no_cli):
        """Listing them all is the point: a key can be pasted for any of them."""
        monkeypatch.setattr(server, "available_providers", lambda: ["gemini"])
        config = client.get("/api/config").json()
        assert set(config["all_providers"]) == set(server.PROVIDERS)

    def test_no_key_anywhere_leaves_no_default(self, client, monkeypatch, no_cli):
        monkeypatch.setattr(server, "available_providers", lambda: [])
        config = client.get("/api/config").json()
        assert config["default_provider"] is None
        assert config["providers"] == []
