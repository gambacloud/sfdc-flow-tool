"""
"Save session" files: a Session or PlanSession snapshot (the same one Share
uses) wrapped with a small envelope and a descriptive file name, so the work
can be kept on disk and restored later - no server storage, no expiry.

    PROJ-123__plan-lead-scoring__v3__2026-10-25_1430.sfdcbuildertool.json

The envelope carries a format marker and a schema version so a file that is
not ours, or comes from a newer build, is refused with a clear message rather
than half-loaded. API keys and org tokens are never part of a snapshot.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Dict, Optional

SAVE_FORMAT = "sfdcbuildertool"
SAVE_SCHEMA_VERSION = 1
SAVE_EXTENSION = ".sfdcbuildertool.json"
MAX_SAVE_BYTES = 5 * 1024 * 1024

_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


class SaveFileError(Exception):
    pass


def slug(text: Optional[str], limit: int = 40) -> str:
    """Safe for a file name on every OS: letters, digits, . _ - only."""
    cleaned = _UNSAFE.sub("-", (text or "").strip()).strip("-._")
    return cleaned[:limit].strip("-._")


def filename(
    *,
    reference: Optional[str],
    name: Optional[str],
    kind: str,
    title: Optional[str],
    version: Optional[int],
    now: Optional[datetime] = None,
) -> str:
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y-%m-%d_%H%M")
    # A name the person typed beats the generated title.
    label = slug(name) or slug(title)
    parts = [
        slug(reference, 30),
        "-".join(p for p in (slug(kind, 12), label) if p),
        f"v{version}" if version else "",
        stamp,
    ]
    return "__".join(p for p in parts if p) + SAVE_EXTENSION


def wrap(snapshot: Dict[str, Any], *, name: Optional[str], title: Optional[str]) -> Dict[str, Any]:
    return {
        "format": SAVE_FORMAT,
        "schema_version": SAVE_SCHEMA_VERSION,
        "saved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "name": (name or "").strip() or None,
        "title": title,
        "reference": snapshot.get("reference"),
        "snapshot": snapshot,
    }


def unwrap(data: Any) -> Dict[str, Any]:
    if not isinstance(data, dict) or data.get("format") != SAVE_FORMAT:
        raise SaveFileError("This is not a SFDCBUILDERTOOL session file.")
    version = data.get("schema_version")
    if not isinstance(version, int) or version > SAVE_SCHEMA_VERSION:
        raise SaveFileError(
            "This file was saved by a newer version of the tool. Update the tool and try again."
        )
    snapshot = data.get("snapshot")
    if not isinstance(snapshot, dict) or snapshot.get("type") not in ("session", "plan"):
        raise SaveFileError("The session file is damaged: no saved session inside.")
    return snapshot
