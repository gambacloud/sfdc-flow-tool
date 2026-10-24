"""
Hosted ("no-org") mode, for the backend that serves the Chrome extension.

With FLOWTOOL_HOSTED=1 the server never touches a Salesforce org: the extension
does every org call with the browser's own session, and this server only does
LLM / IR / XML work. It therefore refuses any request that carries an org URL,
a token or an sf CLI alias, and every endpoint that exists only to reach an org.
Unset (the default) none of this applies and the server behaves as before.

Read from the environment on every call, not at import, so a test or a restart
with a different value needs no reload.
"""

from __future__ import annotations

import os
from typing import Any, List

from fastapi import HTTPException, Request

HOSTED_ENV = "FLOWTOOL_HOSTED"
CORS_ENV = "FLOWTOOL_CORS_ORIGINS"

# Body fields that would hand this server a way into an org.
ORG_FIELDS = ("instance_url", "access_token", "org")

HEADERS_ALLOWED = ["Content-Type", "X-Client-Id", "Authorization"]

# Endpoints that exist only to reach an org (the extension does these itself).
# Matched as an exact path or a path prefix followed by "/".
ORG_PATHS = (
    "/api/flows", "/api/apex-classes", "/api/apex-triggers", "/api/lwc-components",
    "/api/permission-sets", "/api/prompt-templates", "/api/custom-objects", "/api/custom-fields",
    "/api/survey/start", "/api/survey/status",
    "/api/org-summary",
    "/api/import/start", "/api/import/status", "/api/import/object-fields",
    "/api/validate", "/api/deploy",
    "/api/plan/validate", "/api/plan/deploy", "/api/plan/agent/publish", "/api/plan/scenarios/run",
    "/api/portal/preflight", "/api/portal/plan", "/api/portal/apply", "/api/portal/status",
)

MESSAGE = (
    "This server is hosted without org access: Salesforce calls run in the browser extension. "
    "Do not send instance_url, access_token or org."
)


def is_hosted() -> bool:
    return os.environ.get(HOSTED_ENV, "").strip().lower() in ("1", "true", "yes", "on")


def cors_origins() -> List[str]:
    """Exact origins allowed to call the API, e.g. chrome-extension://<id>."""
    raw = os.environ.get(CORS_ENV, "")
    return [o.strip().rstrip("/") for o in raw.split(",") if o.strip()]


def is_org_path(path: str) -> bool:
    return any(path == p or path.startswith(p + "/") for p in ORG_PATHS)


def carries_org_fields(data: Any) -> bool:
    """True when a JSON body (top level) sets any ORG_FIELDS to a non-empty value."""
    return isinstance(data, dict) and any(data.get(k) for k in ORG_FIELDS)


async def guard(request: Request) -> None:
    """App-wide dependency: in hosted mode, 4xx anything org-shaped. No-op otherwise."""
    if not is_hosted():
        return
    if is_org_path(request.url.path):
        raise HTTPException(403, MESSAGE)
    if request.method in ("POST", "PUT", "PATCH"):
        try:
            data = await request.json()
        except Exception:  # noqa: BLE001 - not JSON: nothing to inspect, the route validates it
            return
        if carries_org_fields(data):
            raise HTTPException(400, MESSAGE)
