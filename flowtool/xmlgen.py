"""
IR -> Flow XML.

Deterministic, no LLM involved. Child element ordering follows what Salesforce
itself emits when you retrieve a Flow, because the Metadata API is picky about
it in places.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from typing import Dict, List, Optional, Tuple
from xml.dom import minidom

from .ir import (
    ActionCall,
    Assignment,
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

METADATA_NS = "http://soap.sforce.com/2006/04/metadata"

# Grid spacing that roughly matches what Flow Builder produces.
_X_STEP = 220
_Y_STEP = 150
_X_ORIGIN = 176
_Y_ORIGIN = 0


def _sub(parent: ET.Element, tag: str, text: Optional[str] = None) -> ET.Element:
    el = ET.SubElement(parent, tag)
    if text is not None:
        el.text = text
    return el


def _bool(value: bool) -> str:
    return "true" if value else "false"


def _number(value: float) -> str:
    # Salesforce accepts both, but integers read better in diffs.
    return str(int(value)) if float(value).is_integer() else repr(value)


def _value_el(parent: ET.Element, tag: str, value: Value) -> None:
    holder = _sub(parent, tag)
    if value.string_value is not None:
        _sub(holder, "stringValue", value.string_value)
    elif value.number_value is not None:
        _sub(holder, "numberValue", _number(value.number_value))
    elif value.boolean_value is not None:
        _sub(holder, "booleanValue", _bool(value.boolean_value))
    elif value.date_value is not None:
        _sub(holder, "dateValue", value.date_value)
    elif value.date_time_value is not None:
        _sub(holder, "dateTimeValue", value.date_time_value)
    elif value.element_reference is not None:
        _sub(holder, "elementReference", value.element_reference)


def _connector(parent: ET.Element, tag: str, target: Optional[str]) -> None:
    """A path that ends emits no connector at all — that is how Flow XML says 'End'."""
    if target is None:
        return
    conn = _sub(parent, tag)
    _sub(conn, "targetReference", target)


def _filters(parent: ET.Element, filters: List[RecordFilter]) -> None:
    for f in filters:
        node = _sub(parent, "filters")
        _sub(node, "field", f.field)
        _sub(node, "operator", f.operator)
        if f.value is not None:
            _value_el(node, "value", f.value)


# --------------------------------------------------------------------------
# Layout
# --------------------------------------------------------------------------


def compute_layout(flow: Flow) -> Dict[str, Tuple[int, int]]:
    """
    Breadth-first placement so the flow is actually readable in Flow Builder.
    Not as pretty as hand-placement, but every element gets distinct coordinates
    instead of everything piling up at (0, 0).
    """
    by_name = flow.by_name()
    coords: Dict[str, Tuple[int, int]] = {}
    depth: Dict[str, int] = {}

    successors = flow.successors  # shared with the IR's reachability check

    queue: List[Tuple[str, int]] = []
    if flow.start.next:
        queue.append((flow.start.next, 1))

    while queue:
        name, level = queue.pop(0)
        if name in depth:
            continue
        depth[name] = level
        el = by_name.get(name)
        if el is None:
            continue
        for nxt in successors(el):
            if nxt not in depth:
                queue.append((nxt, level + 1))

    # Anything unreachable still needs coordinates.
    max_level = max(depth.values(), default=0)
    for el in flow.elements:
        if el.name not in depth:
            max_level += 1
            depth[el.name] = max_level

    per_level: Dict[int, int] = {}
    for el in flow.elements:
        level = depth[el.name]
        column = per_level.get(level, 0)
        per_level[level] = column + 1
        coords[el.name] = (_X_ORIGIN + column * _X_STEP, _Y_ORIGIN + level * _Y_STEP)

    return coords


# --------------------------------------------------------------------------
# Element writers
# --------------------------------------------------------------------------


def _write_common(node: ET.Element, el: Element, xy: Tuple[int, int]) -> None:
    _sub(node, "name", el.name)
    _sub(node, "label", el.label)
    _sub(node, "locationX", str(xy[0]))
    _sub(node, "locationY", str(xy[1]))
    if el.description:
        _sub(node, "description", el.description)


def _fault(node: ET.Element, el: Element) -> None:
    """Every fault-capable element writes its fault path the same way."""
    _connector(node, "faultConnector", getattr(el, "fault_next", None))


def _write_assignment(root: ET.Element, el: Assignment, xy) -> None:
    node = _sub(root, "assignments")
    _write_common(node, el, xy)
    for item in el.items:
        ai = _sub(node, "assignmentItems")
        _sub(ai, "assignToReference", item.to_reference)
        _sub(ai, "operator", item.operator)
        _value_el(ai, "value", item.value)
    _connector(node, "connector", el.next)


def _write_decision(root: ET.Element, el: Decision, xy) -> None:
    node = _sub(root, "decisions")
    _write_common(node, el, xy)
    _connector(node, "defaultConnector", el.next)
    _sub(node, "defaultConnectorLabel", el.default_outcome_label)
    for oc in el.outcomes:
        rule = _sub(node, "rules")
        _sub(rule, "name", oc.name)
        _sub(rule, "conditionLogic", oc.condition_logic)
        for cond in oc.conditions:
            c = _sub(rule, "conditions")
            _sub(c, "leftValueReference", cond.left)
            _sub(c, "operator", cond.operator)
            if cond.right is not None:
                _value_el(c, "rightValue", cond.right)
        _connector(rule, "connector", oc.next)
        _sub(rule, "label", oc.label)


def _write_get_records(root: ET.Element, el: GetRecords, xy) -> None:
    node = _sub(root, "recordLookups")
    _write_common(node, el, xy)
    _sub(node, "assignNullValuesIfNoRecordsFound", "false")
    _connector(node, "connector", el.next)
    _fault(node, el)
    if el.filters:
        _sub(node, "filterLogic", el.filter_logic)
        _filters(node, el.filters)
    _sub(node, "getFirstRecordOnly", _bool(el.first_record_only))
    _sub(node, "object", el.object)
    if el.sort_field:
        _sub(node, "sortField", el.sort_field)
        _sub(node, "sortOrder", "Asc" if el.sort_order != "Desc" else "Desc")
    _sub(node, "storeOutputAutomatically", _bool(el.store_output_automatically))


def _write_record_create(root: ET.Element, el: RecordCreate, xy) -> None:
    node = _sub(root, "recordCreates")
    _write_common(node, el, xy)
    _connector(node, "connector", el.next)
    _fault(node, el)
    if el.input_reference:
        _sub(node, "inputReference", el.input_reference)
    else:
        for assignment in el.fields:
            ia = _sub(node, "inputAssignments")
            _sub(ia, "field", assignment.field)
            _value_el(ia, "value", assignment.value)
        _sub(node, "object", el.object)
        _sub(node, "storeOutputAutomatically", "true")


def _write_record_update(root: ET.Element, el: RecordUpdate, xy) -> None:
    node = _sub(root, "recordUpdates")
    _write_common(node, el, xy)
    _connector(node, "connector", el.next)
    _fault(node, el)
    if el.filters:
        _sub(node, "filterLogic", el.filter_logic)
        _filters(node, el.filters)
    for assignment in el.fields:
        ia = _sub(node, "inputAssignments")
        _sub(ia, "field", assignment.field)
        _value_el(ia, "value", assignment.value)
    if el.input_reference:
        _sub(node, "inputReference", el.input_reference)
    if el.object:
        _sub(node, "object", el.object)


def _write_record_delete(root: ET.Element, el: RecordDelete, xy) -> None:
    node = _sub(root, "recordDeletes")
    _write_common(node, el, xy)
    _connector(node, "connector", el.next)
    _fault(node, el)
    if el.filters:
        _filters(node, el.filters)
    if el.input_reference:
        _sub(node, "inputReference", el.input_reference)
    if el.object:
        _sub(node, "object", el.object)


def _write_loop(root: ET.Element, el: Loop, xy) -> None:
    node = _sub(root, "loops")
    _write_common(node, el, xy)
    _sub(node, "collectionReference", el.collection_reference)
    _sub(node, "iterationOrder", el.iteration_order)
    _connector(node, "nextValueConnector", el.first_element)
    _connector(node, "noMoreValuesConnector", el.next)


def _write_action_call(root: ET.Element, el: ActionCall, xy) -> None:
    node = _sub(root, "actionCalls")
    _write_common(node, el, xy)
    _sub(node, "actionName", el.action_name)
    _sub(node, "actionType", el.action_type)
    _connector(node, "connector", el.next)
    _fault(node, el)
    for parameter in el.input_parameters:
        ip = _sub(node, "inputParameters")
        _sub(ip, "name", parameter.name)
        _value_el(ip, "value", parameter.value)
    if el.store_output_automatically:
        _sub(node, "storeOutputAutomatically", "true")


def _write_subflow(root: ET.Element, el: Subflow, xy) -> None:
    node = _sub(root, "subflows")
    _write_common(node, el, xy)
    _connector(node, "connector", el.next)
    _fault(node, el)
    _sub(node, "flowName", el.flow_name)
    for assignment in el.input_assignments:
        ia = _sub(node, "inputAssignments")
        _sub(ia, "name", assignment.name)
        _value_el(ia, "value", assignment.value)


_WRITERS = {
    ActionCall: ("actionCalls", _write_action_call),
    Assignment: ("assignments", _write_assignment),
    Decision: ("decisions", _write_decision),
    Loop: ("loops", _write_loop),
    RecordCreate: ("recordCreates", _write_record_create),
    RecordDelete: ("recordDeletes", _write_record_delete),
    GetRecords: ("recordLookups", _write_get_records),
    RecordUpdate: ("recordUpdates", _write_record_update),
    Subflow: ("subflows", _write_subflow),
}

# The order Salesforce emits Flow children in.
_ROOT_ORDER = [
    "actionCalls",
    "assignments",
    "decisions",
    "loops",
    "recordCreates",
    "recordDeletes",
    "recordLookups",
    "recordUpdates",
    "subflows",
]


def generate(flow: Flow) -> str:
    """Render a validated Flow IR as deployable Flow metadata XML."""
    ET.register_namespace("", METADATA_NS)
    root = ET.Element(f"{{{METADATA_NS}}}Flow")

    _sub(root, "apiVersion", flow.api_version)

    coords = compute_layout(flow)

    # Group elements by their XML tag so same-tag elements stay contiguous.
    buckets: Dict[str, List[Element]] = {tag: [] for tag in _ROOT_ORDER}
    for el in flow.elements:
        tag, _ = _WRITERS[type(el)]
        buckets[tag].append(el)

    for tag in _ROOT_ORDER:
        for el in buckets[tag]:
            _, writer = _WRITERS[type(el)]
            writer(root, el, coords[el.name])

    if flow.description:
        _sub(root, "description", flow.description)
    _sub(root, "environments", "Default")
    _sub(root, "interviewLabel", flow.interview_label)
    _sub(root, "label", flow.label)
    _sub(root, "processType", flow.process_type)

    start = _sub(root, "start")
    _sub(start, "locationX", str(_X_ORIGIN))
    _sub(start, "locationY", "0")
    _connector(start, "connector", flow.start.next)
    if flow.start.filters:
        _sub(start, "filterLogic", flow.start.filter_logic)
        _filters(start, flow.start.filters)
    if flow.start.object:
        _sub(start, "object", flow.start.object)
    if flow.start.only_when_changed_to_meet_criteria:
        _sub(start, "doesRequireRecordChangedToMeetCriteria", "true")
    if flow.start.record_trigger_type:
        _sub(start, "recordTriggerType", flow.start.record_trigger_type)
    if flow.start.trigger_type:
        _sub(start, "triggerType", flow.start.trigger_type)

    _sub(root, "status", flow.status)

    for var in flow.variables:
        node = _sub(root, "variables")
        _sub(node, "name", var.name)
        _sub(node, "dataType", var.data_type)
        _sub(node, "isCollection", _bool(var.is_collection))
        _sub(node, "isInput", _bool(var.is_input))
        _sub(node, "isOutput", _bool(var.is_output))
        if var.object_type:
            _sub(node, "objectType", var.object_type)

    raw = ET.tostring(root, encoding="unicode")
    pretty = minidom.parseString(raw).toprettyxml(indent="    ")
    # minidom emits its own declaration with double quotes; normalise to SF style.
    body = "\n".join(line for line in pretty.split("\n")[1:] if line.strip())
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + body + "\n"
