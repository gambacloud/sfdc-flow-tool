"""
Run a test scenario as Anonymous Apex against an org and report what it cost.

The script is wrapped in a savepoint and rolled back, so nothing a scenario
inserts stays in the org. The Apex SOAP executeAnonymous call (not the Tooling
REST one) is used because it hands the debug log back in the response itself,
and that log ends with a CUMULATIVE_LIMIT_USAGE block - governor limits for
free, no instrumentation of the generated script.

A rolled-back transaction never commits, so anything asynchronous (future,
queueable, async flow paths, scheduled) does not run here; a scenario that
depends on one is a manual step, see flowtool/scenarios.py.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
from xml.sax.saxutils import escape

import httpx

from .sfdc import _HEADERS, _fault_string, _normalise

APEX_NS = "http://soap.sforce.com/2006/08/apex"

WARN_AT = 0.70
CRITICAL_AT = 0.90

# Limits worth a heads-up, and what to do about each when it runs hot.
_ADVICE = {
    "Number of SOQL queries": "A query per record will break at scale - query once, outside the loop.",
    "Number of DML statements": "DML per record will break at scale - collect records and write them once.",
    "Number of DML rows": "Close to the DML row cap for one transaction.",
    "Number of query rows": "Close to the query row cap - tighten the WHERE clause or add a LIMIT.",
    "Maximum CPU time": "CPU heavy - look for nested loops or work done per record that could be done once.",
    "Maximum heap size": "Large in-memory collections - process in smaller batches.",
    "Number of callouts": "Close to the callout cap for one transaction.",
}


@dataclass
class LimitUsage:
    name: str
    used: int
    allowed: int

    @property
    def ratio(self) -> float:
        return self.used / self.allowed if self.allowed else 0.0

    @property
    def level(self) -> str:
        if self.ratio >= CRITICAL_AT:
            return "critical"
        if self.ratio >= WARN_AT:
            return "warn"
        return "ok"

    def as_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name, "used": self.used, "allowed": self.allowed,
            "percent": round(self.ratio * 100, 1), "level": self.level,
        }


@dataclass
class ScenarioRun:
    success: bool
    compiled: bool = True
    problem: Optional[str] = None
    limits: List[LimitUsage] = field(default_factory=list)
    log_excerpt: str = ""

    @property
    def warnings(self) -> List[str]:
        out = []
        for lim in self.limits:
            if lim.level == "ok":
                continue
            tip = _ADVICE.get(lim.name, "")
            out.append(
                f"{lim.name}: {lim.used} of {lim.allowed} ({lim.ratio:.0%}). {tip}".strip()
            )
        return out

    def as_dict(self) -> Dict[str, Any]:
        return {
            "success": self.success, "compiled": self.compiled, "problem": self.problem,
            "limits": [lim.as_dict() for lim in self.limits],
            "warnings": self.warnings, "log_excerpt": self.log_excerpt,
        }


def wrap_script(body: str) -> str:
    """The scenario inside a savepoint that is always rolled back. The
    savepoint and the rollback each count as a DML statement."""
    return (
        "Savepoint forgeSp = Database.setSavepoint();\n"
        "try {\n"
        f"{body}\n"
        "} finally {\n"
        "    Database.rollback(forgeSp);\n"
        "}\n"
    )


_LIMIT_LINE = re.compile(r"^\s*(.+?):\s*(\d+)\s+out of\s+(\d+)\s*$")


def parse_limits(log: str) -> List[LimitUsage]:
    """The first (default-namespace) CUMULATIVE_LIMIT_USAGE block of a debug log."""
    limits: List[LimitUsage] = []
    inside = False
    for line in log.splitlines():
        if "|CUMULATIVE_LIMIT_USAGE_END" in line:
            break
        if "|LIMIT_USAGE_FOR_NS|" in line:
            if inside and limits:
                break  # only the default namespace
            inside = True
            continue
        if not inside:
            continue
        m = _LIMIT_LINE.match(line)
        if m and int(m.group(3)) > 0:
            limits.append(LimitUsage(m.group(1), int(m.group(2)), int(m.group(3))))
    return limits


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _text(root: ET.Element, name: str) -> Optional[str]:
    for el in root.iter():
        if _local(el.tag) == name:
            return el.text
    return None


def parse_response(payload: str) -> ScenarioRun:
    root = ET.fromstring(payload)
    log = _text(root, "debugLog") or ""
    compiled = (_text(root, "compiled") or "").lower() == "true"
    success = (_text(root, "success") or "").lower() == "true"
    problem = None
    if not compiled:
        where = f" (line {_text(root, 'line')})" if _text(root, "line") else ""
        problem = (_text(root, "compileProblem") or "Did not compile") + where
    elif not success:
        problem = _text(root, "exceptionMessage") or "The script threw an exception"
    return ScenarioRun(
        success=success, compiled=compiled, problem=problem,
        limits=parse_limits(log), log_excerpt=log[-4000:],
    )


def _envelope(session_id: str, script: str) -> str:
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/"'
        f' xmlns:apex="{APEX_NS}"><soapenv:Header>'
        "<apex:DebuggingHeader>"
        "<apex:categories><apex:category>Apex_code</apex:category><apex:level>DEBUG</apex:level></apex:categories>"
        "<apex:categories><apex:category>Apex_profiling</apex:category><apex:level>FINE</apex:level></apex:categories>"
        "<apex:categories><apex:category>Workflow</apex:category><apex:level>INFO</apex:level></apex:categories>"
        "</apex:DebuggingHeader>"
        f"<apex:SessionHeader><apex:sessionId>{escape(session_id)}</apex:sessionId></apex:SessionHeader>"
        "</soapenv:Header><soapenv:Body>"
        f"<apex:executeAnonymous><apex:String>{escape(script)}</apex:String></apex:executeAnonymous>"
        "</soapenv:Body></soapenv:Envelope>"
    )


async def is_production(instance_url: str, token: str, api_version: str = "64.0") -> bool:
    """True unless the org says it is a sandbox or a Developer/scratch org."""
    base = _normalise(instance_url)
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(
            f"{base}/services/data/v{api_version}/query",
            params={"q": "SELECT IsSandbox, OrganizationType FROM Organization"},
            headers={"Authorization": f"Bearer {token}"},
        )
    if resp.status_code != 200:
        raise RuntimeError(f"Could not tell what kind of org this is ({resp.status_code}): {resp.text[:300]}")
    rows = resp.json().get("records") or []
    if not rows:
        return True
    row = rows[0]
    return not row.get("IsSandbox") and "Developer" not in str(row.get("OrganizationType"))


async def run_anonymous(
    instance_url: str, token: str, body: str, api_version: str = "64.0",
) -> ScenarioRun:
    endpoint = f"{_normalise(instance_url)}/services/Soap/s/{api_version}"
    async with httpx.AsyncClient(timeout=120.0) as client:
        resp = await client.post(
            endpoint, content=_envelope(token, wrap_script(body)).encode("utf-8"),
            headers=_HEADERS,
        )
    if resp.status_code != 200:
        fault = _fault_string(resp.text)
        raise RuntimeError(f"Salesforce rejected the call: {fault}" if fault else
                           f"executeAnonymous failed ({resp.status_code}): {resp.text[:600]}")
    return parse_response(resp.text)
