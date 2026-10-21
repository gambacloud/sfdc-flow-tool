"""
The backend half of the Chrome extension split (docs/store/ARCHITECTURE.md):
org-free bundle / parse / score endpoints, org context supplied in request
bodies, and hosted ("no-org") mode with CORS. No LLM, org or network is used.
"""

import time

import pytest
from fastapi.testclient import TestClient

import server
from flowtool.ir_object import CustomField, CustomObject
from flowtool.xmlgen_object import generate_object
from tests.test_ir_apex_generator import VALID as VALID_APEX
from tests.test_ir_object_generator import VALID_FIELD, VALID_OBJECT
from tests.test_llm import VALID, ScriptedProvider
from tests.test_server import design
from tests.test_server_plan import BUNDLE_PLAN, ONE_STEP_PLAN, execute, make_plan

OBJECT_NAME = VALID_OBJECT["api_name"] + "__c"
EXTENSION = "chrome-extension://abcdefghijklmnopabcdefghijklmnop"

EXISTING_PS = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<PermissionSet xmlns="http://soap.sforce.com/2006/04/metadata">\n'
    "    <label>Existing Set</label>\n"
    "    <fieldPermissions>\n"
    "        <editable>false</editable>\n"
    "        <field>Account.SomeOtherField__c</field>\n"
    "        <readable>true</readable>\n"
    "    </fieldPermissions>\n"
    "</PermissionSet>\n"
)


@pytest.fixture
def client(monkeypatch):
    for var in (server.hosted.HOSTED_ENV, server.hosted.CORS_ENV):
        monkeypatch.delenv(var, raising=False)
    server.SESSIONS.clear()
    server.PLAN_SESSIONS.clear()
    server.PLANS.clear()
    server.PLAN_JOBS.clear()
    with TestClient(server.app) as test_client:
        yield test_client


@pytest.fixture
def scripted(monkeypatch):
    def install(*payloads):
        provider = ScriptedProvider(*payloads)
        monkeypatch.setattr(server, "build_provider", lambda *_a, **_k: provider)
        return provider

    return install


@pytest.fixture
def typed_scripted(monkeypatch):
    from tests.test_llm import TypedScriptedProvider

    def install(**payloads_by_type):
        provider = TypedScriptedProvider(**payloads_by_type)
        monkeypatch.setattr(server, "build_provider", lambda *_a, **_k: provider)
        return provider

    return install


def approved_flow(client, scripted):
    scripted(VALID)
    data = design(client)
    client.post("/api/approve", json={"session_id": data["session_id"], "version": data["version"]})
    return data


# --------------------------------------------------------------------------
# bundle
# --------------------------------------------------------------------------


class TestSessionBundle:
    def test_needs_approval(self, client, scripted):
        scripted(VALID)
        data = design(client)
        assert client.get(f"/api/session/{data['session_id']}/bundle").status_code == 403

    def test_returns_files_types_and_api_version(self, client, scripted):
        data = approved_flow(client, scripted)
        body = client.get(f"/api/session/{data['session_id']}/bundle").json()
        assert body["version"] == data["version"]
        assert body["api_version"] == data["api_version"]
        (path, xml), = body["files"].items()
        assert path.startswith("flows/") and path.endswith(".flow") and "<Flow" in xml
        assert body["types"] == {"Flow": [data["api_name"]]}

    def test_matches_the_deploy_files(self, client, scripted):
        data = approved_flow(client, scripted)
        files, types = server._deploy_files(server.SESSIONS[data["session_id"]])
        body = client.get(f"/api/session/{data['session_id']}/bundle").json()
        assert (body["files"], body["types"]) == (files, types)

    def test_unknown_session_is_404(self, client):
        assert client.get("/api/session/nope/bundle").status_code == 404

    def test_other_artifacts_still_resolve(self, client, scripted):
        data = approved_flow(client, scripted)
        assert client.get(f"/api/session/{data['session_id']}/package").status_code == 200

    def test_deploy_result_feeds_the_repair_loop(self, client, scripted):
        data = approved_flow(client, scripted)
        sid = data["session_id"]
        r = client.post(f"/api/session/{sid}/deploy-result", json={"success": False, "failures": ["bad field"]})
        assert r.json()["recorded"] is True
        assert server.SESSIONS[sid].last_failures == ["bad field"]
        client.post(f"/api/session/{sid}/deploy-result", json={"success": True})
        assert server.SESSIONS[sid].last_failures == []


class TestPlanBundle:
    def test_needs_approval(self, client, typed_scripted):
        typed_scripted(plan=ONE_STEP_PLAN, flow=VALID)
        session = execute(client, make_plan(client)["plan_id"])
        assert client.get(f"/api/plan/session/{session['session_id']}/bundle").status_code == 403

    def test_returns_the_org_free_bundle(self, client, typed_scripted):
        typed_scripted(plan=ONE_STEP_PLAN, flow=VALID)
        session = execute(client, make_plan(client)["plan_id"])
        sid = session["session_id"]
        client.post("/api/plan/approve", json={"session_id": sid, "version": session["version"]})
        body = client.get(f"/api/plan/session/{sid}/bundle").json()
        files, types = server._bundle_files_and_types(server.PLAN_SESSIONS[sid].steps)
        assert body["files"] == files and body["types"] == types
        assert body["version"] == session["version"]
        assert body["api_version"] == "64.0"
        assert body["needs_existing_permission_set"] is None

    def test_new_permission_set_is_included(self, client, typed_scripted):
        typed_scripted(plan=BUNDLE_PLAN, object=VALID_OBJECT, field=VALID_FIELD, apex=VALID_APEX)
        plan = make_plan(client, grant_permission_set="new", permission_set_label="Invoice_Access")
        session = execute(client, plan["plan_id"])
        sid = session["session_id"]
        client.post("/api/plan/approve", json={"session_id": sid, "version": session["version"]})
        body = client.get(f"/api/plan/session/{sid}/bundle").json()
        assert body["types"]["PermissionSet"] == ["Invoice_Access"]
        assert "permissionsets/Invoice_Access.permissionset" in body["files"]

    def test_existing_permission_set_needs_its_xml_then_merges(self, client, typed_scripted):
        typed_scripted(plan=BUNDLE_PLAN, object=VALID_OBJECT, field=VALID_FIELD, apex=VALID_APEX)
        plan = make_plan(client, grant_permission_set="existing", permission_set_api_name="Existing_Set")
        session = execute(client, plan["plan_id"])
        sid = session["session_id"]
        client.post("/api/plan/approve", json={"session_id": sid, "version": session["version"]})

        first = client.get(f"/api/plan/session/{sid}/bundle").json()
        assert first["needs_existing_permission_set"] == "Existing_Set"
        assert "PermissionSet" not in first["types"]

        merged = client.post(
            f"/api/plan/session/{sid}/bundle", json={"existing_permission_set_xml": EXISTING_PS}
        ).json()
        xml = merged["files"]["permissionsets/Existing_Set.permissionset"]
        assert "Account.SomeOtherField__c" in xml and "Invoice__c.Amount__c" in xml
        assert merged["types"]["PermissionSet"] == ["Existing_Set"]
        assert merged["needs_existing_permission_set"] is None

    def test_post_without_xml_for_an_existing_set_reports_it_missing(self, client, typed_scripted):
        typed_scripted(plan=BUNDLE_PLAN, object=VALID_OBJECT, field=VALID_FIELD, apex=VALID_APEX)
        plan = make_plan(client, grant_permission_set="existing", permission_set_api_name="Existing_Set")
        session = execute(client, plan["plan_id"])
        sid = session["session_id"]
        client.post("/api/plan/approve", json={"session_id": sid, "version": session["version"]})
        body = client.post(f"/api/plan/session/{sid}/bundle", json={}).json()
        assert body["needs_existing_permission_set"] == "Existing_Set"

    def test_deploy_result_records_failures_and_deployment(self, client, typed_scripted):
        typed_scripted(plan=ONE_STEP_PLAN, flow=VALID)
        session = execute(client, make_plan(client)["plan_id"])
        sid = session["session_id"]
        client.post(
            f"/api/plan/session/{sid}/deploy-result",
            json={"success": False, "failures": ["boom"],
                  "component_failures": [{"full_name": "X", "problem": "boom", "problem_type": "Error"}]},
        )
        stored = server.PLAN_SESSIONS[sid]
        assert stored.last_failures == ["boom"] and stored.last_component_failures[0].full_name == "X"
        client.post(f"/api/plan/session/{sid}/deploy-result", json={"success": True, "deployed": True})
        assert stored.last_failures == [] and stored.deployed_version == stored.version


class TestPlanOrgContext:
    def test_org_context_reaches_the_planner_as_text(self, client, scripted, monkeypatch):
        seen = {}

        def fake_generate(self, request):
            seen["request"] = request
            raise RuntimeError("stop here")

        monkeypatch.setattr(server.PlannerGenerator, "generate", fake_generate)
        scripted()
        client.post("/api/plan/start", json={
            "request": "add a region",
            "org_context": {"objects": {"Account": {"fields": ["Region__c", {"api_name": "Tier__c"}]}}},
        })
        for _ in range(100):
            if "request" in seen:
                break
            time.sleep(0.02)
        assert "add a region" in seen["request"]
        assert "Account: fields Region__c, Tier__c" in seen["request"]

    def test_no_context_leaves_the_request_unchanged(self):
        assert server._org_context_text(None) == ""
        assert server._org_context_text({"objects": {}}) == ""


# --------------------------------------------------------------------------
# import/parse, survey/score, portal plan
# --------------------------------------------------------------------------


class TestImportParse:
    def flow_xml(self, client, scripted):
        data = approved_flow(client, scripted)
        files, _ = server._deploy_files(server.SESSIONS[data["session_id"]])
        return data["api_name"], next(iter(files.values()))

    def test_flow_xml_opens_a_session_without_an_org(self, client, scripted):
        name, xml = self.flow_xml(client, scripted)
        provider = scripted()  # nothing queued: any model call would fail
        data = client.post("/api/import/parse", json={"api_name": name, "kind": "flow", "xml": xml}).json()
        assert data["imported"] is True and data["approved"] is False
        assert data["api_name"] == name and "session_id" in data
        assert provider.calls == []
        assert server.SESSIONS[data["session_id"]].history[0]["note"].startswith("Imported")

    def test_session_can_then_be_bundled_after_approval(self, client, scripted):
        name, xml = self.flow_xml(client, scripted)
        scripted()
        data = client.post("/api/import/parse", json={"api_name": name, "kind": "flow", "xml": xml}).json()
        sid = data["session_id"]
        client.post("/api/approve", json={"session_id": sid, "version": data["version"]})
        assert client.get(f"/api/session/{sid}/bundle").json()["types"] == {"Flow": [name]}

    def test_apex_source(self, client, scripted):
        scripted()
        data = client.post(
            "/api/import/parse",
            json={"api_name": "Helper", "kind": "apex", "xml": "public class Helper { }"},
        ).json()
        assert data["kind"] == "apex" and data["api_name"] == "Helper"

    def test_lwc_files(self, client, scripted):
        scripted()
        data = client.post("/api/import/parse", json={
            "api_name": "card", "kind": "lwc",
            "files": {
                "lwc/card/card.js": "import { LightningElement } from 'lwc'; export default class Card extends LightningElement {}",
                "lwc/card/card.html": "<template><p>hi</p></template>",
                "lwc/card/card.js-meta.xml": (
                    '<?xml version="1.0"?><LightningComponentBundle xmlns="http://soap.sforce.com/2006/04/metadata">'
                    "<apiVersion>62.0</apiVersion><isExposed>true</isExposed></LightningComponentBundle>"
                ),
            },
        }).json()
        assert data["kind"] == "lwc" and data["api_version"] == "62.0"

    def test_lwc_without_html_is_400(self, client, scripted):
        scripted()
        r = client.post("/api/import/parse", json={"api_name": "c", "kind": "lwc", "files": {"c.js": "x"}})
        assert r.status_code == 400

    def test_missing_xml_is_400(self, client, scripted):
        scripted()
        assert client.post("/api/import/parse", json={"api_name": "X", "kind": "flow"}).status_code == 400

    def test_unparseable_flow_is_refused(self, client, scripted):
        scripted()
        r = client.post("/api/import/parse", json={"api_name": "X", "kind": "flow", "xml": "<nope"})
        assert r.status_code == 422

    def test_custom_object_opens_as_a_plan(self, client, scripted):
        scripted()
        xml = generate_object(CustomObject.model_validate(VALID_OBJECT), [])
        data = client.post(
            "/api/import/parse",
            json={"api_name": OBJECT_NAME, "kind": "custom_object", "xml": xml},
        ).json()
        assert data["opened_as"] == "plan" and data["session_id"] in server.PLAN_SESSIONS

    def test_large_object_asks_for_fields_statelessly(self, client, scripted, monkeypatch):
        scripted()
        monkeypatch.setattr(server, "FIELD_PICK_THRESHOLD", 0)
        xml = generate_object(CustomObject.model_validate(VALID_OBJECT), [CustomField.model_validate(VALID_FIELD)])
        before = len(server.OBJECT_PICKS)
        body = {"api_name": OBJECT_NAME, "kind": "custom_object", "xml": xml}
        data = client.post("/api/import/parse", json=body).json()
        assert data["opened_as"] == "choose_fields"
        assert data["pick_id"] is None and len(server.OBJECT_PICKS) == before
        again = client.post("/api/import/parse", json={**body, "fields": ["Amount__c"]}).json()
        assert again["opened_as"] == "plan"


class TestSurveyScore:
    def test_scores_posted_flows(self, client, scripted):
        data = approved_flow(client, scripted)
        files, _ = server._deploy_files(server.SESSIONS[data["session_id"]])
        xml = next(iter(files.values()))
        body = client.post("/api/survey/score", json={"flows": {"One": xml, "Two": "<nope"}}).json()
        assert body["total"] == 2 and body["parsed"] == 1
        assert "flows" in body["report"]

    def test_empty_is_400(self, client):
        assert client.post("/api/survey/score", json={"flows": {}}).status_code == 400


class TestPortalPlanFromPreflight:
    BODY = {"site_name": "Help Center", "headline": "Hi"}

    def test_planned_steps_without_credentials(self, client):
        body = client.post("/api/portal/plan-from-preflight", json={
            **self.BODY, "preflight": {"can_proceed": True, "checks": []},
        }).json()
        assert body["ok"] is True
        assert body["steps"] and all(s["status"] == "planned" for s in body["steps"])

    def test_a_blocked_preflight_plans_nothing(self, client):
        body = client.post("/api/portal/plan-from-preflight", json={
            **self.BODY, "preflight": {"can_proceed": False, "checks": [{"id": "x"}]},
        }).json()
        assert body["ok"] is False and body["steps"] == []

    def test_messaging_adds_its_step(self, client):
        pre = {"can_proceed": True, "checks": []}
        plain = client.post("/api/portal/plan-from-preflight", json={**self.BODY, "preflight": pre}).json()
        chat = client.post("/api/portal/plan-from-preflight", json={**self.BODY, "chat": True, "preflight": pre}).json()
        assert len(chat["steps"]) == len(plain["steps"]) + 1


# --------------------------------------------------------------------------
# hosted mode and CORS
# --------------------------------------------------------------------------


@pytest.fixture
def hosted(client, monkeypatch):
    monkeypatch.setenv(server.hosted.HOSTED_ENV, "1")
    monkeypatch.setenv(server.hosted.CORS_ENV, EXTENSION)
    return client


class TestHostedMode:
    def test_default_config_is_unchanged(self, client):
        cfg = client.get("/api/config").json()
        assert {"clientId", "orgs", "sf_cli", "env_file"} <= set(cfg)
        assert "hosted" not in cfg

    def test_config_drops_every_org_field(self, hosted, monkeypatch):
        monkeypatch.setenv("SF_CLIENT_ID", "3MVG.secretish")
        cfg = hosted.get("/api/config").json()
        assert cfg["hosted"] is True
        for key in ("clientId", "orgs", "sf_cli", "env_file"):
            assert key not in cfg
        assert "3MVG" not in str(cfg)

    @pytest.mark.parametrize("path", [
        "/api/flows", "/api/apex-classes", "/api/apex-triggers", "/api/lwc-components",
        "/api/permission-sets", "/api/prompt-templates", "/api/custom-objects", "/api/custom-fields",
        "/api/survey/start", "/api/org-summary/start", "/api/import/start", "/api/import/object-fields",
        "/api/validate/start", "/api/deploy/start", "/api/plan/validate/start", "/api/plan/deploy/start",
        "/api/plan/agent/publish/start", "/api/portal/preflight", "/api/portal/plan", "/api/portal/apply",
    ])
    def test_org_endpoints_are_refused_even_with_an_empty_body(self, hosted, path):
        r = hosted.post(path, json={})
        assert r.status_code == 403 and "extension" in r.json()["detail"]

    @pytest.mark.parametrize("path", ["/api/survey/status", "/api/import/status", "/api/deploy/status",
                                      "/api/portal/status", "/api/plan/validate/status"])
    def test_org_status_endpoints_are_refused(self, hosted, path):
        assert hosted.get(path, params={"job_id": "x", "session_id": "x"}).status_code == 403

    @pytest.mark.parametrize("field,value", [
        ("instance_url", "https://acme.my.salesforce.com"), ("access_token", "00Dxx!tok"), ("org", "dev"),
    ])
    def test_org_fields_on_any_endpoint_are_rejected(self, hosted, scripted, field, value):
        scripted(VALID)
        r = hosted.post("/api/design/start", json={"request": "x", field: value})
        assert r.status_code == 400
        assert value not in r.text

    def test_empty_org_fields_are_tolerated(self, hosted, scripted):
        scripted(VALID)
        r = hosted.post("/api/design/start", json={"request": "x", "org": None, "instance_url": ""})
        assert r.status_code == 200

    def test_credentials_itself_refuses(self, hosted):
        with pytest.raises(server.HTTPException) as exc:
            server.credentials("dev", "https://x", "tok")
        assert exc.value.status_code == 403

    def test_llm_only_endpoints_still_work(self, hosted, scripted):
        data = approved_flow(hosted, scripted)
        assert hosted.get(f"/api/session/{data['session_id']}/bundle").status_code == 200
        assert hosted.post("/api/survey/score", json={"flows": {"A": "<Flow/>"}}).status_code == 200
        assert hosted.get("/api/config").status_code == 200

    def test_sessions_hold_no_credentials(self, hosted, scripted):
        data = approved_flow(hosted, scripted)
        session = server.SESSIONS[data["session_id"]]
        assert session.pending_deploy is None
        blob = repr(vars(session))
        assert "access_token" not in blob and "instance_url" not in blob

    def test_import_parse_origin_is_not_an_instance_url(self, hosted, scripted):
        scripted()
        r = hosted.post("/api/import/parse", json={"api_name": "H", "kind": "apex", "xml": "public class H {}"})
        assert r.status_code == 200


class TestCors:
    def test_no_cors_headers_by_default(self, client):
        r = client.get("/api/config", headers={"Origin": EXTENSION})
        assert "access-control-allow-origin" not in r.headers

    def test_preflight_from_the_configured_origin(self, hosted):
        r = hosted.options("/api/design/start", headers={
            "Origin": EXTENSION, "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type,x-client-id",
        })
        assert r.status_code == 204
        assert r.headers["access-control-allow-origin"] == EXTENSION
        allowed = r.headers["access-control-allow-headers"].lower()
        assert "x-client-id" in allowed and "content-type" in allowed
        assert "POST" in r.headers["access-control-allow-methods"]

    def test_simple_request_gets_the_header(self, hosted):
        r = hosted.get("/api/config", headers={"Origin": EXTENSION})
        assert r.headers["access-control-allow-origin"] == EXTENSION
        assert "Origin" in r.headers["vary"]

    def test_errors_carry_the_header_too(self, hosted):
        r = hosted.post("/api/flows", json={}, headers={"Origin": EXTENSION})
        assert r.status_code == 403 and r.headers["access-control-allow-origin"] == EXTENSION

    def test_other_origins_get_nothing(self, hosted):
        for origin in ("https://evil.example", "chrome-extension://other", EXTENSION + ".evil"):
            r = hosted.get("/api/config", headers={"Origin": origin})
            assert "access-control-allow-origin" not in r.headers
        pre = hosted.options("/api/design/start", headers={
            "Origin": "https://evil.example", "Access-Control-Request-Method": "POST",
        })
        assert "access-control-allow-origin" not in pre.headers

    def test_several_origins_and_trailing_slash(self, client, monkeypatch):
        monkeypatch.setenv(server.hosted.CORS_ENV, f"https://a.example/, {EXTENSION}")
        assert client.get("/api/config", headers={"Origin": "https://a.example"}).headers[
            "access-control-allow-origin"] == "https://a.example"
        assert client.get("/api/config", headers={"Origin": EXTENSION}).headers[
            "access-control-allow-origin"] == EXTENSION


class TestExtensionContract:
    def test_session_bundle_has_kind_and_name(self, client, scripted):
        data = approved_flow(client, scripted)
        body = client.get(f"/api/session/{data['session_id']}/bundle").json()
        assert body["kind"] == "flow" and body["name"] == data["api_name"]

    def test_plan_bundle_components_and_prompt_templates(self, client, typed_scripted):
        typed_scripted(plan=BUNDLE_PLAN, object=VALID_OBJECT, field=VALID_FIELD, apex=VALID_APEX)
        plan = make_plan(client, grant_permission_set="new", permission_set_label="Invoice_Access")
        session = execute(client, plan["plan_id"])
        sid = session["session_id"]
        client.post("/api/plan/approve", json={"session_id": sid, "version": session["version"]})
        body = client.get(f"/api/plan/session/{sid}/bundle").json()
        types = {c["type"] for c in body["components"]}
        assert types == {"object", "field", "apex", "permission_set"}
        field = next(c for c in body["components"] if c["type"] == "field")
        assert field["object"] == "Invoice__c" and body["prompt_templates"] == []

    def test_plan_deploy_result_accepts_contract_body_and_returns_plan(self, client, typed_scripted):
        typed_scripted(plan=ONE_STEP_PLAN, flow=VALID)
        session = execute(client, make_plan(client)["plan_id"])
        sid = session["session_id"]
        r = client.post(f"/api/plan/session/{sid}/deploy-result", json={
            "success": True, "status": "Succeeded", "failures": [], "check_only": False,
            "version": session["version"],
        }).json()
        assert r["plan"]["session_id"] == sid and r["plan"]["deployed"] is True
        client.post(f"/api/plan/session/{sid}/deploy-result", json={
            "success": True, "status": "Succeeded", "failures": [], "check_only": True, "version": 1,
        })

    def test_parse_accepts_zip_paths_and_apex_body(self, client, scripted):
        name, xml = TestImportParse().flow_xml(client, scripted)
        scripted()
        r = client.post("/api/import/parse", json={
            "kind": "flow", "api_name": name, "files": {"package.xml": "<x/>", f"flows/{name}.flow": xml},
        }).json()
        assert r["done"] is True and r["imported"] is True
        a = client.post("/api/import/parse", json={
            "kind": "apex", "api_name": "H", "files": {"body": "public class H {}"},
        }).json()
        assert a["done"] is True and a["kind"] == "apex"
        obj = generate_object(CustomObject.model_validate(VALID_OBJECT), [])
        o = client.post("/api/import/parse", json={
            "kind": "custom_object", "api_name": OBJECT_NAME, "files": {f"objects/{OBJECT_NAME}.object": obj},
        }).json()
        assert o["opened_as"] == "plan"
