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
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence

from .ir import (
    ActionCall,
    Assignment,
    AssignmentItem,
    Choice,
    ComponentOutput,
    Condition,
    Constant,
    Decision,
    DynamicChoiceSet,
    Element,
    FieldValue,
    Flow,
    Formula,
    GetRecords,
    InputAssignment,
    Loop,
    Outcome,
    RecordCreate,
    RecordDelete,
    RecordFilter,
    RecordUpdate,
    Screen,
    ScreenField,
    Start,
    Subflow,
    TextTemplate,
    Value,
    Variable,
)
from .xmlgen import METADATA_NS

NS = {"m": METADATA_NS}


# Refused by decision, not by gap. These will not be supported, so a survey
# should keep counting them - the count is the evidence for the decision - but
# never recommend building them. Without this the report kept naming migrated
# Workflow Rules as the biggest available win, survey after survey.
OUT_OF_SCOPE = frozenset({"process_type:Workflow"})


@dataclass(frozen=True)
class Gap:
    """
    One thing this build cannot represent.

    `code` is stable and groupable — it is what a survey across an org counts.
    `detail` is the sentence a person reads and names the element it came from,
    so the two are deliberately different: "child:queriedFields" groups, while
    "Get_Contacts uses a hand-picked field list" explains.
    """

    code: str
    detail: str


class UnsupportedFlow(ValueError):
    """The flow uses constructs this IR cannot represent."""

    def __init__(self, gaps: Sequence, api_name: str = ""):
        self.gaps: List[Gap] = [
            gap if isinstance(gap, Gap) else Gap("other", str(gap)) for gap in gaps
        ]
        self.api_name = api_name
        subject = f"{api_name} " if api_name else ""
        super().__init__(
            f"{subject}uses Flow features SFDC Flow Tool cannot represent yet:\n"
            + "\n".join(f"  - {gap.detail}" for gap in self.gaps)
        )

    @property
    def reasons(self) -> List[str]:
        return [gap.detail for gap in self.gaps]

    @property
    def codes(self) -> List[str]:
        return [gap.code for gap in self.gaps]


# Tags that carry no logic and can be ignored without changing behaviour.
_IGNORED = {
    "apiVersion", "description", "environments", "interviewLabel", "label",
    "processType", "start", "status", "variables", "processMetadataValues",
    "isTemplate", "sourceTemplate", "runInMode", "segment", "fullName",
    "areMetricsLoggedToDataCloud", "isOverridable", "overriddenFlow",
    "migratedFromWorkflowRuleName", "triggerOrder", "timeZoneSidKey",
    # Older flows name their first element at the root instead of on the
    # start connector. Same meaning, read below.
    "startElementReference",
}

# Element collections this module can turn into IR.
_SUPPORTED_ELEMENTS = {
    "actionCalls", "assignments", "decisions", "loops", "recordCreates", "recordDeletes",
    "recordLookups", "recordUpdates", "screens", "subflows",
}

# Resources rather than elements: nothing connects to them, screen fields name
# them. Read alongside the elements, checked with their own allowlists below.
_SUPPORTED_RESOURCES = {
    "choices", "dynamicChoiceSets", "textTemplates", "constants", "formulas",
}

# Recognised Flow constructs the IR has no equivalent for. Named individually so
# the message tells the user what is actually in their flow.
_KNOWN_UNSUPPORTED = {
    "waits": "wait / pause elements",
    "apexPluginCalls": "Apex plugin calls",
    "recordRollbacks": "rollback elements",
    "steps": "steps",
    "orchestratedStages": "orchestration stages",
    "stages": "stages",
    "collectionProcessors": "collection filter/sort elements",
    "transforms": "transform elements",
    "customErrors": "custom error elements",
    "scheduledPaths": "scheduled paths",
    "exitRules": "exit rules",
    "filters": "top-level filters",
}


# --------------------------------------------------------------------------
# What each element is allowed to contain
# --------------------------------------------------------------------------
#
# Checking only the root let anything nested slip through unnoticed: a
# faultConnector on a Get Records, a scheduled path on the start, an output
# parameter on an action. Each was read as if it were not there, drawn as if it
# were not there, and deleted on the next deploy. Every level is checked now.

# Present on every element and carrying no logic of its own.
_ELEMENT_COMMON = {
    "name", "label", "locationX", "locationY", "description", "connector",
    "processMetadataValues",
}
_FAULT = {"faultConnector"}

_ELEMENT_CHILDREN = {
    "assignments": _ELEMENT_COMMON | {"assignmentItems"},
    "decisions": _ELEMENT_COMMON | {
        "rules", "defaultConnector", "defaultConnectorLabel",
    },
    "loops": _ELEMENT_COMMON | {
        "collectionReference", "iterationOrder", "nextValueConnector",
        "noMoreValuesConnector",
    },
    "recordLookups": _ELEMENT_COMMON | _FAULT | {
        "object", "filters", "filterLogic", "getFirstRecordOnly",
        "storeOutputAutomatically", "assignNullValuesIfNoRecordsFound",
        "sortField", "sortOrder", "queriedFields", "outputReference",
    },
    "recordCreates": _ELEMENT_COMMON | _FAULT | {
        "object", "inputAssignments", "inputReference", "storeOutputAutomatically",
        "assignRecordIdToReference",
    },
    "recordUpdates": _ELEMENT_COMMON | _FAULT | {
        "object", "filters", "filterLogic", "inputAssignments", "inputReference",
    },
    "recordDeletes": _ELEMENT_COMMON | _FAULT | {
        "object", "filters", "filterLogic", "inputReference",
    },
    "subflows": _ELEMENT_COMMON | _FAULT | {"flowName", "inputAssignments"},
    "actionCalls": _ELEMENT_COMMON | _FAULT | {
        "actionName", "actionType", "inputParameters", "storeOutputAutomatically",
    },
    # No faultConnector: a screen cannot fail the way a DML element can.
    "screens": _ELEMENT_COMMON | {
        "fields", "allowBack", "allowFinish", "allowPause", "showFooter", "showHeader",
    },
}

# Two levels below the root, and the reason this pass exists at all: a screen
# whose field list was read but whose choices, components or visibility rules
# were not would draw as a plain input box and lose them on the next deploy.
_SCREEN_FIELD_CHILDREN = {
    "name", "dataType", "fieldText", "fieldType", "isRequired", "choiceReferences",
    "defaultValue", "defaultSelectedChoiceReference", "scale",
    # A component and the four things that travel with it.
    "extensionName", "inputParameters", "outputParameters",
    "storeOutputAutomatically", "inputsOnNextNavToAssocScrn",
}

# Everything else on a screen - region containers, and the field types that
# arrange other fields - is a later stage. Refused by name so a survey can count
# which is worth building.
_SUPPORTED_SCREEN_FIELD_TYPES = {
    "DisplayText", "InputField", "LargeTextArea",
    "RadioButtons", "DropdownBox", "MultiSelectCheckboxes", "MultiSelectPicklist",
    "ComponentInstance",
}

_CHOICE_CHILDREN = {"name", "choiceText", "dataType", "value", "processMetadataValues"}

_TEXT_TEMPLATE_CHILDREN = {"name", "text", "isViewedAsPlainText", "description",
                          "processMetadataValues"}

_CONSTANT_CHILDREN = {"name", "dataType", "value", "description",
                     "processMetadataValues"}

# processMetadataValues carries the pre-migration formula text on flows that
# came from Workflow. It is commentary, not behaviour.
_FORMULA_CHILDREN = {"name", "dataType", "expression", "scale", "description",
                    "processMetadataValues"}

_CHOICE_SET_CHILDREN = {
    "name", "dataType", "displayField", "valueField", "object", "filters",
    "filterLogic", "limit", "sortField", "sortOrder",
    "picklistField", "picklistObject", "processMetadataValues",
}

_START_CHILDREN = {
    "locationX", "locationY", "connector", "object", "recordTriggerType",
    "triggerType", "filters", "filterLogic",
    "doesRequireRecordChangedToMeetCriteria",
}

_VARIABLE_CHILDREN = {
    "name", "dataType", "isCollection", "isInput", "isOutput", "objectType",
    "description", "scale", "value",
}

# Plain-language names for the nested things we know about but cannot hold, so
# the refusal says what the flow actually uses rather than naming a tag.
_CHILD_MEANING = {
    "outputReference": "manual output storage",
    "outputAssignments": "manually assigned outputs",
    "outputParameters": "an action's output parameters",
    "assignNextValueToReference": "its own loop variable",
    "limit": "a record limit",
    "scheduledPaths": "scheduled paths",
    "schedule": "a schedule",
    "filterFormula": "a formula-based entry condition",
    "flowTransactionModel": "an explicit transaction model",
    "value": "a default value",
    "scale": "a decimal scale",
    "apexClass": "an Apex type",
    "elementSubtype": "an element subtype this build does not model",
    "storeOutputAutomatically": "automatic output storage",
    # On a screen field.
    "extensionName": "a custom LWC or Aura component",
    "inputParameters": "component input parameters",
    "visibilityRule": "conditional visibility",
    "validationRule": "a validation rule",
    "helpText": "help text",
    "fields": "nested sections or columns",
    "objectFieldReference": "a bound record field",
    "inputsOnNextNavToAssocScrn": "revisit behaviour",
    "userInput": "a choice that lets the user type their own answer",
    "collectionReference": "options built from a collection",
    # On a screen.
    "pausedText": "custom pause text",
    "nextOrFinishButtonLabel": "a custom button label",
    "backButtonLabel": "a custom button label",
    "pauseButtonLabel": "a custom button label",
}


def _unknown_children(node: ET.Element, allowed: set, where: str) -> List[Gap]:
    """Every child tag of `node` that this module would otherwise ignore."""
    reasons = []
    seen = set()
    for child in node:
        tag = child.tag.split("}")[-1]
        if tag in allowed or tag in seen:
            continue
        seen.add(tag)
        meaning = _CHILD_MEANING.get(tag, f"<{tag}>")
        reasons.append(Gap(f"child:{tag}", f"{where} uses {meaning}"))
    return reasons


def _text(node: Optional[ET.Element], path: str) -> Optional[str]:
    if node is None:
        return None
    found = node.find(path, NS)
    return found.text if found is not None else None


def _bool(node: Optional[ET.Element], path: str, default: bool = False) -> bool:
    raw = _text(node, path)
    return default if raw is None else raw.strip().lower() == "true"


def _opt_bool(node: Optional[ET.Element], path: str) -> Optional[bool]:
    """None when the flow never said, which is not the same as saying false."""
    raw = _text(node, path)
    return None if raw is None else raw.strip().lower() == "true"


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
        "description": _text(node, "m:description"),
        "next": _target(node, "connector"),
    }


def _fault_common(node: ET.Element) -> Dict[str, Optional[str]]:
    return {**_common(node), "fault_next": _target(node, "faultConnector")}


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
        **_fault_common(node),
        object=_text(node, "m:object") or "",
        queried_fields=[
            (f.text or "") for f in node.findall("m:queriedFields", NS)
        ],
        filters=_filters(node),
        filter_logic=_text(node, "m:filterLogic") or "and",
        first_record_only=_opt_bool(node, "m:getFirstRecordOnly"),
        # Absent means off. Flows that store into a variable omit it entirely.
        store_output_automatically=_bool(node, "m:storeOutputAutomatically"),
        output_reference=_text(node, "m:outputReference"),
        sort_field=_text(node, "m:sortField"),
        sort_order=_text(node, "m:sortOrder"),
    )


def _read_record_create(node: ET.Element) -> RecordCreate:
    return RecordCreate(
        **_fault_common(node),
        object=_text(node, "m:object"),
        fields=_field_values(node),
        input_reference=_text(node, "m:inputReference"),
        assign_record_id_to_reference=_text(node, "m:assignRecordIdToReference"),
        # Absent means off. It was allowed through the allowlist but never read,
        # so a Create with automatic storage switched off came back with it on.
        store_output_automatically=_bool(node, "m:storeOutputAutomatically"),
    )


def _read_record_update(node: ET.Element) -> RecordUpdate:
    return RecordUpdate(
        **_fault_common(node),
        object=_text(node, "m:object"),
        filters=_filters(node),
        filter_logic=_text(node, "m:filterLogic") or "and",
        fields=_field_values(node),
        input_reference=_text(node, "m:inputReference"),
    )


def _read_record_delete(node: ET.Element) -> RecordDelete:
    return RecordDelete(
        **_fault_common(node),
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
        **_fault_common(node),
        action_name=_text(node, "m:actionName") or "",
        action_type=_text(node, "m:actionType") or "",
        input_parameters=parameters,
        store_output_automatically=_bool(node, "m:storeOutputAutomatically"),
    )


def _read_choice(node: ET.Element) -> Choice:
    return Choice(
        name=_text(node, "m:name") or "",
        choice_text=_text(node, "m:choiceText") or "",
        data_type=_text(node, "m:dataType") or "String",
        value=_value(node.find("m:value", NS)),
    )


def _read_choice_set(node: ET.Element) -> DynamicChoiceSet:
    limit = _text(node, "m:limit")
    return DynamicChoiceSet(
        name=_text(node, "m:name") or "",
        data_type=_text(node, "m:dataType") or "String",
        object=_text(node, "m:object"),
        display_field=_text(node, "m:displayField"),
        value_field=_text(node, "m:valueField"),
        filters=_filters(node),
        filter_logic=_text(node, "m:filterLogic") or "and",
        sort_field=_text(node, "m:sortField"),
        sort_order=_text(node, "m:sortOrder"),
        limit=int(limit) if limit else None,
        picklist_object=_text(node, "m:picklistObject"),
        picklist_field=_text(node, "m:picklistField"),
    )


def _read_component_inputs(item: ET.Element) -> List[InputAssignment]:
    inputs = []
    for parameter in item.findall("m:inputParameters", NS):
        value = _value(parameter.find("m:value", NS))
        if value is None:
            continue
        inputs.append(
            InputAssignment(name=_text(parameter, "m:name") or "", value=value)
        )
    return inputs


def _read_component_outputs(item: ET.Element) -> List[ComponentOutput]:
    return [
        ComponentOutput(
            name=_text(parameter, "m:name") or "",
            assign_to_reference=_text(parameter, "m:assignToReference") or "",
        )
        for parameter in item.findall("m:outputParameters", NS)
    ]


def _read_screen(node: ET.Element) -> Screen:
    fields = []
    for item in node.findall("m:fields", NS):
        fields.append(
            ScreenField(
                name=_text(item, "m:name") or "",
                field_type=_text(item, "m:fieldType") or "DisplayText",
                # None, not "": a ComponentInstance has no fieldText at all, and
                # an empty string would be written back as an empty tag.
                field_text=_text(item, "m:fieldText"),
                data_type=_text(item, "m:dataType"),
                extension_name=_text(item, "m:extensionName"),
                input_parameters=_read_component_inputs(item),
                output_parameters=_read_component_outputs(item),
                store_output_automatically=_bool(
                    item, "m:storeOutputAutomatically"
                ),
                inputs_on_revisit=_text(item, "m:inputsOnNextNavToAssocScrn"),
                is_required=_bool(item, "m:isRequired"),
                choice_references=[
                    (ref.text or "") for ref in item.findall("m:choiceReferences", NS)
                ],
                default_value=_value(item.find("m:defaultValue", NS)),
                default_selected_choice=_text(
                    item, "m:defaultSelectedChoiceReference"
                ),
                scale=int(_text(item, "m:scale"))
                if _text(item, "m:scale") else None,
            )
        )
    return Screen(
        **_common(node),
        fields=fields,
        # Salesforce's own defaults, so a screen that omits them round-trips to
        # the same flow rather than one with Back and Pause switched off.
        allow_back=_bool(node, "m:allowBack", True),
        allow_finish=_bool(node, "m:allowFinish", True),
        allow_pause=_bool(node, "m:allowPause", True),
        show_footer=_bool(node, "m:showFooter", True),
        show_header=_bool(node, "m:showHeader", True),
    )


def _read_subflow(node: ET.Element) -> Subflow:
    inputs = []
    for item in node.findall("m:inputAssignments", NS):
        value = _value(item.find("m:value", NS))
        if value is None:
            continue
        inputs.append(InputAssignment(name=_text(item, "m:name") or "", value=value))
    return Subflow(
        **_fault_common(node),
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
    "screens": _read_screen,
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
        raise UnsupportedFlow(
            [Gap("unparseable", f"the XML did not parse: {exc}")], api_name
        ) from exc

    reasons: List[Gap] = []

    process_type = _text(root, "m:processType") or "AutoLaunchedFlow"
    if process_type not in ("AutoLaunchedFlow", "Flow"):
        reasons.append(Gap(
            f"process_type:{process_type}",
            f"process type {process_type} (only screen, record-triggered and "
            "autolaunched flows are supported)",
        ))

    seen_unsupported = set()
    for child in root:
        tag = child.tag.split("}")[-1]
        if tag in _IGNORED or tag in _SUPPORTED_ELEMENTS or tag in _SUPPORTED_RESOURCES:
            continue
        if tag in seen_unsupported:
            continue
        seen_unsupported.add(tag)
        reasons.append(Gap(
            f"element:{tag}",
            _KNOWN_UNSUPPORTED.get(tag, f"unrecognised element <{tag}>"),
        ))

    # The same check, one level down. This is where a fault path or a scheduled
    # path used to disappear without a word.
    for tag, allowed in _ELEMENT_CHILDREN.items():
        for node in root.findall(f"m:{tag}", NS):
            name = _text(node, "m:name") or tag
            reasons.extend(_unknown_children(node, allowed, name))

    # Resources get the same treatment: a choice read without its userInput would
    # lose the user's own typed answer on the next deploy.
    for tag, allowed in (
        ("choices", _CHOICE_CHILDREN),
        ("dynamicChoiceSets", _CHOICE_SET_CHILDREN),
        ("textTemplates", _TEXT_TEMPLATE_CHILDREN),
        ("constants", _CONSTANT_CHILDREN),
        ("formulas", _FORMULA_CHILDREN),
    ):
        for node in root.findall(f"m:{tag}", NS):
            reasons.extend(
                _unknown_children(node, allowed, _text(node, "m:name") or tag)
            )

    # And one level below that, for the fields on each screen.
    for node in root.findall("m:screens", NS):
        screen_name = _text(node, "m:name") or "a screen"
        for item in node.findall("m:fields", NS):
            where = f"{screen_name}.{_text(item, 'm:name') or 'a field'}"
            reasons.extend(_unknown_children(item, _SCREEN_FIELD_CHILDREN, where))
            # The tag is allowed; the value may not be. A RadioButtons field has
            # exactly the same children as an InputField, so nothing above would
            # notice it - and it would silently become a text box.
            kind = _text(item, "m:fieldType")
            if kind and kind not in _SUPPORTED_SCREEN_FIELD_TYPES:
                reasons.append(Gap(
                    f"screen_field:{kind}", f"{where} is a {kind} field"
                ))

    start_node = root.find("m:start", NS)
    if start_node is not None:
        reasons.extend(_unknown_children(start_node, _START_CHILDREN, "the trigger"))

    for node in root.findall("m:variables", NS):
        name = _text(node, "m:name") or "a variable"
        reasons.extend(_unknown_children(node, _VARIABLE_CHILDREN, name))

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
                reasons.append(Gap(
                    "ir_mismatch",
                    f"{name} does not fit the model: {exc}",
                ))

    if reasons:
        raise UnsupportedFlow(reasons, api_name or _text(root, "m:label") or "")

    # Flows written before Flow Builder have no <start> at all and name their
    # first element at the root. It is the same fact in an older spelling, so it
    # is read rather than refused - though writing the flow back emits the
    # modern form, which is what Salesforce itself does on save.
    legacy_start = _text(root, "m:startElementReference")
    start = Start(
        next=(
            (_target(start_node, "connector") if start_node is not None else None)
            or legacy_start
        ),
        object=_text(start_node, "m:object"),
        record_trigger_type=_text(start_node, "m:recordTriggerType"),
        trigger_type=_text(start_node, "m:triggerType"),
        filters=_filters(start_node) if start_node is not None else [],
        filter_logic=_text(start_node, "m:filterLogic") or "and",
        only_when_changed_to_meet_criteria=_bool(
            start_node, "m:doesRequireRecordChangedToMeetCriteria"
        ),
    )

    variables = []
    for node in root.findall("m:variables", NS):
        scale = _text(node, "m:scale")
        variables.append(
            Variable(
                name=_text(node, "m:name") or "",
                data_type=_text(node, "m:dataType") or "String",
                is_collection=_bool(node, "m:isCollection"),
                is_input=_bool(node, "m:isInput"),
                is_output=_bool(node, "m:isOutput"),
                object_type=_text(node, "m:objectType"),
                description=_text(node, "m:description"),
                scale=int(scale) if scale else None,
                value=_value(node.find("m:value", NS)),
            )
        )

    choices, choice_sets = [], []
    for node in root.findall("m:choices", NS):
        try:
            choices.append(_read_choice(node))
        except ValueError as exc:
            reasons.append(Gap("ir_mismatch", f"choice does not fit the model: {exc}"))
    for node in root.findall("m:dynamicChoiceSets", NS):
        try:
            choice_sets.append(_read_choice_set(node))
        except ValueError as exc:
            reasons.append(
                Gap("ir_mismatch", f"choice set does not fit the model: {exc}")
            )

    if reasons:
        raise UnsupportedFlow(reasons, api_name or _text(root, "m:label") or "")

    templates = []
    for node in root.findall("m:textTemplates", NS):
        try:
            templates.append(TextTemplate(
                name=_text(node, "m:name") or "",
                text=_text(node, "m:text") or "",
                is_viewed_as_plain_text=_bool(node, "m:isViewedAsPlainText"),
                description=_text(node, "m:description"),
            ))
        except ValueError as exc:
            reasons.append(
                Gap("ir_mismatch", f"text template does not fit the model: {exc}")
            )

    constants, formulas = [], []
    for node in root.findall("m:constants", NS):
        try:
            constants.append(Constant(
                name=_text(node, "m:name") or "",
                data_type=_text(node, "m:dataType") or "String",
                value=_value(node.find("m:value", NS)),
                description=_text(node, "m:description"),
            ))
        except ValueError as exc:
            reasons.append(Gap("ir_mismatch", f"constant does not fit the model: {exc}"))
    for node in root.findall("m:formulas", NS):
        scale = _text(node, "m:scale")
        try:
            formulas.append(Formula(
                name=_text(node, "m:name") or "",
                data_type=_text(node, "m:dataType") or "String",
                expression=_text(node, "m:expression") or "",
                scale=int(scale) if scale else None,
                description=_text(node, "m:description"),
            ))
        except ValueError as exc:
            reasons.append(Gap("ir_mismatch", f"formula does not fit the model: {exc}"))

    if reasons:
        raise UnsupportedFlow(reasons, api_name or _text(root, "m:label") or "")

    label = _text(root, "m:label") or api_name or "Untitled"
    try:
        return Flow(
            api_name=api_name or _text(root, "m:fullName") or _slug(label),
            label=label,
            description=_text(root, "m:description"),
            api_version=_text(root, "m:apiVersion") or "62.0",
            process_type=process_type,
            status=_text(root, "m:status") or "Draft",
            start=start,
            elements=elements,
            variables=variables,
            choices=choices,
            dynamic_choice_sets=choice_sets,
            text_templates=templates,
            constants=constants,
            formulas=formulas,
        )
    except ValueError as exc:
        # The flow deployed, so this means the IR is stricter than Salesforce.
        # Say so plainly rather than pretending the flow is malformed.
        raise UnsupportedFlow(
            [Gap("ir_mismatch", f"it does not fit the model: {exc}")],
            api_name or label,
        ) from exc


def _slug(label: str) -> str:
    parts = [part for part in "".join(
        char if char.isalnum() else " " for char in label
    ).split() if part]
    return "_".join(parts) or "Flow"
