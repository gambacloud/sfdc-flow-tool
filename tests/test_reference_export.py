"""
The optional external reference (JIRA key) and the downloadable Metadata API
zip - flowtool/reference.py plus /api/reference and the /package endpoints.
"""

import io
import zipfile

import pytest

import server
from flowtool.ir import Flow
from flowtool.ir_apex import ApexClass
from flowtool.ir_lwc import LightningComponent
from flowtool.ir_object import CustomField
from flowtool.reference import clean_reference, tag_description, tag_value
from tests.test_ir_apex_generator import VALID as VALID_APEX
from tests.test_ir_lwc_generator import VALID as VALID_LWC
from tests.test_ir_object_generator import VALID_FIELD, VALID_OBJECT
from tests.test_llm import VALID as VALID_FLOW
from tests.test_report import FLOW_IR
from tests.test_server_plan import (  # noqa: F401 - fixtures
    BUNDLE_PLAN, client, execute, make_plan, scripted, typed_scripted,
)


class TestCleanReference:
    def test_blank_is_none(self):
        assert clean_reference(None) is None
        assert clean_reference("   ") is None

    def test_jira_key_and_url_are_kept(self):
        assert clean_reference(" ABC-123 ") == "ABC-123"
        assert clean_reference("https://x.atlassian.net/browse/ABC-1")

    @pytest.mark.parametrize("bad", ["a*/b", "x\ny", "<b>", "-lead", "a" * 81, "a]b"])
    def test_anything_that_could_break_a_comment_or_markup_is_rejected(self, bad):
        with pytest.raises(ValueError):
            clean_reference(bad)


class TestTagValue:
    def test_no_reference_returns_the_same_object(self):
        flow = Flow(**FLOW_IR)
        assert tag_value(flow, None) is flow

    def test_flow_and_every_element_get_the_tag_without_touching_the_original(self):
        flow = Flow(**FLOW_IR)
        tagged = tag_value(flow, "ABC-1")
        assert tagged.description == "[ABC-1]"
        assert all(e.description.startswith("[ABC-1]") for e in tagged.elements)
        assert flow.description is None and flow.elements[0].description is None

    def test_existing_description_is_kept_after_the_tag(self):
        assert tag_description("Does a thing", "ABC-1") == "[ABC-1] Does a thing"

    def test_tagging_twice_does_not_stack(self):
        once = tag_value(Flow(**FLOW_IR), "ABC-1")
        assert tag_value(once, "ABC-1").description == "[ABC-1]"

    def test_field_description_and_source_comments(self):
        field = CustomField(**VALID_FIELD)
        assert tag_value(field, "ABC-1").description.startswith("[ABC-1]")

        apex = ApexClass(**VALID_APEX)
        assert tag_value(apex, "ABC-1").body.startswith("// Ref: ABC-1\n")
        assert tag_value(tag_value(apex, "ABC-1"), "ABC-1").body.count("Ref: ABC-1") == 1

        lwc = LightningComponent(**VALID_LWC)
        assert tag_value(lwc, "ABC-1").js.startswith("// Ref: ABC-1\n")


class TestPlanReferenceAndPackage:
    def _session(self, client, typed_scripted):
        typed_scripted(plan=BUNDLE_PLAN, object=VALID_OBJECT, field=VALID_FIELD, apex=VALID_APEX)
        return execute(client, make_plan(client)["plan_id"])

    def test_package_is_a_deployable_zip(self, client, typed_scripted):
        sid = self._session(client, typed_scripted)["session_id"]
        response = client.get(f"/api/plan/session/{sid}/package")
        assert response.status_code == 200
        assert response.headers["content-type"] == "application/zip"
        assert "attachment" in response.headers["content-disposition"]
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            names = archive.namelist()
            assert "package.xml" in names
            assert any(n.startswith("objects/") for n in names)
            assert any(n.startswith("classes/") and n.endswith(".cls") for n in names)
            assert "<name>ApexClass</name>" in archive.read("package.xml").decode()

    def test_reference_lands_in_the_package_and_can_be_cleared(self, client, typed_scripted):
        sid = self._session(client, typed_scripted)["session_id"]
        set_ref = client.post(
            "/api/reference", json={"kind": "plan", "session_id": sid, "reference": "ABC-7"},
        )
        assert set_ref.json() == {"reference": "ABC-7"}
        assert client.get(f"/api/plan/session/{sid}").json()["reference"] == "ABC-7"

        def package_text():
            with zipfile.ZipFile(io.BytesIO(client.get(f"/api/plan/session/{sid}/package").content)) as a:
                return "\n".join(a.read(n).decode() for n in a.namelist())

        assert "// Ref: ABC-7" in package_text()
        assert "[ABC-7]" in package_text()

        client.post("/api/reference", json={"kind": "plan", "session_id": sid, "reference": ""})
        assert "ABC-7" not in package_text()

    def test_invalid_reference_is_rejected(self, client, typed_scripted):
        sid = self._session(client, typed_scripted)["session_id"]
        response = client.post(
            "/api/reference", json={"kind": "plan", "session_id": sid, "reference": "a*/b"},
        )
        assert response.status_code == 400

    def test_reference_does_not_need_reapproval(self, client, typed_scripted):
        session = self._session(client, typed_scripted)
        sid = session["session_id"]
        client.post("/api/plan/approve", json={"session_id": sid, "version": session["version"]})
        client.post("/api/reference", json={"kind": "plan", "session_id": sid, "reference": "ABC-7"})
        assert client.get(f"/api/plan/session/{sid}").json()["approved"] is True

    def test_deploy_carries_the_reference(self, client, typed_scripted, monkeypatch):
        from flowtool.sfdc import DeployResult

        session = self._session(client, typed_scripted)
        sid = session["session_id"]
        seen = {}

        async def fake_validate_bundle(url, token, files, types, api_version="62.0", check_only=True):
            seen.update(files)
            return DeployResult("1", "Succeeded", True)

        monkeypatch.setattr(server, "validate_bundle", fake_validate_bundle)
        client.post("/api/reference", json={"kind": "plan", "session_id": sid, "reference": "ABC-9"})
        client.post("/api/plan/approve", json={"session_id": sid, "version": session["version"]})
        client.post("/api/plan/deploy/start", json={"session_id": sid, "confirm": True})
        assert any("// Ref: ABC-9" in v for k, v in seen.items() if k.endswith(".cls"))


class TestSingleSessionPackage:
    def test_flow_session_package_and_reference(self, client, scripted):
        scripted(VALID_FLOW)
        started = client.post("/api/design/start", json={"request": "a flow"})
        assert started.status_code == 200, started.text
        job_id = started.json()["job_id"]
        for _ in range(50):
            status = client.get("/api/design/status", params={"job_id": job_id}).json()
            if status.get("done"):
                break
        sid = status["session_id"]

        client.post("/api/reference", json={"kind": "session", "session_id": sid, "reference": "ABC-3"})
        response = client.get(f"/api/session/{sid}/package")
        assert response.status_code == 200
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            flow_files = [n for n in archive.namelist() if n.startswith("flows/")]
            assert flow_files
            assert "[ABC-3]" in archive.read(flow_files[0]).decode()
