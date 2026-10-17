"""
HTTP surface for the portal builder (the "New Agentforce" tab).

    POST /api/portal/preflight   read-only checklist
    POST /api/portal/plan        the checklist plus the steps a build would run
    POST /api/portal/apply       start the build in the background (needs confirm)
    GET  /api/portal/status      steps so far, then the result

Nothing here writes to an org except `apply`, and `apply` refuses without an
explicit `confirm` - and, on an org that looks like production, without
`confirm_production` as well.
"""

from __future__ import annotations

import asyncio
import re
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Dict, List, Literal, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, field_validator

from . import portal_miaw as miaw
from . import portal_pages as pages
from .portal_preflight import OrgClient, run_preflight
from .portal_site import OrgWriter, SiteResult, SiteSpec, Step, build_site
from .sfdc import MetadataClient

_SITE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _-]{0,58}$")
_PREFIX_RE = re.compile(r"^[a-z0-9]{1,30}$")
_USERNAME_RE = re.compile(r"^[^\s@']+@[^\s@']+$")


class PortalRequest(BaseModel):
    site_name: str
    url_prefix: Optional[str] = None
    headline: Optional[str] = Field(default=None, max_length=120)
    intro: Optional[str] = Field(default=None, max_length=300)
    license: Literal["customer_community", "customer_community_plus"] = "customer_community"
    chat: bool = False
    reuse: bool = False
    admin_username: Optional[str] = None
    api_version: str = "64.0"

    org: Optional[str] = None
    instance_url: Optional[str] = None
    access_token: Optional[str] = None

    confirm: bool = False
    confirm_production: bool = False

    @field_validator("site_name")
    @classmethod
    def valid_site_name(cls, v: str) -> str:
        v = v.strip()
        if not _SITE_NAME_RE.match(v):
            raise ValueError("Site name: letters, digits, spaces, '-' and '_' only, starting with a letter or digit.")
        return v

    @field_validator("url_prefix")
    @classmethod
    def valid_prefix(cls, v: Optional[str]) -> Optional[str]:
        v = (v or "").strip().lower() or None
        if v and not _PREFIX_RE.match(v):
            raise ValueError("URL prefix: lowercase letters and digits only.")
        return v

    @field_validator("admin_username")
    @classmethod
    def valid_username(cls, v: Optional[str]) -> Optional[str]:
        v = (v or "").strip() or None
        if v and not _USERNAME_RE.match(v):
            raise ValueError("Admin username must look like name@domain.")
        return v

    @field_validator("api_version")
    @classmethod
    def valid_api_version(cls, v: str) -> str:
        if not re.match(r"^\d{2}\.0$", v):
            raise ValueError("API version looks like 64.0.")
        return v


def spec_from(body: PortalRequest) -> SiteSpec:
    content = None
    if body.headline:
        content = pages.PortalContent(
            site_name=body.site_name,
            headline=body.headline,
            intro=body.intro or pages.PortalContent.__dataclass_fields__["intro"].default,
        )
    chat = None
    if body.chat:
        # Names carry the site: an Embedded Service Deployment is bound to one
        # site for good and cannot be re-pointed, so two sites must not share one.
        chat = miaw.MiawSpec(channel_label=f"{body.site_name} Chat", queue_label=f"{body.site_name} Support")
    return SiteSpec(
        site_name=body.site_name,
        url_path_prefix=body.url_prefix,
        license=body.license,
        admin_username=body.admin_username,
        want_miaw=body.chat,
        allow_existing_site=body.reuse,
        content=content,
        messaging=chat,
        api_version=body.api_version,
    )


def result_to_dict(result: SiteResult) -> Dict[str, Any]:
    return {
        "ok": result.ok,
        "preflight": result.preflight.to_dict(),
        "steps": [asdict(s) for s in result.steps],
        "validation": [asdict(c) for c in result.validation],
        "site_url": result.site_url,
        "register_url": result.register_url,
        "manual_steps": result.manual_steps,
    }


@dataclass
class PortalJob:
    task: "asyncio.Task"
    steps: List[Step] = field(default_factory=list)


def make_router(credentials: Callable[..., tuple]) -> APIRouter:
    """`credentials` is server.credentials: (org alias, instance_url, token) -> (url, token)."""
    router = APIRouter()
    jobs: Dict[str, PortalJob] = {}

    def creds(body: PortalRequest) -> tuple:
        return credentials(body.org, body.instance_url, body.access_token)

    @router.post("/api/portal/preflight")
    async def portal_preflight(body: PortalRequest) -> Dict[str, Any]:
        url, token = creds(body)
        report = await run_preflight(OrgClient(url, token, body.api_version), spec_from(body))
        return report.to_dict()

    @router.post("/api/portal/plan")
    async def portal_plan(body: PortalRequest) -> Dict[str, Any]:
        url, token = creds(body)
        spec = spec_from(body)
        org = OrgWriter(url, token, body.api_version)
        async with MetadataClient(url, token, body.api_version) as meta:
            result = await build_site(org, meta, spec, apply=False)
        return result_to_dict(result)

    @router.post("/api/portal/apply")
    async def portal_apply(body: PortalRequest) -> Dict[str, Any]:
        if not body.confirm:
            raise HTTPException(400, "Building writes to the org - confirm it first.")
        url, token = creds(body)
        spec = spec_from(body)

        report = await run_preflight(OrgClient(url, token, body.api_version), spec)
        org_check = next((c for c in report.checks if c.id == "org"), None)
        if org_check is not None and org_check.status == "fail" and not body.confirm_production:
            raise HTTPException(
                403,
                "This looks like a production org. Building a site there needs "
                "confirm_production as well - and a sandbox or scratch org is the safer place to start.",
            )

        job_id = uuid.uuid4().hex
        job_holder: Dict[str, PortalJob] = {}

        async def run() -> SiteResult:
            org = OrgWriter(url, token, body.api_version)
            async with MetadataClient(url, token, body.api_version) as meta:
                return await build_site(
                    org, meta, spec, apply=True, progress=lambda step: job_holder["job"].steps.append(step)
                )

        task = asyncio.create_task(run())
        job = PortalJob(task=task)
        job_holder["job"] = job
        jobs[job_id] = job
        return {"job_id": job_id}

    @router.get("/api/portal/status")
    async def portal_status(job_id: str) -> Dict[str, Any]:
        job = jobs.get(job_id)
        if job is None:
            raise HTTPException(404, "Unknown job - the server may have restarted.")
        steps = [asdict(s) for s in job.steps]
        if not job.task.done():
            return {"done": False, "steps": steps}
        jobs.pop(job_id, None)
        try:
            result = job.task.result()
        except Exception as exc:  # noqa: BLE001 - surfaced to the user as text
            return {"done": True, "ok": False, "steps": steps, "error": f"{type(exc).__name__}: {exc}"}
        return {"done": True, **result_to_dict(result)}

    return router
