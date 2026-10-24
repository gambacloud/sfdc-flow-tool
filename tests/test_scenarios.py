"""
Test scenarios: generated Anonymous Apex, run in a rolled-back transaction,
with governor limits read from the debug log.
"""

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

import server
from flowtool import anon_apex
from flowtool.ir_apex import ApexClass
from flowtool.planner import Plan, PlanStep, StepResult
from flowtool.scenarios import ScenarioGenerator, TestScenario, TestScenarios
from tests.test_ir_apex_generator import VALID as VALID_APEX
from tests.test_llm import ScriptedProvider

SCENARIO = {
    "title": "Closing an opportunity creates a task",
    "kind": "positive",
    "apex": "Account a = new Account(Name='t'); insert a;\nSystem.assert(a.Id != null);",
    "expect": "The account is created.",
}
BULK = {**SCENARIO, "title": "200 at once", "kind": "bulk"}
PAYLOAD = {"scenarios": [SCENARIO, BULK], "manual_notes": "Check the async path by hand."}

LOG = """\
12:00:00.0 (1)|EXECUTION_STARTED
12:00:00.9 (9)|CUMULATIVE_LIMIT_USAGE
12:00:00.9 (9)|LIMIT_USAGE_FOR_NS|(default)|
  Number of SOQL queries: 100 out of 100 ******* CLOSE TO LIMIT
  Number of query rows: 0 out of 50000
  Number of DML statements: 140 out of 150
  Maximum CPU time: 8000 out of 10000
  Maximum heap size: 0 out of 6000000
  Number of Publish Immediate DML: 0 out of 150

12:00:00.9 (9)|LIMIT_USAGE_FOR_NS|Other|
  Number of SOQL queries: 50 out of 100

12:00:00.9 (9)|CUMULATIVE_LIMIT_USAGE_END
"""


def soap(*, compiled="true", success="true", problem="", exception="", log=""):
    return (
        '<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/"'
        ' xmlns="http://soap.sforce.com/2006/08/apex"><soapenv:Header><DebuggingInfo>'
        f"<debugLog>{log}</debugLog></DebuggingInfo></soapenv:Header><soapenv:Body>"
        "<executeAnonymousResponse><result>"
        f"<column>1</column><compileProblem>{problem}</compileProblem><compiled>{compiled}</compiled>"
        f"<exceptionMessage>{exception}</exceptionMessage><line>4</line><success>{success}</success>"
        "</result></executeAnonymousResponse></soapenv:Body></soapenv:Envelope>"
    )


class TestRunner:
    def test_limits_come_from_the_default_namespace_only(self):
        limits = {lim.name: lim for lim in anon_apex.parse_limits(LOG)}
        assert limits["Number of SOQL queries"].used == 100  # the "CLOSE TO LIMIT" line
        assert "Number of Publish Immediate DML" in limits
        assert sum(1 for lim in anon_apex.parse_limits(LOG) if lim.name == "Number of SOQL queries") == 1

    def test_hot_limits_become_warnings_with_advice(self):
        run = anon_apex.parse_response(soap(log=LOG))
        assert run.success
        levels = {lim.name: lim.level for lim in run.limits}
        assert levels["Number of DML statements"] == "critical"
        assert levels["Maximum CPU time"] == "warn"
        assert levels["Number of SOQL queries"] == "critical"
        text = "\n".join(run.warnings)
        assert "140 of 150" in text and "DML per record" in text
        assert "SOQL queries: 100 of 100" in text

    def test_compile_error_and_exception_are_reported(self):
        bad = anon_apex.parse_response(soap(compiled="false", success="false", problem="Unexpected token"))
        assert not bad.compiled and "Unexpected token" in bad.problem and "line 2" in bad.problem
        boom = anon_apex.parse_response(soap(success="false", exception="System.AssertException: nope"))
        assert boom.compiled and not boom.success and "AssertException" in boom.problem

    def test_script_is_wrapped_in_a_rolled_back_savepoint(self):
        wrapped = anon_apex.wrap_script("insert new Account(Name='x');")
        assert wrapped.index("setSavepoint") < wrapped.index("insert new Account") < wrapped.index("rollback")
        assert "finally" in wrapped

    def test_request_escapes_the_script(self):
        body = anon_apex._envelope("tok", "if (a < b && c) {}")
        assert "a &lt; b &amp;&amp; c" in body


class TestModel:
    def test_forbidden_constructs_are_rejected(self):
        for bad in ("Database.rollback(sp);", "System.enqueueJob(new J());", "Database.executeBatch(b);"):
            with pytest.raises(ValidationError):
                TestScenario(**{**SCENARIO, "apex": bad})

    def test_needs_at_least_one_scenario(self):
        with pytest.raises(ValidationError):
            TestScenarios(scenarios=[])

    def test_generator_gets_the_built_components(self):
        provider = ScriptedProvider(PAYLOAD)
        plan = Plan(steps=[PlanStep(artifact_type="apex", name="Helper", brief="x")], how_to_test="try it")
        steps = [StepResult(step=plan.steps[0], value=ApexClass(**VALID_APEX), repairs=0, messages=[])]
        result = ScenarioGenerator(provider).generate(plan, steps, request="make invoices")
        assert [s.kind for s in result.value.scenarios] == ["positive", "bulk"]
        sent = provider.calls[0][0].content
        assert "Invoice_Helper" in sent and "make invoices" in sent and "try it" in sent

    def test_a_forbidden_script_is_repaired(self):
        provider = ScriptedProvider(
            {"scenarios": [{**SCENARIO, "apex": "Database.rollback(sp);"}]}, PAYLOAD,
        )
        plan = Plan(steps=[PlanStep(artifact_type="apex", name="Helper", brief="x")])
        steps = [StepResult(step=plan.steps[0], value=ApexClass(**VALID_APEX), repairs=0, messages=[])]
        result = ScenarioGenerator(provider).generate(plan, steps)
        assert result.repairs == 1


@pytest.fixture
def client(monkeypatch):
    server.PLAN_SESSIONS.clear()
    monkeypatch.setattr(
        server, "credentials", lambda org, instance_url=None, access_token=None: ("https://x", "tok")
    )
    with TestClient(server.app) as c:
        yield c


def make_session(*payloads, deployed=True):
    plan = Plan(steps=[PlanStep(artifact_type="apex", name="Helper", brief="x")])
    session = server.PlanSession(
        provider=ScriptedProvider(*payloads), plan=plan,
        steps=[StepResult(step=plan.steps[0], value=ApexClass(**VALID_APEX), repairs=0, messages=[])],
    )
    if deployed:
        session.deployed_version = session.version
    server.PLAN_SESSIONS["s1"] = session
    return session


def generate(client):
    assert client.post("/api/plan/scenarios/start", json={"session_id": "s1"}).status_code == 200
    for _ in range(50):
        r = client.get("/api/plan/scenarios/status", params={"session_id": "s1"})
        if r.json().get("done"):
            return r.json()
    raise AssertionError("never completed")


class TestEndpoints:
    def test_generate_then_plan_view_carries_them(self, client):
        session = make_session(PAYLOAD)
        data = generate(client)
        assert len(data["scenarios"]["scenarios"]) == 2
        assert server.plan_view("s1", session)["scenarios"]["manual_notes"]

    def test_revising_hides_stale_scenarios(self, client):
        session = make_session(PAYLOAD)
        generate(client)
        session.version += 1
        assert server.plan_view("s1", session)["scenarios"] is None

    def test_run_needs_scenarios_and_a_deploy(self, client):
        make_session(PAYLOAD)
        r = client.post("/api/plan/scenarios/run", json={"session_id": "s1", "index": 0})
        assert r.status_code == 400
        make_session(PAYLOAD, deployed=False)
        generate(client)
        r = client.post("/api/plan/scenarios/run", json={"session_id": "s1", "index": 0})
        assert r.status_code == 409

    def test_production_is_refused(self, client, monkeypatch):
        make_session(PAYLOAD)
        generate(client)

        async def prod(*_a, **_k):
            return True

        async def must_not_run(*_a, **_k):
            raise AssertionError("ran in production")

        monkeypatch.setattr(anon_apex, "is_production", prod)
        monkeypatch.setattr(anon_apex, "run_anonymous", must_not_run)
        r = client.post("/api/plan/scenarios/run", json={"session_id": "s1", "index": 0})
        assert r.status_code == 403

    def test_run_returns_limits(self, client, monkeypatch):
        make_session(PAYLOAD)
        generate(client)
        seen = {}

        async def sandbox(*_a, **_k):
            return False

        async def fake_run(url, token, body, api_version="64.0"):
            seen["body"] = body
            return anon_apex.parse_response(soap(log=LOG))

        monkeypatch.setattr(anon_apex, "is_production", sandbox)
        monkeypatch.setattr(anon_apex, "run_anonymous", fake_run)
        r = client.post("/api/plan/scenarios/run", json={"session_id": "s1", "index": 1})
        assert r.status_code == 200
        assert seen["body"] == BULK["apex"]
        assert r.json()["warnings"] and r.json()["limits"][0]["percent"] == 100.0

    def test_report_includes_scenarios(self, client):
        make_session(PAYLOAD)
        generate(client)
        html = client.get("/api/plan/session/s1/report").text
        assert "Test scenarios (Anonymous Apex)" in html and "200 at once" in html


class TestScriptEndpoint:
    def test_returns_script_and_deployed_flag(self, client):
        make_session(PAYLOAD)
        generate(client)
        data = client.get("/api/plan/session/s1/scenarios/1").json()
        assert data["apex"] == BULK["apex"] and data["deployed"] is True

    def test_needs_scenarios_and_a_valid_index(self, client):
        make_session(PAYLOAD)
        assert client.get("/api/plan/session/s1/scenarios/0").status_code == 400
        generate(client)
        assert client.get("/api/plan/session/s1/scenarios/9").status_code == 404
