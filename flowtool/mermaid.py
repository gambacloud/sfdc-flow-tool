"""
IR -> Mermaid + Markdown.

The diagram is a *view*, not the source of truth. It is allowed to show things
the IR does not contain (explicit End nodes, humanised conditions) because its
job is comprehension, not round-tripping. Anything the user approves here is
backed by the IR, which is what actually compiles to XML.
"""

from __future__ import annotations

import re
from typing import Dict, Iterable, List, Optional, Tuple

from .ir import (
    ActionCall,
    Assignment,
    CollectionFilter,
    CollectionSort,
    Condition,
    CustomError,
    Decision,
    Element,
    Flow,
    GetRecords,
    Loop,
    OrchestratedStage,
    RecordCreate,
    RecordDelete,
    RecordFilter,
    RecordUpdate,
    Screen,
    Subflow,
    Transform,
    Value,
    Wait,
    referenced_conditions,
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


def _escape_pipes(text: str) -> str:
    """A pipe in a formula would otherwise split the Markdown table cell."""
    return text.replace("|", "\\|").replace("\n", " ")


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


_UNIT_WORD = {"Minutes": "minute", "Hours": "hour", "Days": "day", "Months": "month"}


def _path_timing(path) -> str:
    """
    When a scheduled path fires, in words.

    The offset is a signed number in the metadata and "-2 Days" is not how
    anyone says it, so the sign becomes before/after and the unit is
    pluralised. This label is the only place the timing appears on the diagram.
    """
    if path.run_asynchronously:
        return "right after saving, separately"
    if path.offset_number is None or not path.offset_unit:
        return path.label or path.name
    size = abs(path.offset_number)
    unit = _UNIT_WORD.get(path.offset_unit, path.offset_unit.lower())
    when = "before" if path.offset_number < 0 else "after"
    anchor = (
        f"`{path.record_field}`" if path.time_source == "RecordField"
        else "the trigger"
    )
    if size == 0:
        return f"at {anchor}"
    return f"{size} {unit}{'' if size == 1 else 's'} {when} {anchor}"


_EVENT_WORD = {
    "AlarmEvent": "until a time",
    "DateRefAlarmEvent": "until a date on a record",
}


def _wait_parameter(event, key: str) -> Optional[str]:
    for parameter in event.input_parameters:
        if parameter.name == key:
            return value_text(parameter.value)
    return None


def _wait_event_text(event) -> str:
    """
    What a wait event is waiting for, as far as the IR can tell.

    The parameters are a name/value bag because the same tag carries a platform
    event as carries a time, so this reads the keys it knows and falls back to
    naming the event type. A reader needs to see something about why the flow
    stopped: a Pause with no explanation is the least reviewable thing a flow
    can contain.
    """
    if event.event_type == "AlarmEvent":
        when = _wait_parameter(event, "AlarmTime")
        return f"until {when}" if when else "until a time"
    if event.event_type == "DateRefAlarmEvent":
        field = _wait_parameter(event, "BaseDateTimeFieldName")
        offset = _wait_parameter(event, "TimeOffset")
        unit = _wait_parameter(event, "TimeOffsetUnit")
        anchor = field.strip('"') if field else "a date field"
        if offset and unit:
            return f"until {offset} {unit.strip(chr(34)).lower()} from {anchor}"
        return f"until {anchor}"
    return f"for {event.event_type}"


def _wait_caption(element) -> str:
    if not element.wait_events:
        return "waits for nothing"
    return "; ".join(_wait_event_text(event) for event in element.wait_events)


def _join(parts: Iterable[str], logic: str) -> str:
    """
    Combine conditions the way the flow does.

    With `and` or `or` that is one word between them. With a custom expression
    it cannot be: "1 OR (2 AND 3)" is a shape, not a separator, and pasting it
    between the conditions would read as though every one of them were joined
    that way. So the expression is shown as itself and the conditions are
    numbered underneath it, which is also how Flow Builder presents them.
    """
    parts = list(parts)
    if logic.lower() in ("and", "or"):
        return f" {logic.upper()} ".join(parts)

    numbered = ", ".join(f"({i}) {part}" for i, part in enumerate(parts, 1))
    detail = f"{logic}, where {numbered}"

    # A condition the expression never names is evaluated and then ignored.
    # Valid, and accepted everywhere, so the IR allows it - which leaves saying
    # so here as the only way anyone finds out.
    try:
        unused = sorted(set(range(1, len(parts) + 1)) - referenced_conditions(logic))
    except ValueError:
        unused = []
    if unused:
        listed = ", ".join(str(n) for n in unused)
        detail += f" (nothing in the expression uses {listed})"
    return detail


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
    if isinstance(element, OrchestratedStage):
        # Same subroutine shape as a Subflow: a stage is its own self-contained
        # unit of work, just made of steps instead of a called flow.
        return f'{name}[["{text}"]]'
    if isinstance(element, Screen):
        # Hexagon: the only place the flow stops and waits for a person.
        return f'{name}{{{{"{text}"}}}}'
    if isinstance(element, Wait):
        # Stadium, like Start and End: the flow genuinely stops here and a
        # later event starts it again, so it reads as a terminus, not a step.
        return f'{name}(["{text}"])'
    if isinstance(element, CustomError):
        # Stadium too: a thrown error is terminal, the same as End - just for
        # a reason the flow chose rather than one it ran out of steps.
        return f'{name}(["{text}"])'
    if isinstance(element, ActionCall):
        # Mermaid's trapezoid ends with a literal backslash, hence the raw string.
        return rf'{name}[/"{text}"\]'
    if isinstance(element, (RecordCreate, RecordUpdate, RecordDelete, GetRecords,
                            CollectionFilter, CollectionSort, Transform)):
        return f'{name}[("{text}")]'
    return f'{name}["{text}"]'


def _transform_is_generic(element: Transform) -> bool:
    """
    True when any action is one of the five shapes this build has never
    confirmed against a real org (Count, Sum, GetItemByIndex, InnerJoin,
    InvocableAction) - checkOnly accepts them with no input_parameters at
    all, so it cannot be used to learn what they actually need. Only Map is
    understood; the rest round-trip exactly but their shape is a guess.
    """
    return any(
        action.transform_type != "Map"
        for tv in element.transform_values
        for action in tv.actions
    )


def _transform_action_text(action) -> str:
    if action.transform_type == "Map":
        target = action.output_field_api_name or "(value)"
        return f"{target} = {value_text(action.value)}" if action.value else target
    params = ", ".join(
        f"{p.name} = {value_text(p.value)}" for p in action.input_parameters
    )
    return f"{action.transform_type}({params})"


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
    if isinstance(element, CollectionFilter):
        return f"{element.label}\nFilter {element.collection_reference}"
    if isinstance(element, CollectionSort):
        fields = ", ".join(o.sort_field for o in element.sort_options)
        return f"{element.label}\nSort {element.collection_reference} by {fields}"
    if isinstance(element, Wait):
        return f"{element.label}\n{_wait_caption(element)}"
    if isinstance(element, CustomError):
        first = element.messages[0].error_message
        detail = f'Reject: "{first}"'
        if len(element.messages) > 1:
            detail += f" (+{len(element.messages) - 1} more)"
        return f"{element.label}\n{detail}"
    if isinstance(element, Subflow):
        return f"{element.label}\nCall {element.flow_name}"
    if isinstance(element, OrchestratedStage):
        names = ", ".join(s.label or s.name for s in element.stage_steps)
        return f"{element.label}\n{names}"
    if isinstance(element, Transform):
        target = element.object_type or element.apex_class or "a value"
        detail = f"Build {target}"
        if _transform_is_generic(element):
            detail += " (shape not fully confirmed)"
        return f"{element.label}\n{detail}"
    if isinstance(element, Screen):
        inputs = [f.name for f in element.all_fields()
                  if f.field_type not in ("DisplayText", "RegionContainer",
                                          "Region")]
        if inputs:
            detail = "Asks for " + ", ".join(inputs)
        else:
            detail = "Shows text"
        return f"{element.label}\n{detail}"
    return element.label


def _schedule_text(schedule) -> str:
    when = {"Once": "once", "Daily": "every day", "Weekly": "every week"}.get(
        schedule.frequency, schedule.frequency.lower()
    )
    return f"{when}, starting {schedule.start_date} at {schedule.start_time}"


def _start_caption(flow: Flow) -> str:
    if flow.process_type == "Flow":
        return "Start\nRun by a user"
    if flow.start.schedule:
        return f"Start\nScheduled: {_schedule_text(flow.start.schedule)}"
    if not flow.start.object:
        return "Start\nAutolaunched"
    # A platform event has no record_trigger_type - it is only ever published -
    # so the usual "Object Create (after save)" shape would read
    # "My_Event__e None (platform event)".
    if flow.start.trigger_type == "PlatformEvent":
        caption = f"{flow.start.object} published"
    else:
        trigger = {
            "RecordAfterSave": "after save",
            "RecordBeforeSave": "before save",
            "RecordBeforeDelete": "before delete",
            "Scheduled": "scheduled",
        }.get(flow.start.trigger_type or "", flow.start.trigger_type or "")
        caption = (f"{flow.start.object} {flow.start.record_trigger_type} "
                   f"({trigger})")
    if flow.start.filters:
        caption += "\n" + _join(
            (filter_text(f) for f in flow.start.filters), flow.start.filter_logic
        )
    if flow.start.filter_formula:
        caption += f"\nwhere {flow.start.filter_formula}"
    if flow.start.flow_run_as_user:
        caption += f"\nruns as: {flow.start.flow_run_as_user}"
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
            # Unlike node labels, edge pipe-text is unquoted by default, so
            # parentheses or a colon in the text (e.g. "Success (200/201): ...")
            # get read as Mermaid syntax instead of label text. Quoting it,
            # like every node label already is, fixes that.
            edges.append(f'    {source} {arrow}|"{_escape(label)}"| {target}')
        else:
            edges.append(f"    {source} {arrow} {target}")

    # The start connector is the only one allowed to go nowhere without meaning
    # "end of path" - a flow with no start target is simply empty.
    if flow.start.next:
        edge("START", flow.start.next)

    # A scheduled path leaves the same Start node but is not a continuation of
    # the immediate run: the flow finishes, and later Salesforce comes back and
    # begins again here. Dotted, and labelled with when, because "3 days after
    # Close Date" is the whole point of the branch and invisible otherwise.
    for path in flow.start.scheduled_paths:
        edge("START", path.next, _path_timing(path), dotted=True)

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
        elif isinstance(element, Wait):
            # Every exit is dotted: the flow is not running between here and
            # there. Time passes on each of these edges, which is the one thing
            # a reader has to see about a Pause.
            for event in element.wait_events:
                edge(element.name, event.next, event.label or event.name,
                     dotted=True)
            edge(element.name, element.default_next, element.default_label,
                 dotted=True)
        else:
            edge(element.name, element.next)

        # A fault path is real control flow; leaving it off the diagram would
        # mean approving a flow whose error handling is invisible.
        fault = getattr(element, "fault_next", None)
        if fault:
            edge(element.name, fault, "on error", dotted=True)

        timeout = getattr(element, "timeout_next", None)
        if timeout:
            edge(element.name, timeout, "on timeout", dotted=True)

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
    Screen: "Screen",
    Wait: "Pause",
    Subflow: "Subflow",
    ActionCall: "Action",
    CollectionFilter: "Collection Filter",
    CollectionSort: "Collection Sort",
    CustomError: "Custom Error",
    OrchestratedStage: "Orchestration Stage",
    Transform: "Transform",
}

_FIELD_LABEL = {
    "DisplayText": "text",
    "InputField": "input",
    "LargeTextArea": "long text",
    "RadioButtons": "radio buttons",
    "DropdownBox": "dropdown",
    "MultiSelectCheckboxes": "checkboxes",
    "MultiSelectPicklist": "multi-select",
    "ComponentInstance": "component",
    "RegionContainer": "section",
    "Region": "column",
}


def _walk_fields(fields, depth: int = 0):
    """Every field with how deep it sits, so the layout survives in text."""
    for screen_field in fields:
        yield screen_field, depth
        if screen_field.fields:
            yield from _walk_fields(screen_field.fields, depth + 1)


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
        if element.output_assignments:
            detail += " → " + ", ".join(
                f"{a.field} into `{a.assign_to_reference}`"
                for a in element.output_assignments
            )
        elif element.output_reference:
            detail += f" → `{element.output_reference}`"
        if element.assign_null_values_if_no_records_found:
            detail += "; clears them when nothing is found"
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
        item = (f"`{element.assign_next_value_to_reference}`"
                if element.assign_next_value_to_reference
                else f"`{element.name}`")
        return (
            f"over `{element.collection_reference}` ({element.iteration_order}), "
            f"each item as {item}, "
            f"body starts at `{element.first_element or 'nothing'}`"
        )

    if isinstance(element, Screen):
        if not element.fields:
            return "no fields"
        parts = []
        # Nested, because a field inside a column is a field the reader has to
        # see. Indented rather than flattened, so the layout is legible too - a
        # two-column section reads differently from two fields in a row.
        for screen_field, depth in _walk_fields(element.fields):
            kind = _FIELD_LABEL.get(screen_field.field_type, screen_field.field_type)
            if screen_field.data_type:
                kind += f", {screen_field.data_type}"
            if screen_field.is_required:
                kind += ", required"
            if screen_field.field_type == "Region":
                width = next((p.value for p in screen_field.input_parameters
                              if p.name == "width"), None)
                if width is not None:
                    kind += f", width {value_text(width).strip(chr(34))}"
            detail = ("&nbsp;" * 4 * depth) + f"`{screen_field.name}` ({kind})"
            if screen_field.extension_name:
                detail += f" `{screen_field.extension_name}`"
            if screen_field.choice_references:
                detail += " from " + ", ".join(
                    f"`{ref}`" for ref in screen_field.choice_references
                )
            # A component's inputs and outputs are the whole of what it does
            # from the flow's side, and they are the part a reviewer can
            # actually check. The component's own behaviour is not in the flow.
            # A Region's only input parameter is its width, already shown in
            # the label above. Repeating it as a component input reads as
            # though a column took arguments.
            if screen_field.input_parameters and screen_field.field_type != "Region":
                detail += " in: " + ", ".join(
                    f"{p.name} = {value_text(p.value)}"
                    for p in screen_field.input_parameters
                )
            if screen_field.output_parameters:
                detail += " out: " + ", ".join(
                    f"{p.name} → `{p.assign_to_reference}`"
                    for p in screen_field.output_parameters
                )
            elif screen_field.store_output_automatically:
                detail += f" out: `{{!{screen_field.name}.…}}`"
            # Both of these change what the user actually experiences, and
            # neither is visible anywhere else in the documentation.
            if screen_field.visibility:
                shown = _join(
                    (condition_text(c) for c in screen_field.visibility.conditions),
                    screen_field.visibility.condition_logic,
                )
                detail += f" — shown when {shown}"
            if screen_field.validation:
                detail += (
                    f" — must satisfy `{_escape_pipes(screen_field.validation.formula_expression)}`"
                    f", else \"{screen_field.validation.error_message}\""
                )
            if screen_field.help_text:
                detail += " — has help text"
            parts.append(detail)
        return "<br>".join(parts)

    if isinstance(element, CollectionFilter):
        condition = _join(
            (condition_text(c) for c in element.conditions), element.condition_logic
        )
        return f"`{element.collection_reference}` where {condition}, tested as `{element.current_item}`"

    if isinstance(element, CollectionSort):
        return f"`{element.collection_reference}` by " + ", ".join(
            f"{o.sort_field} ({o.sort_order})" for o in element.sort_options
        )

    if isinstance(element, Wait):
        parts = []
        for event in element.wait_events:
            line = f"**{event.label or event.name}**: waits {_wait_event_text(event)}"
            if event.conditions:
                line += " and " + _join(
                    (condition_text(c) for c in event.conditions),
                    event.condition_logic,
                )
            parts.append(f"{line} -> `{event.next or 'End'}`")
        parts.append(
            f"**{element.default_label}** -> `{element.default_next or 'End'}`"
        )
        return "<br>".join(parts)

    if isinstance(element, ActionCall):
        detail = f"`{element.action_name}` ({element.action_type})"
        # Worth saying only when the flow said it. An action in its own
        # transaction cannot be rolled back with the rest, which is the whole
        # reason anyone sets this.
        if element.flow_transaction_model == "NewTransaction":
            detail += ", in its own transaction"
        elif element.flow_transaction_model == "CurrentTransaction":
            detail += ", in this transaction"
        if element.input_parameters:
            detail += " with " + ", ".join(
                f"{p.name} = {value_text(p.value)}"
                for p in element.input_parameters
            )
        if element.output_parameters:
            detail += " → " + ", ".join(
                f"{p.name} into `{p.assign_to_reference}`"
                for p in element.output_parameters
            )
        elif element.store_output_automatically:
            detail += f", results read as `{{!{element.name}.…}}`"
        if element.is_wait_until_completed:
            detail += ", waits for it to finish"
            if element.timeout_offset is not None and element.timeout_offset_unit:
                detail += (
                    f" (times out after {element.timeout_offset} "
                    f"{element.timeout_offset_unit.lower()} -> "
                    f"`{element.timeout_next or 'End'}`)"
                )
        return detail

    if isinstance(element, Subflow):
        detail = f"calls `{element.flow_name}`"
        if element.input_assignments:
            inputs = ", ".join(
                f"{a.name} = {value_text(a.value)}" for a in element.input_assignments
            )
            detail += f" with {inputs}"
        if element.output_assignments:
            detail += " → " + ", ".join(
                f"{a.name} into `{a.assign_to_reference}`"
                for a in element.output_assignments
            )
        elif element.store_output_automatically:
            detail += f", results read as `{{!{element.name}.…}}`"
        return detail

    if isinstance(element, OrchestratedStage):
        parts = []
        for step in element.stage_steps:
            kind = "Background" if step.step_subtype == "BackgroundStep" else "Interactive"
            part = f"**{step.label}** ({kind}): runs `{step.action_name}`"
            if step.assignees:
                who = ", ".join(
                    f"{a.assignee_type} {value_text(a.assignee)}" for a in step.assignees
                )
                part += f", assigned to {who}"
            if step.entry_conditions:
                part += " — enters when " + _join(
                    (condition_text(c) for c in step.entry_conditions),
                    step.entry_condition_logic,
                )
            if step.exit_conditions:
                part += " — exits when " + _join(
                    (condition_text(c) for c in step.exit_conditions),
                    step.exit_condition_logic,
                )
            parts.append(part)
        if element.exit_conditions:
            condition = _join(
                (condition_text(c) for c in element.exit_conditions),
                element.exit_condition_logic,
            )
            parts.append(f"stage exits when {condition}")
        return "<br>".join(parts)

    if isinstance(element, CustomError):
        parts = []
        for msg in element.messages:
            part = f'"{msg.error_message}"'
            if msg.is_field_error:
                part += f" (on `{msg.field_selection}`)"
            parts.append(part)
        return "Rejects the record: " + "; ".join(parts)

    if isinstance(element, Transform):
        target = element.object_type or element.apex_class or "a value"
        actions = [
            _transform_action_text(action)
            for tv in element.transform_values
            for action in tv.actions
        ]
        detail = f"Builds `{target}`"
        if actions:
            detail += ": " + "; ".join(actions)
        if _transform_is_generic(element):
            detail += (
                ". Note: Count/Sum/GetItemByIndex/InnerJoin/InvocableAction "
                "round-trip exactly but their real shape has never been "
                "confirmed against a live org - treat this as a raw readout, "
                "not a validated one."
            )
        return detail

    return ""


# Markdown syntax that reads fine in an exported table but would show up as
# literal backticks and asterisks in a plain UI panel - the detail text is
# authored once, for the table, and lightened for the panel here rather than
# maintained twice.
def _plain(markdown: str) -> str:
    text = markdown.replace("<br>", "\n")
    text = text.replace("`", "")
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    return text


def element_index(flow: Flow) -> List[Dict[str, str]]:
    """
    One row per element, in flow order: name, label, type, and the same detail
    the Documentation tab's table shows - the full logic inside the element,
    not just the one-line diagram caption. Meant for a panel next to the
    diagram, where the caption alone (Assignment: "set 1 field (+2 more)")
    is not enough to review what the element actually does.
    """
    return [
        {
            "name": element.name,
            "label": element.label,
            "type": _TYPE_LABEL.get(type(element), type(element).__name__),
            "detail": _plain(_element_detail(element)) or "",
        }
        for element in flow.elements
    ]


def to_markdown(flow: Flow, include_diagram: bool = True) -> str:
    """Human-readable documentation for the flow, with the diagram embedded."""
    lines: List[str] = [f"# {flow.label}", ""]

    if flow.description:
        lines += [flow.description, ""]

    # Above the diagram, not below it. These are the things that deploy and are
    # still probably wrong, so they belong where someone deciding whether to
    # approve will read them first - an element nothing reaches draws on the
    # diagram like any other and gives no sign that it never runs.
    for note in flow.warnings():
        first, _, rest = note.partition("\n")
        lines += ["> [!WARNING]", f"> {first}", ""]
        if rest.strip():
            lines += ["> <details><summary>details</summary>", ">", "> ```"]
            lines += [f"> {line}" for line in rest.splitlines()]
            lines += ["> ```", "> </details>", ""]

    lines += ["## Trigger", ""]
    if flow.process_type == "Flow":
        lines.append(
            "- **Screen flow** — run by a user, from an app, a record page, or a link."
        )
    elif flow.start.object:
        lines.append(
            f"- **Object**: `{flow.start.object}`"
        )
        if flow.start.trigger_type == "PlatformEvent":
            lines.append("- **When**: every time one of these events is published")
        else:
            lines.append(
                f"- **When**: {flow.start.record_trigger_type} / "
                f"{flow.start.trigger_type}"
            )
        if flow.start.filters:
            criteria = _join(
                (filter_text(f) for f in flow.start.filters), flow.start.filter_logic
            )
            lines.append(f"- **Entry criteria**: {criteria}")
        if flow.start.filter_formula:
            lines.append(f"- **Entry criteria (formula)**: `{flow.start.filter_formula}`")
        if flow.start.flow_run_as_user:
            lines.append(f"- **Runs as**: {flow.start.flow_run_as_user}")
        for path in flow.start.scheduled_paths:
            name = path.label or path.name
            lines.append(
                f"- **Scheduled path** `{path.name}`: {_path_timing(path)} → "
                f"`{path.next or 'nothing'}`"
                + (f" ({name})" if path.label and path.label != path.name else "")
            )
    elif flow.start.schedule:
        lines.append(f"- **Scheduled**: {_schedule_text(flow.start.schedule)}")
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
        elif isinstance(element, Wait):
            # A Pause has no plain `next` - the IR refuses one - so the column
            # would otherwise read "End" for an element that continues.
            next_label = "see events"
        elif isinstance(element, Loop):
            next_label = f"`{element.next}`" if element.next else "End"
        else:
            next_label = f"`{element.next}`" if element.next else "End"
        lines.append(f"| `{element.name}` | {type_label} | {detail} | {next_label} |")
    lines.append("")

    # A formula is logic, and it is the one thing here nothing can check - only
    # the org knows whether the expression is valid, and only a person knows
    # whether it is right. So it is shown verbatim.
    if flow.constants or flow.formulas:
        lines += ["## Resources", "", "| Name | Kind | Type | Value |",
                  "|---|---|---|---|"]
        for constant in flow.constants:
            lines.append(
                f"| `{constant.name}` | Constant | {constant.data_type} "
                f"| {value_text(constant.value)} |"
            )
        for formula in flow.formulas:
            data_type = formula.data_type
            if formula.scale is not None:
                data_type += f", {formula.scale} dp"
            expression = _escape_pipes(formula.expression)
            lines.append(
                f"| `{formula.name}` | Formula | {data_type} | `{expression}` |"
            )
        lines.append("")

    # A template's body is what gets emailed or shown. Summarising it would mean
    # approving one thing and sending another, so it is quoted in full.
    if flow.text_templates:
        lines += ["## Text templates", ""]
        for template in flow.text_templates:
            kind = "plain text" if template.is_viewed_as_plain_text else "rich text"
            lines.append(f"**`{template.name}`** ({kind})")
            if template.description:
                lines.append(f"_{template.description}_")
            lines += ["", "```", template.text, "```", ""]

    # A picker whose options are not listed is a picker approved unseen.
    if flow.choices or flow.dynamic_choice_sets:
        lines += ["## Options", "", "| Name | Kind | What it offers |", "|---|---|---|"]
        for choice in flow.choices:
            shown = f'"{choice.choice_text}"'
            if choice.value is not None:
                shown += f" (stores {value_text(choice.value)})"
            if choice.user_input is not None:
                shown += ", lets the user type their own answer"
                if choice.user_input.is_required:
                    shown += " (required)"
            lines.append(f"| `{choice.name}` | Choice | {shown} |")
        for choice_set in flow.dynamic_choice_sets:
            if choice_set.picklist_field:
                offers = (
                    f"the values of `{choice_set.picklist_object}."
                    f"{choice_set.picklist_field}`"
                )
            else:
                offers = (
                    f"one per `{choice_set.object}` record, showing "
                    f"`{choice_set.display_field}`, storing `{choice_set.value_field}`"
                )
                if choice_set.filters:
                    offers += " where " + _join(
                        (filter_text(f) for f in choice_set.filters),
                        choice_set.filter_logic,
                    )
                if choice_set.limit is not None:
                    offers += f", first {choice_set.limit}"
            lines.append(f"| `{choice_set.name}` | Choice set | {offers} |")
        lines.append("")

    if flow.variables:
        lines += ["## Variables", "",
                  "| Name | Type | Collection | Input | Output | Default | Notes |",
                  "|---|---|---|---|---|---|---|"]
        for variable in flow.variables:
            data_type = variable.data_type
            if variable.object_type:
                data_type += f" ({variable.object_type})"
            if variable.scale is not None:
                data_type += f", {variable.scale} dp"
            # A default is not documentation - it is what the variable holds
            # before anything runs, so it belongs where the flow is approved.
            default = value_text(variable.value) if variable.value else "—"
            lines.append(
                f"| `{variable.name}` | {data_type} | {'yes' if variable.is_collection else 'no'} "
                f"| {'yes' if variable.is_input else 'no'} | {'yes' if variable.is_output else 'no'} "
                f"| {default} | {variable.description or ''} |"
            )
        lines.append("")

    return "\n".join(lines)
