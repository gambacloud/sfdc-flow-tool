"""
mcp_server.py's tools, driven directly (asyncio.run, matching this repo's own
convention in test_pipeline.py rather than pulling in pytest-asyncio) - proves
the in-process wiring (planner -> execute_plan -> _bundle_files_and_types ->
validate_bundle) works the same way server.py's own /api/plan/* routes do,
without needing a running FastAPI process or a real org.
"""

import asyncio

import pytest

import mcp_server
from flowtool.sfdc import DeployResult
from tests.test_ir_apex_generator import VALID as VALID_APEX
from tests.test_ir_object_generator import VALID_FIELD, VALID_OBJECT
from tests.test_llm import TypedScriptedProvider

BUNDLE_PLAN = {
    "steps": [
        {"artifact_type": "object", "name": "Object", "brief": "an Invoice object"},
        {"artifact_type": "field", "name": "Field", "brief": "an Amount field",
         "depends_on": ["Object"]},
        {"artifact_type": "apex", "name": "Apex", "brief": "a helper class"},
    ]
}


@pytest.fixture(autouse=True)
def clean_builds():
    mcp_server.BUILDS.clear()
    yield
    mcp_server.BUILDS.clear()


@pytest.fixture
def typed_scripted(monkeypatch):
    def install(**payloads_by_type):
        provider = TypedScriptedProvider(**payloads_by_type)
        monkeypatch.setattr(mcp_server, "build_provider", lambda *_a, **_k: provider)
        return provider

    return install


class TestBuildTool:
    def test_build_returns_a_step_summary_per_artifact(self, typed_scripted):
        typed_scripted(plan=BUNDLE_PLAN, object=VALID_OBJECT, field=VALID_FIELD, apex=VALID_APEX)

        result = asyncio.run(
            mcp_server.build("an Invoice object, an Amount field, a helper class")
        )

        assert result["build_id"] in mcp_server.BUILDS
        by_name = {s["name"]: s for s in result["steps"]}
        assert by_name["Object"]["api_name"] == "Invoice__c"
        assert by_name["Field"]["object_api_name"] == "Invoice__c"
        assert by_name["Apex"]["api_name"] == VALID_APEX["api_name"]


class TestValidateTool:
    def test_unknown_build_id_is_rejected(self):
        with pytest.raises(ValueError, match="Unknown build_id"):
            asyncio.run(mcp_server.validate("nope", "dev"))

    def test_validate_bundles_every_step_and_reports_the_org_s_answer(
        self, typed_scripted, monkeypatch,
    ):
        typed_scripted(plan=BUNDLE_PLAN, object=VALID_OBJECT, field=VALID_FIELD, apex=VALID_APEX)
        built = asyncio.run(mcp_server.build("bundle it"))

        seen_types = {}

        async def fake_validate_bundle(url, token, files, types, api_version="62.0", check_only=True):
            seen_types.update(types)
            assert check_only is True
            return DeployResult("1", "Succeeded", True)

        monkeypatch.setattr(mcp_server, "validate_bundle", fake_validate_bundle)
        monkeypatch.setattr(mcp_server, "_org_credentials", lambda alias: ("https://x", "tok"))

        result = asyncio.run(mcp_server.validate(built["build_id"], "dev"))

        assert result["success"] is True
        # Field targets the Object step's object, created in this same
        # build, so it's embedded rather than its own CustomField member -
        # same rule server.py's _bundle_files_and_types follows.
        assert set(seen_types) == {"CustomObject", "ApexClass"}


class TestDeployTool:
    def test_deploy_without_confirm_is_rejected(self, typed_scripted):
        typed_scripted(plan=BUNDLE_PLAN, object=VALID_OBJECT, field=VALID_FIELD, apex=VALID_APEX)
        built = asyncio.run(mcp_server.build("bundle it"))

        with pytest.raises(ValueError, match="confirm"):
            asyncio.run(mcp_server.deploy(built["build_id"], "dev", False))

    def test_confirmed_deploy_returns_setup_links(self, typed_scripted, monkeypatch):
        typed_scripted(plan=BUNDLE_PLAN, object=VALID_OBJECT, field=VALID_FIELD, apex=VALID_APEX)
        built = asyncio.run(mcp_server.build("bundle it"))

        async def fake_validate_bundle(url, token, files, types, api_version="62.0", check_only=True):
            assert check_only is False
            return DeployResult("1", "Succeeded", True)

        async def fake_component_setup_url(
            instance_url, session_id, artifact_type, api_name, api_version="62.0",
            object_api_name=None,
        ):
            return f"https://x/setup/{artifact_type}/{api_name}"

        monkeypatch.setattr(mcp_server, "validate_bundle", fake_validate_bundle)
        monkeypatch.setattr(mcp_server, "component_setup_url", fake_component_setup_url)
        monkeypatch.setattr(mcp_server, "_org_credentials", lambda alias: ("https://x", "tok"))

        result = asyncio.run(mcp_server.deploy(built["build_id"], "dev", True))

        assert result["success"] is True
        assert result["setup_urls"] == {
            "Object": "https://x/setup/object/Invoice__c",
            "Field": "https://x/setup/field/Amount__c",
            "Apex": "https://x/setup/apex/Invoice_Helper",
        }
