"""
In-process sliding-window rate limit for the endpoints that spend LLM calls.

Keyed by client IP plus the X-Client-Id header (the id is client-chosen, so it
only separates users behind one NAT; a per-IP bucket with a higher ceiling
stops anyone rotating ids). State lives in this process: one dyno, reset on
restart. Limits come from the environment on every call; 0 disables a window.

  FLOWTOOL_RATE_PER_MIN  requests per minute per client (default 60)
  FLOWTOOL_RATE_PER_DAY  requests per 24 hours per client (default 2000)

The per-IP ceiling is IP_FACTOR times these.
"""

from __future__ import annotations

import os
import time
from collections import deque
from typing import Deque, Dict, Optional, Tuple

from fastapi import HTTPException, Request

MIN_ENV = "FLOWTOOL_RATE_PER_MIN"
DAY_ENV = "FLOWTOOL_RATE_PER_DAY"
DEFAULT_PER_MIN = 60
DEFAULT_PER_DAY = 2000
IP_FACTOR = 5
MAX_KEYS = 10000

# POST endpoints that start an LLM-backed job (matched as a path suffix so a
# mount prefix such as /flow-tool does not matter).
LIMITED_PATHS = (
    "/api/design/start", "/api/lwc/start", "/api/kb-chat/start", "/api/explain/start",
    "/api/refine/start", "/api/repair/start", "/api/import/parse", "/api/import/start",
    "/api/org-summary/start", "/api/plan/start", "/api/plan/execute/start",
    "/api/plan/repair/start", "/api/plan/step/revise/start", "/api/plan/revise/start",
    "/api/plan/agent/publish/start", "/api/reference", "/api/models",
)

_hits: Dict[Tuple[str, str], Deque[float]] = {}


def _limit(env: str, default: int) -> int:
    try:
        return max(0, int(os.environ.get(env, default)))
    except ValueError:
        return default


def client_ip(request: Request) -> str:
    """Last X-Forwarded-For entry: the one Heroku's router appended (earlier ones are client-supplied)."""
    forwarded = request.headers.get("x-forwarded-for", "")
    parts = [p.strip() for p in forwarded.split(",") if p.strip()]
    if parts:
        return parts[-1]
    return request.client.host if request.client else "unknown"


def is_limited(request: Request) -> bool:
    if request.method != "POST":
        return False
    path = request.url.path.rstrip("/")
    return any(path.endswith(p) for p in LIMITED_PATHS)


def _check(key: Tuple[str, str], now: float, per_min: int, per_day: int) -> Optional[str]:
    """Record a hit for `key`; return a message when a window is already full."""
    if len(_hits) > MAX_KEYS:
        for k in [k for k, d in _hits.items() if not d or now - d[-1] > 86400]:
            del _hits[k]
    hits = _hits.setdefault(key, deque())
    while hits and now - hits[0] > 86400:
        hits.popleft()
    if per_day and len(hits) >= per_day:
        return f"Daily limit of {per_day} model requests reached. Try again tomorrow."
    if per_min:
        recent = sum(1 for t in reversed(hits) if now - t <= 60)
        if recent >= per_min:
            return f"Too many requests ({per_min} per minute). Wait a minute and try again."
    hits.append(now)
    return None


async def guard(request: Request) -> None:
    """App-wide dependency: 429 an LLM-spending POST that is over its limit."""
    if not is_limited(request):
        return
    per_min = _limit(MIN_ENV, DEFAULT_PER_MIN)
    per_day = _limit(DAY_ENV, DEFAULT_PER_DAY)
    if not per_min and not per_day:
        return
    now = time.monotonic()
    ip = client_ip(request)
    cid = request.headers.get("x-client-id", "")[:64]
    message = _check((ip, cid), now, per_min, per_day)
    if message is None and cid:
        message = _check((ip, ""), now, per_min * IP_FACTOR, per_day * IP_FACTOR)
    if message:
        raise HTTPException(429, message, headers={"Retry-After": "60"})


def reset() -> None:
    _hits.clear()
