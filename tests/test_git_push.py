"""
Push to Git - flowtool/github.py against a fake GitHub, plus the
/api/plan/git/push endpoint.
"""

import asyncio
import json

import httpx
import pytest

from flowtool import github
from tests.test_ir_apex_generator import VALID as VALID_APEX
from tests.test_ir_object_generator import VALID_FIELD, VALID_OBJECT
from tests.test_server_plan import (  # noqa: F401 - fixtures
    BUNDLE_PLAN, client, execute, make_plan, scripted, typed_scripted,
)


def fake_github(monkeypatch, *, taken=(), status=None):
    """Route flowtool.github's httpx client to an in-memory GitHub. Returns
    the list of (method, path, json) requests it saw."""
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/repos/o/r")
        payload = json.loads(request.content) if request.content else None
        seen.append((request.method, path, payload))
        if status and path in status:
            return httpx.Response(status[path], json={"message": "nope"})
        if path == "":
            return httpx.Response(200, json={"default_branch": "main"})
        if path == "/git/ref/heads/main":
            return httpx.Response(200, json={"object": {"sha": "base-sha"}})
        if path == "/git/commits/base-sha":
            return httpx.Response(200, json={"tree": {"sha": "base-tree"}})
        if path == "/git/trees":
            return httpx.Response(201, json={"sha": "new-tree"})
        if path == "/git/commits":
            return httpx.Response(201, json={"sha": "new-commit"})
        if path == "/git/refs":
            if payload["ref"].removeprefix("refs/heads/") in taken:
                return httpx.Response(422, json={"message": "Reference already exists"})
            return httpx.Response(201, json={})
        if path == "/pulls":
            return httpx.Response(201, json={"html_url": "https://github.com/o/r/pull/9", "number": 9})
        return httpx.Response(500, json={"message": f"unexpected {path}"})

    real = httpx.AsyncClient
    monkeypatch.setattr(
        github.httpx, "AsyncClient",
        lambda **kw: real(transport=httpx.MockTransport(handler), **kw),
    )
    return seen


def push(**overrides):
    args = dict(
        token="tok", repo="o/r", base=None, branch="flow-forge/x",
        files={"package.xml": "<Package/>", "classes/A.cls": "class A {}"},
        folder="sfdc-flow-forge/x", commit_message="msg", title="title", body="body",
    )
    args.update(overrides)
    return asyncio.run(github.open_pull_request(**args))


class TestCleaners:
    def test_repo_accepts_url_and_name(self):
        assert github.clean_repo("https://github.com/o/r.git") == "o/r"
        assert github.clean_repo(" o/r ") == "o/r"

    @pytest.mark.parametrize("bad", ["", "justname", "a/b/c", "a b/c"])
    def test_bad_repo(self, bad):
        with pytest.raises(github.GitHubError):
            github.clean_repo(bad)

    def test_folder_blocks_traversal_and_normalises(self):
        assert github.clean_folder(r"/a\b//c/") == "a/b/c"
        assert github.clean_folder("") == ""
        with pytest.raises(github.GitHubError):
            github.clean_folder("a/../b")

    def test_branch_is_made_ref_safe(self):
        assert github.clean_branch("flow forge/ABC-1:x") == "flow-forge/ABC-1-x"


class TestOpenPullRequest:
    def test_one_commit_with_every_file_under_the_folder_then_a_pr(self, monkeypatch):
        seen = fake_github(monkeypatch)
        result = push()
        assert result == {
            "url": "https://github.com/o/r/pull/9", "number": "9",
            "branch": "flow-forge/x", "base": "main",
        }
        tree = next(p for m, path, p in seen if path == "/git/trees")
        assert tree["base_tree"] == "base-tree"
        assert {e["path"] for e in tree["tree"]} == {
            "sfdc-flow-forge/x/package.xml", "sfdc-flow-forge/x/classes/A.cls",
        }
        pr = next(p for m, path, p in seen if path == "/pulls")
        assert pr["head"] == "flow-forge/x" and pr["base"] == "main"

    def test_empty_folder_puts_files_at_the_root(self, monkeypatch):
        seen = fake_github(monkeypatch)
        push(folder="")
        tree = next(p for m, path, p in seen if path == "/git/trees")
        assert {e["path"] for e in tree["tree"]} == {"package.xml", "classes/A.cls"}

    def test_taken_branch_gets_a_suffix_instead_of_being_overwritten(self, monkeypatch):
        fake_github(monkeypatch, taken={"flow-forge/x"})
        assert push()["branch"] == "flow-forge/x-2"

    def test_bad_token_message_never_contains_the_token(self, monkeypatch):
        fake_github(monkeypatch, status={"": 401})
        with pytest.raises(github.GitHubError) as err:
            push(token="super-secret")
        assert "401" in str(err.value) and "super-secret" not in str(err.value)

    def test_missing_base_branch_is_explained(self, monkeypatch):
        fake_github(monkeypatch, status={"/git/ref/heads/nope": 404})
        with pytest.raises(github.GitHubError, match="nope"):
            push(base="nope")


class TestEndpoint:
    def _sid(self, client, typed_scripted):
        typed_scripted(plan=BUNDLE_PLAN, object=VALID_OBJECT, field=VALID_FIELD, apex=VALID_APEX)
        return execute(client, make_plan(client)["plan_id"])["session_id"]

    def test_pushes_the_package_with_package_xml_and_a_useful_pr_body(
        self, client, typed_scripted, monkeypatch,
    ):
        sid = self._sid(client, typed_scripted)
        client.post("/api/reference", json={"kind": "plan", "session_id": sid, "reference": "ABC-7"})
        seen = fake_github(monkeypatch)
        response = client.post("/api/plan/git/push", json={
            "session_id": sid, "token": "tok", "repo": "o/r", "folder": "pkg",
        })
        assert response.status_code == 200, response.text
        assert response.json()["url"].endswith("/pull/9")

        tree = next(p for m, path, p in seen if path == "/git/trees")
        paths = {e["path"] for e in tree["tree"]}
        assert "pkg/package.xml" in paths
        assert any(p.startswith("pkg/classes/") and p.endswith(".cls") for p in paths)
        pr = next(p for m, path, p in seen if path == "/pulls")
        assert pr["title"].startswith("ABC-7:")
        assert "sf project deploy start --metadata-dir pkg" in pr["body"]
        assert pr["head"].startswith("flow-forge/ABC-7-")

    def test_needs_no_approval_because_it_deploys_nothing(self, client, typed_scripted, monkeypatch):
        sid = self._sid(client, typed_scripted)
        fake_github(monkeypatch)
        assert client.get(f"/api/plan/session/{sid}").json()["approved"] is False
        assert client.post("/api/plan/git/push", json={
            "session_id": sid, "token": "t", "repo": "o/r",
        }).status_code == 200

    def test_bad_repo_is_a_400_not_a_crash(self, client, typed_scripted):
        sid = self._sid(client, typed_scripted)
        response = client.post("/api/plan/git/push", json={
            "session_id": sid, "token": "t", "repo": "nonsense",
        })
        assert response.status_code == 400 and "owner/name" in response.json()["detail"]
