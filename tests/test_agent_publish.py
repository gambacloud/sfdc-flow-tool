"""Publishing + activating an employee agent over HTTP (flowtool/agent_publish.py), mocked."""

import asyncio
import io
import json
import zipfile

import httpx
import pytest

import server
from flowtool.agent_publish import AgentPublishError, AgentPublisher, with_target
from flowtool.ir_agent import Agent
from flowtool.planner import Plan, PlanStep, StepResult
from flowtool.sfdc import ComponentProblem, DeployResult
from tests.test_ir_agent import VALID_AGENT
from tests.test_llm import ScriptedProvider

ORG = "https://acme.my.salesforce.com"
JWT = "aaa.bbb.ccc"
META = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<AiAuthoringBundle xmlns="http://soap.sforce.com/2006/04/metadata">\n'
    "    <bundleType>AGENT</bundleType>\n"
    "    <target>Help_Agent.v1</target>\n"
    "</AiAuthoringBundle>\n"
)
SCRIPT = 'config:\n    agent_type: "AgentforceEmployeeAgent"\n'


def run(coro):
    return asyncio.run(coro)


def zip_of(files):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for path, text in files.items():
            z.writestr(path, text)
    return buf.getvalue()


class FakeMetadata:
    def __init__(self, bundle=True, deploy_ok=True):
        self.bundle = bundle
        self.deploy_ok = deploy_ok
        self.deployed = []

    async def start_retrieve_types(self, types):
        self.types = types
        return "job"

    async def wait_for_retrieve(self, job):
        files = {"package.xml": "<Package/>"}
        if self.bundle:
            files["aiAuthoringBundles/Help_Agent/Help_Agent.agent"] = SCRIPT
            files["aiAuthoringBundles/Help_Agent/Help_Agent.bundle-meta.xml"] = META
        return zip_of(files)

    async def deploy_and_wait(self, zip_bytes, check_only=True):
        archive = zipfile.ZipFile(io.BytesIO(zip_bytes))
        self.deployed.append(({n: archive.read(n).decode() for n in archive.namelist()}, check_only))
        if self.deploy_ok:
            return DeployResult("1", "Succeeded", True)
        return DeployResult("1", "Failed", False, [ComponentProblem("Help_Agent", "nope", "Error")])

    async def aclose(self):
        pass


class FakeOrg:
    """The org's REST API and api.salesforce.com, as one MockTransport handler."""

    def __init__(self, agent_type="AgentforceEmployeeAgent", existing_bot=None, compile_ok=True,
                 publish_status=200, final_status="Active"):
        self.agent_type = agent_type
        self.existing_bot = existing_bot
        self.compile_ok = compile_ok
        self.publish_status = publish_status
        self.final_status = final_status
        self.activated = False
        self.calls = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        body = json.loads(request.content) if request.content else None
        self.calls.append((request.method, url.split("?")[0], body, dict(request.headers)))
        if url.endswith("/agentforce/bootstrap/nameduser"):
            assert request.headers["cookie"] == "sid=TOKEN"
            return httpx.Response(200, json={"access_token": JWT, "token_format": "jwt"})
        if url == "https://api.salesforce.com/einstein/ai-agent/v1.1/authoring/scripts":
            assert request.headers["authorization"] == f"Bearer {JWT}"
            assert body["assets"][0]["content"] == SCRIPT
            if not self.compile_ok:
                return httpx.Response(200, json={"status": "failure", "errors": [{"description": "bad topic"}]})
            return httpx.Response(200, json={"status": "success", "compiledArtifact": {
                "globalConfiguration": {"developerName": "Help_Agent", "agentType": self.agent_type},
                "agentVersion": {"nodes": []},
            }})
        if url.startswith("https://api.salesforce.com/einstein/ai-agent/v1.1/authoring/agents"):
            assert request.headers["x-client-name"] == "afdx"
            assert body["instanceConfig"]["endpoint"] == ORG
            if self.publish_status != 200:
                return httpx.Response(self.publish_status, json={"errorMessage": "no access"})
            return httpx.Response(200, json={"botId": "0Xx1", "botVersionId": "0X92"})
        if "/query" in url:
            q = request.url.params["q"]
            if "FROM BotDefinition" in q:
                return httpx.Response(200, json={"records": [{"Id": self.existing_bot}] if self.existing_bot else []})
            if q.startswith("SELECT Status FROM BotVersion"):
                return httpx.Response(200, json={"records": [{"Status": self.final_status if self.activated else "Inactive"}]})
            return httpx.Response(200, json={"records": [
                {"Id": "0X92", "DeveloperName": "v2", "VersionNumber": 2, "Status": "Inactive"}]})
        if url.endswith("/connect/bot-versions/0X92/activation"):
            assert body == {"status": "Active"}
            self.activated = True
            return httpx.Response(200, json={"success": True, "isActivated": True})
        return httpx.Response(404, json={"message": f"unexpected {url}"})


def publish(org, metadata=None, activate=True):
    async def go():
        http = httpx.AsyncClient(transport=httpx.MockTransport(org))
        async with AgentPublisher(ORG, "TOKEN", "65.0", http=http, metadata=metadata or FakeMetadata()) as p:
            try:
                return await p.publish("Help_Agent", activate=activate)
            finally:
                await http.aclose()
    return run(go())


def test_a_first_publish_creates_the_agent_links_the_bundle_and_activates_it():
    org, metadata = FakeOrg(), FakeMetadata()
    result = publish(org, metadata)
    assert result.first_publish and result.version_number == 2 and result.status == "Active"
    urls = [u for _, u, _, _ in org.calls]
    assert "https://api.salesforce.com/einstein/ai-agent/v1.1/authoring/agents" in urls
    files, check_only = metadata.deployed[0]
    assert check_only is False
    meta = files["aiAuthoringBundles/Help_Agent/Help_Agent.bundle-meta.xml"]
    assert "<target>Help_Agent.v2</target>" in meta and "Help_Agent.v1" not in meta
    assert files["aiAuthoringBundles/Help_Agent/Help_Agent.agent"] == SCRIPT
    assert metadata.types == {"AiAuthoringBundle": ["Help_Agent"]}


def test_a_republish_adds_a_version_to_the_existing_agent():
    org = FakeOrg(existing_bot="0XxOLD")
    result = publish(org)
    assert not result.first_publish
    assert any(u.endswith("/authoring/agents/0XxOLD/versions") for _, u, _, _ in org.calls)


def test_publish_without_activation_leaves_the_version_inactive():
    org = FakeOrg()
    result = publish(org, activate=False)
    assert result.status == "Inactive" and not org.activated


def test_a_service_agent_is_refused_before_anything_is_published():
    org = FakeOrg(agent_type="AgentforceServiceAgent")
    with pytest.raises(AgentPublishError, match="Agentforce Builder"):
        publish(org)
    assert not any("/authoring/agents" in u for _, u, _, _ in org.calls)


def test_compile_errors_are_reported():
    with pytest.raises(AgentPublishError, match="bad topic"):
        publish(FakeOrg(compile_ok=False))


def test_a_rejected_publish_says_what_the_api_said():
    with pytest.raises(AgentPublishError, match="no access"):
        publish(FakeOrg(publish_status=403))


def test_a_bundle_that_is_not_in_the_org_is_reported():
    with pytest.raises(AgentPublishError, match="not deployed"):
        publish(FakeOrg(), FakeMetadata(bundle=False))


def test_a_failed_redeploy_of_the_bundle_is_reported():
    with pytest.raises(AgentPublishError, match="redeploying the bundle failed"):
        publish(FakeOrg(), FakeMetadata(deploy_ok=False))


def test_an_activation_the_org_does_not_show_is_an_error():
    with pytest.raises(AgentPublishError, match="still Inactive"):
        publish(FakeOrg(final_status="Inactive"))


def test_with_target_adds_or_replaces_the_target():
    fresh = META.replace("    <target>Help_Agent.v1</target>\n", "")
    assert with_target(fresh, "A.v1").count("<target>A.v1</target>") == 1
    assert with_target(META, "A.v3").count("<target>") == 1


# ---- the plan endpoints ----

def agent(kind="employee"):
    return Agent.model_validate({**VALID_AGENT, "agent_type": kind})


def plan_session(kind="employee", deployed=True):
    session = server.PlanSession(
        provider=ScriptedProvider([]),
        plan=Plan(steps=[PlanStep(artifact_type="agent", name="Agent", brief="x")]),
        steps=[StepResult(step=PlanStep(artifact_type="agent", name="Agent", brief="x"), value=agent(kind), repairs=0, messages=[])],
    )
    if deployed:
        session.deployed_version = session.version
    return session


@pytest.fixture
def registered(monkeypatch):
    added = []

    def register(session):
        server.PLAN_SESSIONS["publish-test"] = session
        added.append("publish-test")
        return session
    yield register
    for key in added:
        server.PLAN_SESSIONS.pop(key, None)


def request(**kw):
    return server.AgentPublishRequest(session_id="publish-test", agent="Help_Agent",
                                      instance_url=ORG, access_token="TOKEN", **kw)


def test_the_plan_view_says_whether_the_shown_version_is_deployed():
    session = plan_session(deployed=False)
    assert server.plan_view("x", session)["deployed"] is False
    session.deployed_version = session.version
    assert server.plan_view("x", session)["deployed"] is True
    session.version += 1  # revised after the deploy: publish again only after deploying again
    assert server.plan_view("x", session)["deployed"] is False


def test_publish_runs_in_the_background_and_reports_the_version(registered, monkeypatch):
    session = registered(plan_session())
    seen = {}

    async def fake_publish(url, token, name, api_version="65.0", activate=True):
        seen.update(url=url, token=token, name=name, api_version=api_version)
        from flowtool.agent_publish import PublishResult
        return PublishResult("Help_Agent", "0Xx1", "0X92", 1, "v1", "Active", True)
    monkeypatch.setattr(server, "publish_employee_agent", fake_publish)

    async def go():
        await server.plan_agent_publish_start(request())
        await session.pending_publish
        return await server.plan_agent_publish_status("publish-test")
    status = run(go())
    assert status["success"] and status["version_number"] == 1 and status["status"] == "Active"
    assert seen == {"url": ORG, "token": "TOKEN", "name": "Help_Agent", "api_version": "65.0"}
    assert status["plan"]["agent_publish"]["Help_Agent"]["version_name"] == "v1"


def test_a_publish_error_is_returned_not_raised(registered, monkeypatch):
    session = registered(plan_session())

    async def failing(*a, **k):
        raise AgentPublishError("Compiling failed")
    monkeypatch.setattr(server, "publish_employee_agent", failing)

    async def go():
        await server.plan_agent_publish_start(request())
        await asyncio.gather(session.pending_publish, return_exceptions=True)
        return await server.plan_agent_publish_status("publish-test")
    status = run(go())
    assert status == {"done": True, "success": False, "agent": "Help_Agent", "error": "Compiling failed"}


def test_a_service_agent_cannot_be_published_from_the_plan(registered):
    registered(plan_session(kind="service"))
    with pytest.raises(server.HTTPException) as err:
        run(server.plan_agent_publish_start(request()))
    assert err.value.status_code == 400 and "Agentforce Builder" in err.value.detail


def test_publish_needs_a_real_deploy_first(registered):
    registered(plan_session(deployed=False))
    with pytest.raises(server.HTTPException) as err:
        run(server.plan_agent_publish_start(request()))
    assert err.value.status_code == 403


def test_an_unknown_agent_is_a_404(registered):
    registered(plan_session())
    with pytest.raises(server.HTTPException) as err:
        run(server.plan_agent_publish_start(server.AgentPublishRequest(
            session_id="publish-test", agent="Nope", instance_url=ORG, access_token="TOKEN")))
    assert err.value.status_code == 404
