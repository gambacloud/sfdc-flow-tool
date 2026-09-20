"""
"Push to Git" - commit a set of generated files to a new branch of a GitHub
repo and open a pull request for them.

Talks to GitHub's REST API with plain httpx, same as the rest of this app. The
Git Data API (tree -> commit -> ref) is used rather than the Contents API so
that a plan of many files lands as ONE commit instead of one per file.

The caller's personal access token is used for the requests in this module and
nothing else: it is never stored, logged, or echoed back in an error.
"""

from __future__ import annotations

import re
from typing import Dict, Optional

import httpx

API = "https://api.github.com"
_TIMEOUT = 30.0

_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_UNSAFE_REF_CHARS = re.compile(r"[^A-Za-z0-9._/-]+")


class GitHubError(Exception):
    """A user-facing GitHub problem. Always safe to show as-is."""


def clean_repo(value: str) -> str:
    """Accept `owner/name` or a full github.com URL; return `owner/name`."""
    repo = (value or "").strip()
    repo = re.sub(r"^https?://github\.com/", "", repo).removesuffix(".git").strip("/")
    if not _REPO_RE.match(repo):
        raise GitHubError("Repository must look like owner/name.")
    return repo


def clean_folder(value: str) -> str:
    """A repo-relative folder with no traversal; empty means the repo root."""
    parts = [p for p in (value or "").replace("\\", "/").split("/") if p]
    if any(p in (".", "..") for p in parts):
        raise GitHubError("Folder can't contain . or .. segments.")
    return "/".join(parts)


def clean_branch(value: str) -> str:
    name = _UNSAFE_REF_CHARS.sub("-", (value or "").strip()).strip("-/.")
    name = re.sub(r"/{2,}|\.{2,}", "-", name)
    if not name:
        raise GitHubError("Branch name is empty.")
    return name


def _explain(response: httpx.Response, what: str) -> GitHubError:
    try:
        detail = response.json().get("message", "")
    except ValueError:
        detail = ""
    if response.status_code == 401:
        return GitHubError("GitHub rejected the token (401). Check it is valid and not expired.")
    if response.status_code in (403, 404):
        return GitHubError(
            f"GitHub said {response.status_code} while {what}. Check the repo name and that the "
            "token can write to it (classic: `repo` scope; fine-grained: Contents + Pull requests "
            f"read/write). {detail}".strip()
        )
    return GitHubError(f"GitHub error {response.status_code} while {what}: {detail}".strip())


async def open_pull_request(
    *,
    token: str,
    repo: str,
    base: Optional[str],
    branch: str,
    files: Dict[str, str],
    folder: str,
    commit_message: str,
    title: str,
    body: str,
) -> Dict[str, str]:
    """
    Create `branch` off `base` (default: the repo's default branch), commit
    `files` under `folder` in a single commit, and open a PR back to `base`.
    If `branch` is taken a numeric suffix is added rather than overwriting it.
    Returns {"url", "number", "branch", "base"}.
    """
    if not token.strip():
        raise GitHubError("A GitHub token is required.")
    headers = {
        "Authorization": f"Bearer {token.strip()}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    prefix = f"{folder}/" if folder else ""

    root = f"{API}/repos/{repo}"

    async with httpx.AsyncClient(headers=headers, timeout=_TIMEOUT) as http:
        info = await http.get(root)
        if info.status_code != 200:
            raise _explain(info, "opening the repository")
        base = (base or "").strip() or info.json()["default_branch"]

        ref = await http.get(f"{root}/git/ref/heads/{base}")
        if ref.status_code != 200:
            raise _explain(ref, f"finding base branch '{base}'")
        base_sha = ref.json()["object"]["sha"]

        base_commit = await http.get(f"{root}/git/commits/{base_sha}")
        if base_commit.status_code != 200:
            raise _explain(base_commit, "reading the base commit")
        base_tree = base_commit.json()["tree"]["sha"]

        tree = await http.post(f"{root}/git/trees", json={
            "base_tree": base_tree,
            "tree": [
                {"path": prefix + path, "mode": "100644", "type": "blob", "content": content}
                for path, content in files.items()
            ],
        })
        if tree.status_code != 201:
            raise _explain(tree, "writing the files")

        commit = await http.post(f"{root}/git/commits", json={
            "message": commit_message, "tree": tree.json()["sha"], "parents": [base_sha],
        })
        if commit.status_code != 201:
            raise _explain(commit, "creating the commit")
        commit_sha = commit.json()["sha"]

        final = branch
        for attempt in range(1, 6):
            created = await http.post(f"{root}/git/refs", json={
                "ref": f"refs/heads/{final}", "sha": commit_sha,
            })
            if created.status_code == 201:
                break
            if created.status_code == 422:  # branch name already taken
                final = f"{branch}-{attempt + 1}"
                continue
            raise _explain(created, "creating the branch")
        else:
            raise GitHubError(f"Couldn't find a free branch name starting with '{branch}'.")

        pr = await http.post(f"{root}/pulls", json={
            "title": title, "head": final, "base": base, "body": body,
        })
        if pr.status_code != 201:
            raise _explain(pr, "opening the pull request")
        data = pr.json()
        return {
            "url": data["html_url"], "number": str(data["number"]),
            "branch": final, "base": base,
        }
