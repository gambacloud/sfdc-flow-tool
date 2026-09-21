"""
A per-browser activity feed: the `flowtool` logger's records (model calls,
repair attempts, schema fallbacks, ...) held in memory so the UI's Logs panel
can show what a long-running step is actually doing.

The server is shared between users, so every record is tagged with the client
id of the request that caused it and is only ever served back to that client.
A record with no client id (a thread that didn't inherit the request's
context) is dropped rather than shown to whoever asks next.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from contextvars import ContextVar
from typing import Any, Deque, Dict, List, Optional, Tuple

# Set per request by the server's middleware. asyncio tasks and
# asyncio.to_thread copy the context, so the work a request kicks off keeps it.
client_id: ContextVar[Optional[str]] = ContextVar("flowtool_client_id", default=None)

_MAX_ENTRIES = 3000
_MAX_MESSAGE = 600

_lock = threading.Lock()
_entries: Deque[Tuple[int, float, str, str, str]] = deque(maxlen=_MAX_ENTRIES)
_next_seq = 1


class ActivityHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        global _next_seq
        cid = client_id.get()
        if not cid:
            return
        try:
            message = record.getMessage()
        except Exception:  # a bad format string must not break the request
            return
        if len(message) > _MAX_MESSAGE:
            message = message[:_MAX_MESSAGE] + "..."
        with _lock:
            _entries.append((_next_seq, time.time(), cid, record.levelname, message))
            _next_seq += 1


def install() -> None:
    """Attach the handler to the `flowtool` logger once, at INFO."""
    logger = logging.getLogger("flowtool")
    if any(isinstance(h, ActivityHandler) for h in logger.handlers):
        return
    logger.addHandler(ActivityHandler())
    if logger.getEffectiveLevel() > logging.INFO:
        logger.setLevel(logging.INFO)


def since(cid: str, after: int = 0) -> Dict[str, Any]:
    """This client's entries with seq > `after`, and the seq to ask from next."""
    with _lock:
        rows = [e for e in _entries if e[0] > after and e[2] == cid]
        latest = _next_seq - 1
    entries: List[Dict[str, Any]] = [
        {"seq": seq, "time": ts, "level": level, "message": message}
        for seq, ts, _cid, level, message in rows
    ]
    return {"entries": entries, "next": latest}
