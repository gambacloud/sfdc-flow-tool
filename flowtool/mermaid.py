"""
IR -> Mermaid + Markdown.

The diagram is a *view*, not the source of truth. It is allowed to show things
the IR does not contain (explicit End nodes, humanised conditions) because its
job is comprehension, not round-tripping. Anything the user approves here is
backed by the IR, which is what actually compiles to XML.
"""

from __future__ import annotations

from typing import Dict, Iterable, List, Optional, Tuple

from .ir import (
    ActionCall,
    Assignment,
    Condition,
    Decision,
    Element,
    Flow,
    GetRecords,
    Loop,
    RecordCreate,
    RecordDelete,
    RecordFilter,
    RecordUpdate,
    Subflow,
    Value,
)

# Human-readable forms for the operators. Falls back to the raw name so a new
# operator in the IR never breaks rendering.
_OPERATOR_TEXT = {
    "EqualTo": "=",
    "NotEqualTo": "!=",
    "GreaterThan": ">",
    "GreaterThanOrEqualTo": ">=",
    "LessThan": "<",
    "LessThanOrEqualTo": "<=",
    "StartsWith": "starts with",
    "EndsWith": "ends with",
    "Contains": "contains",
    "IsNull": "is null",
    "IsChanged": "is changed",
    "WasSet": "was set",
}

_UNARY = {"IsNull", "IsChanged", "WasSet"}

_END_NODE = "FLOW_END"

# Mermaid labels are single-line; a real newline ends the label and breaks the
# parser, so multi-line captions are joined with an explicit line break.
_BR = "<br/>"


def _escape(text: str) -> str:
    """
    Mermaid label escaping. Quotes terminate a label, and the pipe/brace
    characters confuse the parser even inside quotes. Newlines become explicit
    line breaks - a raw newline ends the label.
    """
    return (
        text.replace('"', "#quot;")
        .replace("|", "#124;")
        .replace("{", "#123;")
        .replace("}", "#125;")
        .replace("\n", _BR)
    )


def value_text(value: Value) -> str:
    if value.string_value is not None:
        return f'"{value.string_value}"'
    if value.number_value is not None:
        number = value.number_value
        return str(int(number)) if float(number).is_integer() else str(number)
    if value.boolean_value is not None:
        return "true" if value.boolean_value else "false"
    if value.date_value is not None:
        return value.date_value
    if value.date_time_value is not None:
        return value.date_time_value
    if value.element_reference is not None:
        return value.element_reference
    return "?"


# Salesforce spells negation as a boolean on the right of a unary operator:
# IsNull with false means "is not null". Reading only the operator would show
# the user the opposite of what the flow does.
_NEGATED_UNARY = {
    "IsNull": "is not null",
    "IsChanged": "is not changed",
    "WasSet": "was not set",
}


def _unary_text(left: str, operator: str, right: Optional[Value]) -> str:
    if right is not None and right.boolean_value is False:
        return f"{left} {_NEGATED_UNARY.get(operator, 'not ' + operator)}"
    return f"{left} {_OPERATOR_TEXT.get(operator, operator)}"


def condition_text(condition: Condition) -> str:
    if condition.operator in _UNARY:
        return _unary_text(condition.left, condition.operator, condition.right)
    operator = _OPERATOR_TEXT.get(condition.operator, condition.operator)
    return f"{condition.left} {operator} {value_text(condition.right)}"


def filter_text(record_filter: RecordFilter) -> str:
    if record_filter.operator in _UNARY:
        return _unary_text(
            record_filter.field, record_filter.operator, record_filter.value
        )
    operator = _OPERATOR_TEXT.get(record_filter.operator, record_filter.operator)
    if record_filter.value is None:
        return f"{record_filter.field} {operator}"
    return f"{record_filter.field} {operator} {value_text(record_filter.value)}"


def _join(parts: Iterable[str], logic: str) -> str:
    joined = f" {logic.upper()} ".join(parts)
    return joined


# --------------------------------------------------------------------------
# Diagram
# --------------------------------------------------------------------------


def _node(name: str, label: str, element: Optional[Element]) -> str:
    """Shape carries the element type, so the diagram is readable without a legend."""
    text = _escape(label)
    if element is None:
        return f'{name}(["{text}"])'
    if isinstance(element, Decision):
        return f'{name}{{"{text}"}}'
    if isinstance(element, Loop):
        return f'{name}[/"{text}"/]'
    if isinstance(element, Subflow):
        return f'{name}[["{text}"]]'
    if isinstance(element, ActionCall):
        # Mermaid's trapezoid ends with a literal backslash, hence the raw string.
        return rf'{name}[/"{text}"\]'
    if isinstance(element, (RecordCreate, RecordUpdate, RecordDelete, GetRecords)):
        return f'{name}[("{text}")]'
    return f'{name}["{text}"]'


def _element_caption(element: Element) -> str:
    """One line describing what the element actually does."""
    if isinstance(element, GetRecords):
        detail = f"Get {element.object}"
        if element.filters:
            detail += f" where {_join((filter_text(f) for f in element.filters), element.filter_logic)}"
        return f"{element.label}\n{detail}"
    if isinstance(element, RecordCreate):
        return f"{element.label}\nCreate {element.object or element.input_reference}"
    if isinstance(element, RecordUpdate):
        target = element.object or element.input_reference
        return f"{element.label}\nUpdate {target}"
    if isinstance(element, RecordDelete):
        target = element.object or element.input_reference
        return f"{element.label}\nDelete {target}"
    if isinstance(element, Assignment):
        first = element.items[0]
        detail = f"{first.to_reference} {first.operator.lower()} {value_text(first.value)}"
        if len(element.items) > 1:
            detail += f" (+{len(element.items) - 1} more)"
        return f"{element.label}\n{detail}"
    if isinstance(element, Loop):
        return f"{element.label}\nFor each {element.collection_reference}"
    if isinstance(element, Subflow):
        return f"{element.label}\nCall {element.flow_name}"
    return element.label


def _start_caption(flow: Flow) -> str:
    if not flow.start.object:
        return "Start\nAutolaunched"
    trigger = {
        "RecordAfterSave": "after save",
        "RecordBeforeSave": "before save",
        "RecordBeforeDelete": "before delete",
        "Scheduled": "scheduled",
    }.get(flow.start.trigger_type or "", flow.start.trigger_type or "")
    caption = f"{flow.start.object} {flow.start.record_trigger_type} ({trigger})"
    if flow.start.filters:
        caption += "\n" + _join(
            (filter_text(f) for f in flow.start.filters), flow.start.filter_logic
        )
    return f"Start\n{caption}"


def to_mermaid(flow: Flow) -> str:
    """Render the flow as a Mermaid flowchart."""
    by_name = flow.by_name()
    lines: List[str] = ["flowchart TD"]
    edges: List[str] = []
    needs_end = False

    lines.append("    " + _node("START", _start_caption(flow), None))
    for element in flow.elements:
        lines.append("    " + _node(element.name, _element_caption(element), element))

    def edge(source: str, target: Optional[str], label: str = "", dotted: bool = False) -> None:
        nonlocal needs_end
        if target is None:
            target = _END_NODE
            needs_end = True
        arrow = "-.->" if dotted else "-->"
        if label:
            edges.append(f"    {source} {arrow}|{_escape(label)}| {target}")
        else:
            edges.append(f"    {source} {arrow} {target}")

    # The start connector is the only one allowed to go nowhere without meaning
    # "end of path" - a flow with no start target is simply empty.
    if flow.start.next:
        edge("START", flow.start.next)

    for element in flow.elements:
        if isinstance(element, Decision):
            for outcome in element.outcomes:
                condition = _join(
                    (condition_text(c) for c in outcome.conditions), outcome.condition_logic
                )
                edge(element.name, outcome.next, f"{outcome.label}: {condition}")
            edge(element.name, element.next, element.default_outcome_label, dotted=True)
        elif isinstance(element, Loop):
            edge(element.name, element.first_element, "each")
            edge(element.name, element.next, "done", dotted=True)
        else:
            edge(element.name, element.next)

        # A fault path is real control flow; leaving it off the diagram would
        # mean approving a flow whose error handling is invisible.
        fault = getattr(element, "fault_next", None)
        if fault:
            edge(element.name, fault, "on error", dotted=True)

    if needs_end:
        lines.append("    " + _node(_END_NODE, "End", None))

    lines.extend(edges)
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Documentation
# --------------------------------------------------------------------------


_TYPE_LABEL = {
    Assignment: "Assignment",
    Decision: "Decision",
    GetRecords: "Get Records",
    RecordCreate: "Create Records",
    RecordUpdate: "Update Records",
    RecordDelete: "Delete Records",
    Loop: "Loop",
    Subflow: "Subflow",
    ActionCall: "Action",
}


def _element_detail(element: Element) -> str:
    if isinstance(element, Decision):
        parts = []
        for outcome in element.outcomes:
            condition = _join(
                (condition_text(c) for c in outcome.conditions), outcome.condition_logic
            )
            parts.append(f"**{outcome.label}** if {condition} -> `{outcome.next or 'End'}`")
        parts.append(f"**{element.default_outcome_label}** -> `{element.next or 'End'}`")
        return "<br>".join(parts)

    if isinstance(element, GetRecords):
        detail = f"`{element.object}`"
        if element.filters:
            detail += " where " + _join(
                (filter_text(f) for f in element.filters), element.filter_logic
            )
        detail += ", first record only" if element.first_record_only else ", all records"
        return detail

    if isinstance(element, (RecordCreate, RecordUpdate)):
        parts = []
        if element.object:
            parts.append(f"`{element.object}`")
        if element.input_reference:
            parts.append(f"from `{element.input_reference}`")
        if getattr(element, "filters", None):
            parts.append(
                "where " + _join((filter_text(f) for f in element.filters), element.filter_logic)
            )
        if element.fields:
            fields = ", ".join(
                f"{a.field} = {value_text(a.value)}" for a in element.fields
            )
            parts.append(f"set {fields}")
        return ", ".join(parts)

    if isinstance(element, RecordDelete):
        target = f"`{element.object}`" if element.object else f"`{element.input_reference}`"
        if element.filters:
            target += " where " + _join((filter_text(f) for f in element.filters), "and")
        return target

    if isinstance(element, Assignment):
        return "<br>".join(
            f"`{item.to_reference}` {item.operator.lower()} {value_text(item.value)}"
            for item in element.items
        )

    if isinstance(element, Loop):
        return (
            f"over `{element.collection_reference}` ({element.iteration_order}), "
            f"body starts at `{element.first_element or 'nothing'}`"
        )

    if isinstance(element, Subflow):
        detail = f"calls `{element.flow_name}`"
        if element.input_assignments:
            inputs = ", ".join(
                f"{a.name} = {value_text(a.value)}" for a in element.input_assignments
            )
            detail += f" with {inputs}"
        return detail

    return ""


def to_markdown(flow: Flow, include_diagram: bool = True) -> str:
    """Human-readable documentation for the flow, with the diagram embedded."""
    lines: List[str] = [f"# {flow.label}", ""]

    if flow.description:
        lines += [flow.description, ""]

    lines += ["## Trigger", ""]
    if flow.start.object:
        lines.append(
            f"- **Object**: `{flow.start.object}`"
        )
        lines.append(f"- **When**: {flow.start.record_trigger_type} / {flow.start.trigger_type}")
        if flow.start.filters:
            criteria = _join(
                (filter_text(f) for f in flow.start.filters), flow.start.filter_logic
            )
            lines.append(f"- **Entry criteria**: {criteria}")
    else:
        lines.append("- **Autolaunched** — invoked from Apex, another flow, or a process.")
    lines += ["", f"- **API name**: `{flow.api_name}`", f"- **API version**: {flow.api_version}",
              f"- **Status**: {flow.status}", ""]

    if include_diagram:
        lines += ["## Diagram", "", "```mermaid", to_mermaid(flow), "```", ""]

    lines += ["## Elements", "", "| Element | Type | What it does | Next |", "|---|---|---|---|"]
    for element in flow.elements:
        type_label = _TYPE_LABEL.get(type(element), type(element).__name__)
        detail = _element_detail(element) or "—"
        if isinstance(element, Decision):
            next_label = "see outcomes"
        elif isinstance(element, Loop):
            next_label = f"`{element.next}`" if element.next else "End"
        else:
            next_label = f"`{element.next}`" if element.next else "End"
        lines.append(f"| `{element.name}` | {type_label} | {detail} | {next_label} |")
    lines.append("")

    if flow.variables:
        lines += ["## Variables", "", "| Name | Type | Collection | Input | Output |",
                  "|---|---|---|---|---|"]
        for variable in flow.variables:
            data_type = variable.data_type
            if variable.object_type:
                data_type += f" ({variable.object_type})"
            lines.append(
                f"| `{variable.name}` | {data_type} | {'yes' if variable.is_collection else 'no'} "
                f"| {'yes' if variable.is_input else 'no'} | {'yes' if variable.is_output else 'no'} |"
            )
        lines.append("")

    return "\n".join(lines)
