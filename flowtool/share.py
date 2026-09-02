"""
"Share this" - snapshot a Session or PlanSession, hand back an unguessable
link and an auto-generated password, and let anyone with both resume from
that exact point within 24 hours.

Storage is Upstash Redis (REST API, no extra SDK dependency - plain httpx
calls, same library the rest of this app already uses for Salesforce). Redis
was picked specifically for its native key TTL: a share expires itself, no
cleanup job needed. Heroku itself ships no free database any more, and
Upstash's free tier is a good fit for a low-volume, short-lived blob like
this one.

The token in the URL (22 random chars, `secrets.token_urlsafe`) is already
unguessable on its own; the password is a second factor for the case where
the link leaks somewhere the token's entropy doesn't help (a browser history
sync, a chat log, a referrer header) - short and easy to read aloud, since
it is layered on top of an already-unguessable URL rather than being the
only thing standing between a stranger and the snapshot.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import string
from typing import Any, Dict, Optional

import httpx

SHARE_TTL_SECONDS = 24 * 60 * 60
_ATTEMPTS_TTL_SECONDS = 15 * 60
_MAX_ATTEMPTS = 5

_PBKDF2_ITERATIONS = 200_000


class ShareError(Exception):
    """A user-facing problem with a share - not configured, expired/unknown,
    wrong password, or too many wrong guesses. Always safe to show as-is."""


def configured() -> bool:
    return bool(os.environ.get("UPSTASH_REDIS_REST_URL")) and bool(
        os.environ.get("UPSTASH_REDIS_REST_TOKEN")
    )


def _base_and_headers() -> tuple[str, Dict[str, str]]:
    url = os.environ.get("UPSTASH_REDIS_REST_URL", "").rstrip("/")
    token = os.environ.get("UPSTASH_REDIS_REST_TOKEN", "")
    if not url or not token:
        raise ShareError(
            "Sharing isn't configured on this server - UPSTASH_REDIS_REST_URL "
            "and UPSTASH_REDIS_REST_TOKEN aren't set."
        )
    return url, {"Authorization": f"Bearer {token}"}


async def _redis_set(key: str, value: str, ex: int) -> None:
    base, headers = _base_and_headers()
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(
            f"{base}/set/{key}", params={"EX": ex}, content=value.encode("utf-8"),
            headers=headers,
        )
        resp.raise_for_status()


async def _redis_get(key: str) -> Optional[str]:
    base, headers = _base_and_headers()
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(f"{base}/get/{key}", headers=headers)
        resp.raise_for_status()
        return resp.json().get("result")


async def _redis_incr_with_expiry(key: str, ex: int) -> int:
    """INCR, then EXPIRE only on the first hit - a repeat failure inside the
    same window must not keep pushing the lockout further into the future."""
    base, headers = _base_and_headers()
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(f"{base}/incr/{key}", headers=headers)
        resp.raise_for_status()
        count = int(resp.json()["result"])
        if count == 1:
            await client.post(f"{base}/expire/{key}/{ex}", headers=headers)
        return count


def generate_token() -> str:
    return secrets.token_urlsafe(16)


def generate_password() -> str:
    """A 6-digit numeric PIN - short enough to read aloud or retype, backed
    by the token's own entropy for the actual unguessability."""
    return "".join(secrets.choice(string.digits) for _ in range(6))


def _hash_password(password: str, salt: Optional[bytes] = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _PBKDF2_ITERATIONS)
    return f"{salt.hex()}:{digest.hex()}"


def _verify_password(password: str, stored: str) -> bool:
    salt_hex, _, _ = stored.partition(":")
    candidate = _hash_password(password, bytes.fromhex(salt_hex))
    return hmac.compare_digest(candidate, stored)


async def create_share(token: str, password: str, snapshot: Dict[str, Any]) -> None:
    record = {"password_hash": _hash_password(password), "snapshot": snapshot}
    await _redis_set(f"share:{token}", json.dumps(record), ex=SHARE_TTL_SECONDS)


async def consume_password(token: str, password: str) -> Dict[str, Any]:
    """Verify `password` against the share at `token` and return its
    snapshot. Wrong guesses are rate-limited per token, independent of the
    share's own 24h TTL, so a short numeric PIN can't just be brute-forced."""
    attempts_key = f"share_attempts:{token}"
    attempts = await _redis_get(attempts_key)
    if attempts is not None and int(attempts) >= _MAX_ATTEMPTS:
        raise ShareError("Too many wrong passwords. This link is locked for a while - ask for a new one.")

    raw = await _redis_get(f"share:{token}")
    if raw is None:
        raise ShareError("This share link is unknown or has expired.")
    record = json.loads(raw)

    if not _verify_password(password, record["password_hash"]):
        count = await _redis_incr_with_expiry(attempts_key, _ATTEMPTS_TTL_SECONDS)
        remaining = max(_MAX_ATTEMPTS - count, 0)
        if remaining == 0:
            raise ShareError("Too many wrong passwords. This link is locked for a while - ask for a new one.")
        raise ShareError(f"Wrong password ({remaining} attempt(s) left).")

    return record["snapshot"]
