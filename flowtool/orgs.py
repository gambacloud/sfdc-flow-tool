"""
Read org credentials from the sf CLI so nobody has to copy an access token by
hand. Optional — everything still works with a session id typed at the prompt.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass
from typing import List, Optional


class SfCliError(RuntimeError):
    pass


@dataclass
class OrgCredentials:
    alias: str
    instance_url: str
    access_token: str
    username: str


def _sf_executable() -> str:
    exe = shutil.which("sf")
    if not exe:
        # Reached from the CLI and from the web UI, so the advice has to hold
        # in both - there is no prompt to fall back to in a browser.
        raise SfCliError(
            "The sf CLI is not on PATH. Install it with:\n"
            "    npm install -g @salesforce/cli\n"
            "If it is already installed, start SFDC Flow Tool from a shell where "
            "`sf` resolves - PATH differs between PowerShell and Git Bash."
        )
    return exe


def _run_sf_raw(args: List[str], timeout: int = 120) -> subprocess.CompletedProcess:
    exe = _sf_executable()
    try:
        return subprocess.run(
            [exe, *args], capture_output=True, text=True, timeout=timeout
        )
    except subprocess.TimeoutExpired as exc:
        raise SfCliError(f"sf {' '.join(args)} timed out") from exc


def _run_sf(args: List[str], timeout: int = 120) -> dict:
    proc = _run_sf_raw([*args, "--json"], timeout)

    # The CLI reports failures in JSON on stdout, so parse before checking the code.
    try:
        payload = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError:
        raise SfCliError(
            f"sf {' '.join(args)} produced unreadable output:\n"
            f"{(proc.stderr or proc.stdout)[:600]}"
        ) from None

    if proc.returncode != 0:
        message = payload.get("message") or proc.stderr or "unknown error"
        raise SfCliError(f"sf {' '.join(args)} failed: {message}")

    return payload.get("result", {})


def _looks_like_token(value: str) -> bool:
    """
    Cheap sanity check so a placeholder never gets sent as a credential.
    Salesforce session ids are long, unbroken, and start with the org id.
    """
    return (
        len(value) >= 80
        and " " not in value
        and not value.startswith("[")
        and "REDACT" not in value.upper()
    )


def _access_token(alias: Optional[str]) -> str:
    """
    Get the real access token. The CLI keeps moving this: it used to be in
    `sf org display`, then only under --verbose, and now lives behind
    `sf org auth show-access-token`. Try the current command first and fall
    back, so this keeps working across CLI versions in either direction.
    """
    target = ["--target-org", alias] if alias else []
    attempts: List[List[str]] = [
        ["org", "auth", "show-access-token", *target],
        ["org", "display", "--verbose", *target],
    ]

    tried: List[str] = []
    for args in attempts:
        try:
            payload = _run_sf(args)
        except SfCliError as exc:
            tried.append(f"sf {' '.join(args)}: {exc}")
            # Older commands may not speak --json; take stdout verbatim.
            proc = _run_sf_raw(args)
            candidate = (proc.stdout or "").strip()
            if _looks_like_token(candidate):
                return candidate
            continue

        candidate = payload if isinstance(payload, str) else payload.get("accessToken", "")
        candidate = (candidate or "").strip()
        if _looks_like_token(candidate):
            return candidate
        tried.append(f"sf {' '.join(args)}: returned {candidate[:40]!r}")

    raise SfCliError(
        "Could not get an access token from the sf CLI. Tried:\n  "
        + "\n  ".join(tried)
        + "\n\nRun this yourself to see what your CLI returns:\n"
        f"    sf org auth show-access-token{' --target-org ' + alias if alias else ''}\n"
        "Or skip the CLI entirely and paste the token at the prompt "
        "(run without --org)."
    )


def _force_token_refresh(alias: Optional[str]) -> None:
    """
    `sf org display` reads the stored token without contacting Salesforce, so it
    happily hands back one that has already expired. Making a real (cheap) API
    call first pushes the CLI through its refresh-token flow. Failures here are
    not fatal — the caller will surface a clearer error if the token is bad.
    """
    args = ["limits", "api", "display"]
    if alias:
        args += ["--target-org", alias]
    try:
        _run_sf(args)
    except SfCliError:
        pass


def get_org(alias: Optional[str] = None) -> OrgCredentials:
    """
    Pull instance url + access token for an org the CLI already knows about.
    Pass no alias to use whatever is set as the default target org.
    """
    _force_token_refresh(alias)

    # The instance url is never redacted, so plain `org display` is fine for it.
    args = ["org", "display"]
    if alias:
        args += ["--target-org", alias]
    result = _run_sf(args)

    instance_url = result.get("instanceUrl")
    if not instance_url:
        raise SfCliError(
            f"sf org display returned no instance url for {alias or 'the default org'}. "
            "Log in first:\n"
            "    sf org login web --alias dev"
        )

    access_token = _access_token(alias)

    return OrgCredentials(
        alias=alias or result.get("alias") or "default",
        instance_url=instance_url,
        access_token=access_token,
        username=result.get("username", "?"),
    )


def list_orgs() -> List[str]:
    """Aliases (or usernames) of every org the CLI is authenticated against."""
    result = _run_sf(["org", "list"])
    names: List[str] = []
    for bucket in ("nonScratchOrgs", "scratchOrgs", "devHubs", "sandboxes", "other"):
        for org in result.get(bucket, []) or []:
            name = org.get("alias") or org.get("username")
            if name and name not in names:
                names.append(name)
    return names


# --------------------------------------------------------------------------
# Scratch orgs
# --------------------------------------------------------------------------

SCRATCH_EDITIONS = (
    "developer", "enterprise", "group", "professional",
    "partner-developer", "partner-enterprise", "partner-group", "partner-professional",
)
MAX_SCRATCH_DAYS = 30
# Leading letter or digit so an alias can never be read by the CLI as a flag.
_ALIAS_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,39}$")
# Creation is queued, then provisioned; the CLI's own --wait is minutes.
SCRATCH_WAIT_MINUTES = 15


def scratch_overview() -> dict:
    """Dev Hubs this CLI can create from, and the scratch orgs it already knows."""
    result = _run_sf(["org", "list"])
    hubs: List[str] = []
    for org in result.get("devHubs", []) or []:
        name = org.get("alias") or org.get("username")
        if name and name not in hubs:
            hubs.append(name)
    scratch = [
        {
            "name": org.get("alias") or org.get("username"),
            "expires": org.get("expirationDate") or "",
            "expired": bool(org.get("isExpired")),
        }
        for org in result.get("scratchOrgs", []) or []
        if org.get("alias") or org.get("username")
    ]
    return {"dev_hubs": hubs, "scratch_orgs": scratch, "editions": list(SCRATCH_EDITIONS), "max_days": MAX_SCRATCH_DAYS}


def validate_scratch_request(alias: str, duration_days: int, edition: str) -> None:
    if not _ALIAS_RE.match(alias or ""):
        raise SfCliError(
            "The scratch org name must be 1-40 characters: letters, digits, '-' and '_', "
            "starting with a letter or digit."
        )
    if not 1 <= duration_days <= MAX_SCRATCH_DAYS:
        raise SfCliError(f"A scratch org lives 1-{MAX_SCRATCH_DAYS} days.")
    if edition not in SCRATCH_EDITIONS:
        raise SfCliError(f"Unknown edition {edition!r}.")


def create_scratch_org(alias: str, duration_days: int, edition: str, dev_hub: Optional[str]) -> dict:
    """
    Create a scratch org and return {alias, username, instance_url, expires}.
    Blocks until the org is ready (or the CLI gives up), so call it off the
    event loop. The org is not made the default - the caller selects it.
    """
    validate_scratch_request(alias, duration_days, edition)
    args = [
        "org", "create", "scratch",
        "--alias", alias,
        "--edition", edition,
        "--duration-days", str(duration_days),
        "--wait", str(SCRATCH_WAIT_MINUTES),
        "--no-track-source",
    ]
    if dev_hub:
        args += ["--target-dev-hub", dev_hub]
    else:
        hubs = scratch_overview()["dev_hubs"]
        if not hubs:
            raise SfCliError(
                "No Dev Hub is connected. Enable Dev Hub in a Developer/Production org, then:\n"
                "    sf org login web --set-default-dev-hub --alias devhub"
            )
        args += ["--target-dev-hub", hubs[0]]

    result = _run_sf(args, timeout=(SCRATCH_WAIT_MINUTES + 2) * 60)
    return {
        "alias": alias,
        "username": result.get("username", ""),
        "instance_url": result.get("instanceUrl", ""),
        "expires": result.get("expirationDate", ""),
    }


def delete_scratch_org(alias: str) -> None:
    """Delete one scratch org - refuses anything `sf org list` does not call a scratch org."""
    known = {o["name"] for o in scratch_overview()["scratch_orgs"]}
    if alias not in known:
        raise SfCliError(f"{alias!r} is not a scratch org this CLI knows - nothing was deleted.")
    _run_sf(["org", "delete", "scratch", "--target-org", alias, "--no-prompt"])
