"""
"Save session" - a Session/PlanSession written to a .sfdcbuildertool.json
file and restored later as a brand-new session. No server-side storage is
involved, so unlike test_share.py nothing needs faking.
"""

from datetime import datetime, timezone

import pytest

from flowtool import savefile
from tests.test_ir_apex_generator import VALID as VALID_APEX
from tests.test_ir_object_generator import VALID_FIELD, VALID_OBJECT
from tests.test_server_plan import BUNDLE_PLAN, make_plan, execute
from tests.test_share import client, design, scripted, typed_scripted  # noqa: F401
from tests.test_llm import VALID as VALID_FLOW
import server


class TestFilename:
    def test_reference_name_version_and_stamp(self):
        name = savefile.filename(
            reference="PROJ-123", name="Lead scoring", kind="plan", title="Invoice object",
            version=3, now=datetime(2026, 10, 25, 14, 30, tzinfo=timezone.utc),
        )
        assert name == "PROJ-123__plan-Lead-scoring__v3__2026-10-25_1430.sfdcbuildertool.json"

    def test_title_is_used_when_there_is_no_name_and_blanks_are_skipped(self):
        name = savefile.filename(
            reference=None, name=None, kind="session", title="Lead_Score",
            version=None, now=datetime(2026, 1, 2, 3, 4, tzinfo=timezone.utc),
        )
        assert name == "session-Lead_Score__2026-01-02_0304.sfdcbuildertool.json"

    def test_nothing_unsafe_survives_into_the_file_name(self):
        name = savefile.filename(
            reference="../../etc/passwd", name=r'a<b>:"c"|?*\d', kind="plan", title=None, version=1,
        )
        assert not any(ch in name for ch in r'/\<>:"|?* ')
        assert ".." not in name
        assert name.endswith(".sfdcbuildertool.json")


class TestUnwrap:
    def test_rejects_a_file_that_is_not_ours(self):
        with pytest.raises(savefile.SaveFileError):
            savefile.unwrap({"hello": "world"})

    def test_rejects_a_newer_schema(self):
        with pytest.raises(savefile.SaveFileError, match="newer"):
            savefile.unwrap({"format": "sfdcbuildertool", "schema_version": 99, "snapshot": {}})

    def test_rejects_a_missing_snapshot(self):
        with pytest.raises(savefile.SaveFileError, match="damaged"):
            savefile.unwrap({"format": "sfdcbuildertool", "schema_version": 1})


class TestSaveRestoreSession:
    def test_round_trip(self, client, scripted):  # noqa: F811
        scripted(VALID_FLOW)
        original = design(client)
        client.post("/api/reference", json={
            "kind": "session", "session_id": original["session_id"], "reference": "PROJ-9",
        })
        saved = client.post(
            "/api/save",
            json={"kind": "session", "session_id": original["session_id"], "name": "my work"},
        )
        assert saved.status_code == 200, saved.text
        body = saved.json()
        assert body["filename"].startswith("PROJ-9__session-my-work__v1__")
        assert body["filename"].endswith(".sfdcbuildertool.json")

        restored = client.post("/api/restore", json={"file": body["file"]})
        assert restored.status_code == 200, restored.text
        data = restored.json()
        assert data["restored_kind"] == "session"
        assert data["approved"] is False
        assert data["api_name"] == original["api_name"]
        assert data["reference"] == "PROJ-9"
        assert data["session_id"] != original["session_id"]
        assert data["session_id"] in server.SESSIONS

    def test_a_file_never_carries_secrets(self, client, scripted):  # noqa: F811
        scripted(VALID_FLOW)
        original = design(client)
        body = client.post(
            "/api/save", json={"kind": "session", "session_id": original["session_id"]}
        ).json()
        text = str(body["file"]).lower()
        assert "api_key" not in text and "access_token" not in text

    def test_unknown_session_is_404(self, client):  # noqa: F811
        resp = client.post("/api/save", json={"kind": "session", "session_id": "nope"})
        assert resp.status_code == 404


class TestSaveRestorePlan:
    def test_round_trip_restores_the_whole_bundle(self, client, typed_scripted):  # noqa: F811
        typed_scripted(plan=BUNDLE_PLAN, object=VALID_OBJECT, field=VALID_FIELD, apex=VALID_APEX)
        plan = make_plan(client)
        original = execute(client, plan["plan_id"])

        saved = client.post(
            "/api/save", json={"kind": "plan", "session_id": original["session_id"]}
        )
        assert saved.status_code == 200, saved.text
        assert saved.json()["filename"].startswith("plan-")

        restored = client.post("/api/restore", json={"file": saved.json()["file"]})
        assert restored.status_code == 200, restored.text
        data = restored.json()
        assert data["restored_kind"] == "plan"
        assert data["approved"] is False
        assert data["session_id"] in server.PLAN_SESSIONS
        assert {s["name"] for s in data["steps"]} == {s["name"] for s in original["steps"]}


class TestRestoreRejects:
    def test_not_our_file(self, client):  # noqa: F811
        resp = client.post("/api/restore", json={"file": {"some": "json"}})
        assert resp.status_code == 400
        assert "not a SFDCBUILDERTOOL" in resp.text

    def test_damaged_snapshot(self, client):  # noqa: F811
        file = {
            "format": "sfdcbuildertool", "schema_version": 1,
            "snapshot": {"type": "session", "kind": "flow"},
        }
        resp = client.post("/api/restore", json={"file": file})
        assert resp.status_code == 400
        assert "damaged" in resp.text

    def test_a_hand_edited_reference_is_validated(self, client, scripted):  # noqa: F811
        scripted(VALID_FLOW)
        original = design(client)
        file = client.post(
            "/api/save", json={"kind": "session", "session_id": original["session_id"]}
        ).json()["file"]
        file["snapshot"]["reference"] = "x*/<evil>"
        assert client.post("/api/restore", json={"file": file}).status_code == 400
