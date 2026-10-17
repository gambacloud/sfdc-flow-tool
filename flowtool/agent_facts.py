"""
What the things an agent's actions call actually look like, read from the steps
that generated them, so an action's parameters can be checked against them.

An action's input and output names must be exactly the names the Apex class,
Flow or Prompt Template declares - the org's deploy checks that the target
exists but not that the parameter names match, so a mismatch deploys cleanly and
fails when the agent runs.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set

from .ir import Flow
from .ir_apex import ApexClass
from .ir_prompt_template import PromptTemplate

_CLASS_RE = re.compile(r"\bclass\s+(\w+)\s*\{")
_VAR_RE = re.compile(r"@InvocableVariable[^;{]*?\bpublic\s+[\w<>,.\s]+?\s+(\w+)\s*;", re.S)
_METHOD_RE = re.compile(
    r"@InvocableMethod[^\n]*\n?\s*(?:@\w+[^\n]*\n\s*)*public\s+static\s+([\w<>,.\s]+?)\s+\w+\s*\(\s*([\w<>,.\s]+?)\s+\w+\s*\)",
    re.S,
)


@dataclass
class TargetFacts:
    kind: str  # apex | flow | prompt
    inputs: Optional[Set[str]] = None   # None = could not be worked out, do not check
    outputs: Optional[Set[str]] = None


def _inner_class_vars(body: str, class_name: str) -> Optional[Set[str]]:
    match = re.search(r"\bclass\s+%s\s*\{" % re.escape(class_name), body)
    if not match:
        return None
    depth, i = 1, match.end()
    start = i
    while i < len(body) and depth:
        depth += {"{": 1, "}": -1}.get(body[i], 0)
        i += 1
    return set(_VAR_RE.findall(body[start:i]))


def _element_type(type_text: str) -> str:
    inner = re.search(r"<\s*([\w.]+)\s*>", type_text)
    return (inner.group(1) if inner else type_text).split(".")[-1].strip()


def apex_facts(apex: ApexClass) -> TargetFacts:
    """Inputs are the @InvocableVariable members of the method's parameter class,
    outputs those of its return class. Anything the parse cannot place stays unchecked."""
    method = _METHOD_RE.search(apex.body)
    if not method:
        return TargetFacts("apex")
    returns, takes = _element_type(method.group(1)), _element_type(method.group(2))
    return TargetFacts(
        "apex",
        inputs=_inner_class_vars(apex.body, takes),
        outputs=None if returns == "void" else _inner_class_vars(apex.body, returns),
    )


def flow_facts(flow: Flow) -> TargetFacts:
    return TargetFacts(
        "flow",
        inputs={v.name for v in flow.variables if v.is_input},
        outputs={v.name for v in flow.variables if v.is_output},
    )


def prompt_facts(template: PromptTemplate) -> TargetFacts:
    return TargetFacts("prompt", inputs={i.name for i in template.inputs}, outputs=None)


def collect(results: Dict[str, object]) -> Dict[str, TargetFacts]:
    """target api name -> facts, from the finished steps an agent depends on."""
    facts: Dict[str, TargetFacts] = {}
    for value in results.values():
        if isinstance(value, ApexClass):
            facts[value.api_name] = apex_facts(value)
        elif isinstance(value, Flow):
            facts[value.api_name] = flow_facts(value)
        elif isinstance(value, PromptTemplate):
            facts[value.api_name] = prompt_facts(value)
    return facts


def problems(agent_payload: dict, facts: Dict[str, TargetFacts]) -> List[str]:
    """Mismatches between a raw agent payload and what its targets declare."""
    out: List[str] = []
    for topic in agent_payload.get("topics") or []:
        for action in topic.get("actions") or []:
            target, kind = action.get("target"), action.get("target_type")
            known = facts.get(target)
            where = f"action {action.get('api_name')!r} in topic {topic.get('api_name')!r}"
            if known is None:
                continue  # not built in this plan - it may already exist in the org
            if known.kind != kind:
                out.append(f"{where}: {target!r} is a {known.kind} target, not {kind!r}")
                continue
            for label, declared, wanted in (
                ("input", known.inputs, [p.get("name") for p in action.get("inputs") or []]),
                ("output", known.outputs, [p.get("name") for p in action.get("outputs") or []]),
            ):
                if declared is None:
                    continue
                bad = sorted(n for n in wanted if n not in declared)
                if bad:
                    out.append(
                        f"{where}: {label} name(s) {bad} do not exist on {target!r}; "
                        f"it declares {sorted(declared) or 'none'} - use exactly those names"
                    )
    return out
