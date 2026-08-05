"""
Flow XML -> IR. The reverse of xmlgen.

A real org contains flows this IR cannot represent - screens, waits, Apex
actions, formulas. Parsing one of those and quietly skipping what it does not
understand would produce a diagram describing a different flow than the one in
the org, and an edit round-trip would then delete the skipped parts on deploy.

So anything unrecognised raises UnsupportedFlow naming what it was. A flow this
module returns is one it fully understands.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from typing import Dict, List, Optional

from .ir import (
    ActionCall,
    Assignment,
    AssignmentItem,
    Condition,
    Decision,
    Element,
    FieldValue,
    Flow,
    GetRecords,
    InputAssignment,
    Loop,
    Outcome,
    RecordCreate,
    RecordDelete,
    RecordFilter,
    RecordUpdate,
    Start,
    Subflow,
    Value,
    Variable,
)
from .xmlgen import METADATA_NS

NS = {"m": METADATA_NS}


class UnsupportedFlow(ValueError):
    """The flow uses constructs this IR cannot represent."""

    def __init__(self, reasons: List[str], api_name: str = ""):
        self.reasons = reasons
        self.api_name = api_name
        subject = f"{api_name} " if api_name else ""
        super().__init__(
            f"{subject}uses Flow features FlowForge cannot represent yet:\n"
            + "\n".join(f"  - {reason}" for reason in reasons)
        )


# Tags that carry no logic and can be ignored without changing behaviour.
_IGNORED = {
    "apiVersion", "description", "environments", "interviewLabel", "label",
    "processType", "start", "status", "variables", "processMetadataValues",
    "isTemplate", "sourceTemplate", "runInMode", "segment", "fullName",
    "areMetricsLoggedToDataCloud", "isOverridable", "overriddenFlow",
    "migratedFromWorkflowRuleName", "triggerOrder", "timeZoneSidKey",
}

# Element collections this module can turn into IR.
_SUPPORTED_ELEMENTS = {
    "actionCalls", "assignments", "decisions", "loops", "recordCreates", "recordDeletes",
    "recordLookups", "recordUpdates", "subflows",
}

# Recognised Flow constructs the IR has no equivalent for. Named individually so
# the message tells the user what is actually in their flow.
_KNOWN_UNSUPPORTED = {
    "screens": "screen elements",
    "waits": "wait / pause elements",
    "apexPluginCalls": "Apex plugin calls",
    "recordRollbacks": "rollback elements",
    "steps": "steps",
    "orchestratedStages": "orchestration stages",
    "stages": "stages",
    "collectionProcessors": "collection filter/sort elements",
    "transforms": "transform elements",
    "customErrors": "custom error elements",
    "formulas": "formula resources",
    "constants": "constant resources",
    "textTemplates": "text templates",
    "dynamicChoiceSets": "dynamic choice sets",
    "choices": "choices",
    "scheduledPaths": "scheduled paths",
    "exitRules": "exit rules",
    "filters": "top-level filters",
}


def _text(node: Optional[ET.Element], path: str) -> Optional[str]:
    if node is None:
        return None
    found = node.find(path, NS)
    return found.text if found is not None else None


def _bool(node: Optional[ET.Element], path: str, default: bool = False) -> bool:
    raw = _text(node, path)
    return default if raw is None else raw.strip().lower() == "true"


def _target(node: ET.Element, tag: str) -> Optional[str]:
    """A connector's target, or None when the path simply ends."""
    return _text(node, f"m:{tag}/m:targetReference")


def _value(node: Optional[ET.Element]) -> Optional[Value]:
    if node is None:
        return None
    for tag, field in (
        ("stringValue", "string_value"),
        ("numberValue", "number_value"),
        ("booleanValue", "boolean_value"),
        ("dateValue", "date_value"),
        ("dateTimeValue", "date_time_value"),
        ("elementReference", "element_reference"),
    ):
        raw = _text(node, f"m:{tag}")
        if raw is None:
            continue
        if field == "number_value":
            return Value(number_value=float(raw))
        if field == "boolean_value":
            return Value(boolean_value=raw.strip().lower() == "true")
        return Value(**{field: raw})
    return None


def _filters(node: ET.Element) -> List[RecordFilter]:
    out = []
    for item in node.findall("m:filters", NS):
        out.append(
            RecordFilter(
                field=_text(item, "m:field") or "",
                operator=_text(item, "m:operator") or "EqualTo",
                value=_value(item.find("m:value", NS)),
            )
        )
    return out


def _field_values(node: ET.Element) -> List[FieldValue]:
    out = []
    for item in node.findall("m:inputAssignments", NS):
        value = _value(item.find("m:value", NS))
        if value is None:
            continue
        out.append(FieldValue(field=_text(item, "m:field") or "", value=value))
    return out


def _common(node: ET.Element) -> Dict[str, Optional[str]]:
    return {
        "name": _text(node, "m:name") or "",
        "label": _text(node, "m:label") or _text(node, "m:name") or "",
        "next": _target(node, "connector"),
    }


# --------------------------------------------------------------------------
# Element readers
# --------------------------------------------------------------------------


def _read_assignment(node: ET.Element) -> Assignment:
    items = []
    for item in node.findall("m:assignmentItems", NS):
        value = _value(item.find("m:value", NS))
        if value is None:
            continue
        items.append(
            AssignmentItem(
                to_reference=_text(item, "m:assignToReference") or "",
                operator=_text(item, "m:operator") or "Assign",
                value=value,
            )
        )
    return Assignment(**_common(node), items=items)


def _read_decision(node: ET.Element) -> Decision:
    outcomes = []
    for rule in node.findall("m:rules", NS):
        conditions = []
        for condition in rule.findall("m:conditions", NS):
            conditions.append(
                Condition(
                    left=_text(condition, "m:leftValueReference") or "",
                    operator=_text(condition, "m:operator") or "EqualTo",
                    right=_value(condition.find("m:rightValue", NS)),
                )
            )
        outcomes.append(
            Outcome(
                name=_text(rule, "m:name") or "",
                label=_text(rule, "m:label") or _text(rule, "m:name") or "",
                conditions=conditions,
                condition_logic=_text(rule, "m:conditionLogic") or "and",
                next=_target(rule, "connector"),
            )
        )
    return Decision(
        name=_text(node, "m:name") or "",
        label=_text(node, "m:label") or "",
        next=_target(node, "defaultConnector"),
        outcomes=outcomes,
        default_outcome_label=_text(node, "m:defaultConnectorLabel") or "Default",
    )


def _read_get_records(node: ET.Element) -> GetRecords:
    return GetRecords(
        **_common(node),
        object=_text(node, "m:object") or "",
        filters=_filters(node),
        filter_logic=_text(node, "m:filterLogic") or "and",
        first_record_only=_bool(node, "m:getFirstRecordOnly", True),
        store_output_automatically=_bool(node, "m:storeOutputAutomatically", True),
        sort_field=_text(node, "m:sortField"),
        sort_order=_text(node, "m:sortOrder"),
    )


def _read_record_create(node: ET.Element) -> RecordCreate:
    return RecordCreate(
        **_common(node),
        object=_text(node, "m:object"),
        fields=_field_values(node),
        input_reference=_text(node, "m:inputReference"),
    )


def _read_record_update(node: ET.Element) -> RecordUpdate:
    return RecordUpdate(
        **_common(node),
        object=_text(node, "m:object"),
        filters=_filters(node),
        filter_logic=_text(node, "m:filterLogic") or "and",
        fields=_field_values(node),
        input_reference=_text(node, "m:inputReference"),
    )


def _read_record_delete(node: ET.Element) -> RecordDelete:
    return RecordDelete(
        **_common(node),
        object=_text(node, "m:object"),
        filters=_filters(node),
        input_reference=_text(node, "m:inputReference"),
    )


def _read_loop(node: ET.Element) -> Loop:
    return Loop(
        name=_text(node, "m:name") or "",
        label=_text(node, "m:label") or "",
        collection_reference=_text(node, "m:collectionReference") or "",
        iteration_order=_text(node, "m:iterationOrder") or "Asc",
        first_element=_target(node, "nextValueConnector"),
        next=_target(node, "noMoreValuesConnector"),
    )


def _read_action_call(node: ET.Element) -> ActionCall:
    parameters = []
    for item in node.findall("m:inputParameters", NS):
        value = _value(item.find("m:value", NS))
        if value is None:
            continue
        parameters.append(InputAssignment(name=_text(item, "m:name") or "", value=value))
    return ActionCall(
        **_common(node),
        action_name=_text(node, "m:actionName") or "",
        action_type=_text(node, "m:actionType") or "",
        input_parameters=parameters,
        store_output_automatically=_bool(node, "m:storeOutputAutomatically"),
        fault_next=_target(node, "faultConnector"),
    )


def _read_subflow(node: ET.Element) -> Subflow:
    inputs = []
    for item in node.findall("m:inputAssignments", NS):
        value = _value(item.find("m:value", NS))
        if value is None:
            continue
        inputs.append(InputAssignment(name=_text(item, "m:name") or "", value=value))
    return Subflow(
        **_common(node),
        flow_name=_text(node, "m:flowName") or "",
        input_assignments=inputs,
    )


_READERS = {
    "actionCalls": _read_action_call,
    "assignments": _read_assignment,
    "decisions": _read_decision,
    "loops": _read_loop,
    "recordCreates": _read_record_create,
    "recordDeletes": _read_record_delete,
    "recordLookups": _read_get_records,
    "recordUpdates": _read_record_update,
    "subflows": _read_subflow,
}


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------


def parse_flow(xml: str, api_name: str = "") -> Flow:
    """
    Turn Flow metadata XML into IR, or raise UnsupportedFlow explaining why not.
    """
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as exc:
        raise UnsupportedFlow([f"the XML did not parse: {exc}"], api_name) from exc

    reasons: List[str] = []

    process_type = _text(root, "m:processType")
    if process_type and process_type != "AutoLaunchedFlow":
        reasons.append(
            f"process type {process_type} (only record-triggered and "
            "autolaunched flows are supported)"
        )

    seen_unsupported = set()
    for child in root:
        tag = child.tag.split("}")[-1]
        if tag in _IGNORED or tag in _SUPPORTED_ELEMENTS:
            continue
        if tag in seen_unsupported:
            continue
        seen_unsupported.add(tag)
        reasons.append(_KNOWN_UNSUPPORTED.get(tag, f"unrecognised element <{tag}>"))

    if reasons:
        raise UnsupportedFlow(reasons, api_name or _text(root, "m:label") or "")

    elements: List[Element] = []
    for tag, reader in _READERS.items():
        for node in root.findall(f"m:{tag}", NS):
            try:
                elements.append(reader(node))
            except ValueError as exc:
                # The flow is deployed and running, so a rejection here means
                # the IR is stricter than Salesforce, not that the flow is
                # broken. Report it as a gap rather than a 500.
                name = _text(node, "m:name") or tag
                reasons.append(f"{name} does not fit FlowForge's model: {exc}")

    if reasons:
        raise UnsupportedFlow(reasons, api_name or _text(root, "m:label") or "")

    start_node = root.find("m:start", NS)
    start = Start(
        next=_target(start_node, "connector") if start_node is not None else None,
        object=_text(start_node, "m:object"),
        record_trigger_type=_text(start_node, "m:recordTriggerType"),
        trigger_type=_text(start_node, "m:triggerType"),
        filters=_filters(start_node) if start_node is not None else [],
        filter_logic=_text(start_node, "m:filterLogic") or "and",
    )

    variables = []
    for node in root.findall("m:variables", NS):
        variables.append(
            Variable(
                name=_text(node, "m:name") or "",
                data_type=_text(node, "m:dataType") or "String",
                is_collection=_bool(node, "m:isCollection"),
                is_input=_bool(node, "m:isInput"),
                is_output=_bool(node, "m:isOutput"),
                object_type=_text(node, "m:objectType"),
            )
        )

    label = _text(root, "m:label") or api_name or "Untitled"
    try:
        return Flow(
            api_name=api_name or _text(root, "m:fullName") or _slug(label),
            label=label,
            description=_text(root, "m:description"),
            api_version=_text(root, "m:apiVersion") or "62.0",
            status=_text(root, "m:status") or "Draft",
            start=start,
            elements=elements,
            variables=variables,
        )
    except ValueError as exc:
        # The flow deployed, so this means the IR is stricter than Salesforce.
        # Say so plainly rather than pretending the flow is malformed.
        raise UnsupportedFlow(
            [f"it does not fit FlowForge's model: {exc}"], api_name or label
        ) from exc


def _slug(label: str) -> str:
    parts = [part for part in "".join(
        char if char.isalnum() else " " for char in label
    ).split() if part]
    return "_".join(parts) or "Flow"
