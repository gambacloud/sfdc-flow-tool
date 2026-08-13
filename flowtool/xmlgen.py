"""
IR -> Flow XML.

Deterministic, no LLM involved. Child element ordering follows what Salesforce
itself emits when you retrieve a Flow, because the Metadata API is picky about
it in places.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from typing import Dict, List, Optional, Tuple

from .ir import (
    ActionCall,
    Assignment,
    Choice,
    CollectionFilter,
    CollectionSort,
    Constant,
    CustomError,
    Decision,
    DynamicChoiceSet,
    Element,
    Flow,
    Formula,
    GetRecords,
    Loop,
    OrchestratedStage,
    RecordCreate,
    RecordDelete,
    RecordFilter,
    RecordUpdate,
    Screen,
    Subflow,
    TextTemplate,
    Transform,
    Value,
    Wait,
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


def _conditions_el(parent: ET.Element, conditions, tag: str = "conditions") -> None:
    for cond in conditions:
        c = _sub(parent, tag)
        _sub(c, "leftValueReference", cond.left)
        _sub(c, "operator", cond.operator)
        if cond.right is not None:
            _value_el(c, "rightValue", cond.right)


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
    _sub(node, "assignNullValuesIfNoRecordsFound",
         _bool(el.assign_null_values_if_no_records_found))
    _connector(node, "connector", el.next)
    _fault(node, el)
    if el.filters:
        _sub(node, "filterLogic", el.filter_logic)
        _filters(node, el.filters)
    # A flow that never stated this is written back without it, rather than
    # having an answer invented for it.
    if el.first_record_only is not None:
        _sub(node, "getFirstRecordOnly", _bool(el.first_record_only))
    _sub(node, "object", el.object)
    for assignment in el.output_assignments:
        oa = _sub(node, "outputAssignments")
        _sub(oa, "assignToReference", assignment.assign_to_reference)
        _sub(oa, "field", assignment.field)
    if el.output_reference:
        _sub(node, "outputReference", el.output_reference)
    for field_name in el.queried_fields:
        _sub(node, "queriedFields", field_name)
    if el.sort_field:
        _sub(node, "sortField", el.sort_field)
        _sub(node, "sortOrder", "Asc" if el.sort_order != "Desc" else "Desc")
    if el.store_output_automatically:
        _sub(node, "storeOutputAutomatically", "true")


def _write_record_create(root: ET.Element, el: RecordCreate, xy) -> None:
    node = _sub(root, "recordCreates")
    _write_common(node, el, xy)
    if el.assign_record_id_to_reference:
        _sub(node, "assignRecordIdToReference", el.assign_record_id_to_reference)
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
    if el.store_output_automatically:
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
    if el.assign_next_value_to_reference:
        _sub(node, "assignNextValueToReference",
             el.assign_next_value_to_reference)
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
    for mapping in el.data_type_mappings:
        dtm = _sub(node, "dataTypeMappings")
        _sub(dtm, "typeName", mapping.type_name)
        _sub(dtm, "typeValue", mapping.type_value)
    _fault(node, el)
    if el.flow_transaction_model:
        _sub(node, "flowTransactionModel", el.flow_transaction_model)
    for parameter in el.input_parameters:
        ip = _sub(node, "inputParameters")
        _sub(ip, "name", parameter.name)
        _value_el(ip, "value", parameter.value)
    if el.is_wait_until_completed:
        _sub(node, "isWaitUntilCompleted", "true")
    if el.timeout_offset is not None:
        _sub(node, "offset", str(el.timeout_offset))
    if el.timeout_offset_unit:
        _sub(node, "offsetUnit", el.timeout_offset_unit)
    for parameter in el.output_parameters:
        op = _sub(node, "outputParameters")
        _sub(op, "assignToReference", parameter.assign_to_reference)
        _sub(op, "name", parameter.name)
    if el.store_output_automatically:
        _sub(node, "storeOutputAutomatically", "true")
    _connector(node, "timeoutConnector", el.timeout_next)


def _write_choice(root: ET.Element, choice: Choice) -> None:
    """A resource, not an element: no name/label pair, no coordinates, no connector."""
    node = _sub(root, "choices")
    _sub(node, "name", choice.name)
    _sub(node, "choiceText", choice.choice_text)
    _sub(node, "dataType", choice.data_type)
    if choice.user_input is not None:
        ui = _sub(node, "userInput")
        if choice.user_input.is_required:
            _sub(ui, "isRequired", "true")
        if choice.user_input.prompt_text:
            _sub(ui, "promptText", choice.user_input.prompt_text)
        if choice.user_input.validation is not None:
            rule = _sub(ui, "validationRule")
            _sub(rule, "errorMessage", choice.user_input.validation.error_message)
            _sub(rule, "formulaExpression",
                 choice.user_input.validation.formula_expression)
    if choice.value is not None:
        _value_el(node, "value", choice.value)


def _write_choice_set(root: ET.Element, choice_set: DynamicChoiceSet) -> None:
    node = _sub(root, "dynamicChoiceSets")
    _sub(node, "name", choice_set.name)
    _sub(node, "dataType", choice_set.data_type)
    if choice_set.collection_reference:
        _sub(node, "collectionReference", choice_set.collection_reference)
    if choice_set.display_field:
        _sub(node, "displayField", choice_set.display_field)
    if choice_set.filters:
        _sub(node, "filterLogic", choice_set.filter_logic)
        _filters(node, choice_set.filters)
    if choice_set.limit is not None:
        _sub(node, "limit", str(choice_set.limit))
    if choice_set.object:
        _sub(node, "object", choice_set.object)
    if choice_set.picklist_field:
        _sub(node, "picklistField", choice_set.picklist_field)
    if choice_set.picklist_object:
        _sub(node, "picklistObject", choice_set.picklist_object)
    if choice_set.sort_field:
        _sub(node, "sortField", choice_set.sort_field)
        _sub(node, "sortOrder", "Desc" if choice_set.sort_order == "Desc" else "Asc")
    if choice_set.value_field:
        _sub(node, "valueField", choice_set.value_field)


def _write_screen_field(parent: ET.Element, screen_field) -> None:
    """
    One field, and any fields nested inside it.

    Recursive because a section holds columns and a column holds fields. Two
    levels is all Salesforce allows, but writing it as a walk rather than two
    hard-coded loops keeps the ordering rules in one place - a field inside a
    column has to be written exactly like one outside it, or a section would
    quietly lose the attributes the top level got right.
    """
    f = _sub(parent, "fields")
    _sub(f, "name", screen_field.name)
    for reference in screen_field.choice_references:
        _sub(f, "choiceReferences", reference)
    if screen_field.data_type:
        _sub(f, "dataType", screen_field.data_type)
    for mapping in screen_field.data_type_mappings:
        dtm = _sub(f, "dataTypeMappings")
        _sub(dtm, "typeName", mapping.type_name)
        _sub(dtm, "typeValue", mapping.type_value)
    if screen_field.default_selected_choice:
        _sub(f, "defaultSelectedChoiceReference",
             screen_field.default_selected_choice)
    if screen_field.default_value is not None:
        _value_el(f, "defaultValue", screen_field.default_value)
    if screen_field.extension_name:
        _sub(f, "extensionName", screen_field.extension_name)
    for nested in screen_field.fields:
        _write_screen_field(f, nested)
    if screen_field.field_text is not None:
        _sub(f, "fieldText", screen_field.field_text)
    _sub(f, "fieldType", screen_field.field_type)
    if screen_field.help_text is not None:
        _sub(f, "helpText", screen_field.help_text)
    for parameter in screen_field.input_parameters:
        ip = _sub(f, "inputParameters")
        _sub(ip, "name", parameter.name)
        _value_el(ip, "value", parameter.value)
    if screen_field.inputs_on_revisit:
        _sub(f, "inputsOnNextNavToAssocScrn", screen_field.inputs_on_revisit)
    if screen_field.is_disabled is not None:
        _value_el(f, "isDisabled", screen_field.is_disabled)
    if screen_field.is_read_only is not None:
        _value_el(f, "isReadOnly", screen_field.is_read_only)
    # DisplayText collects nothing, so isRequired would be meaningless on it -
    # and Salesforce omits it there too.
    if screen_field.field_type != "DisplayText":
        _sub(f, "isRequired", _bool(screen_field.is_required))
    if screen_field.is_visible is not None:
        _sub(f, "isVisible", _bool(screen_field.is_visible))
    if screen_field.object_field_reference:
        _sub(f, "objectFieldReference", screen_field.object_field_reference)
    for parameter in screen_field.output_parameters:
        op = _sub(f, "outputParameters")
        _sub(op, "assignToReference", parameter.assign_to_reference)
        _sub(op, "name", parameter.name)
    if screen_field.region_container_type:
        _sub(f, "regionContainerType", screen_field.region_container_type)
    if screen_field.scale is not None:
        _sub(f, "scale", str(screen_field.scale))
    if screen_field.store_output_automatically:
        _sub(f, "storeOutputAutomatically", "true")
    if screen_field.validation:
        rule = _sub(f, "validationRule")
        _sub(rule, "errorMessage", screen_field.validation.error_message)
        _sub(rule, "formulaExpression",
             screen_field.validation.formula_expression)
    if screen_field.visibility:
        rule = _sub(f, "visibilityRule")
        _sub(rule, "conditionLogic", screen_field.visibility.condition_logic)
        for condition in screen_field.visibility.conditions:
            c = _sub(rule, "conditions")
            _sub(c, "leftValueReference", condition.left)
            _sub(c, "operator", condition.operator)
            if condition.right is not None:
                _value_el(c, "rightValue", condition.right)


def _write_screen(root: ET.Element, el: Screen, xy) -> None:
    node = _sub(root, "screens")
    _write_common(node, el, xy)
    _sub(node, "allowBack", _bool(el.allow_back))
    _sub(node, "allowFinish", _bool(el.allow_finish))
    _sub(node, "allowPause", _bool(el.allow_pause))
    if el.back_button_label:
        _sub(node, "backButtonLabel", el.back_button_label)
    _connector(node, "connector", el.next)
    for screen_field in el.fields:
        _write_screen_field(node, screen_field)
    if el.help_text:
        _sub(node, "helpText", el.help_text)
    if el.next_or_finish_button_label:
        _sub(node, "nextOrFinishButtonLabel", el.next_or_finish_button_label)
    if el.pause_button_label:
        _sub(node, "pauseButtonLabel", el.pause_button_label)
    if el.paused_text:
        _sub(node, "pausedText", el.paused_text)
    _sub(node, "showFooter", _bool(el.show_footer))
    _sub(node, "showHeader", _bool(el.show_header))


def _write_wait(root: ET.Element, el: Wait, xy) -> None:
    node = _sub(root, "waits")
    # _write_common writes the plain connector, which a Pause does not have -
    # the IR refuses `next` on one. Everything else it writes still applies.
    _write_common(node, el, xy)
    _connector(node, "defaultConnector", el.default_next)
    _sub(node, "defaultConnectorLabel", el.default_label)
    _fault(node, el)
    for event in el.wait_events:
        item = _sub(node, "waitEvents")
        _sub(item, "name", event.name)
        if event.conditions:
            _sub(item, "conditionLogic", event.condition_logic)
            for condition in event.conditions:
                c = _sub(item, "conditions")
                _sub(c, "leftValueReference", condition.left)
                _sub(c, "operator", condition.operator)
                if condition.right is not None:
                    _value_el(c, "rightValue", condition.right)
        _connector(item, "connector", event.next)
        _sub(item, "eventType", event.event_type)
        for parameter in event.input_parameters:
            ip = _sub(item, "inputParameters")
            _sub(ip, "name", parameter.name)
            _value_el(ip, "value", parameter.value)
        if event.label:
            _sub(item, "label", event.label)


def _write_transform(root: ET.Element, el: Transform, xy) -> None:
    node = _sub(root, "transforms")
    _write_common(node, el, xy)
    if el.apex_class:
        _sub(node, "apexClass", el.apex_class)
    _connector(node, "connector", el.next)
    if el.is_collection:
        _sub(node, "isCollection", "true")
    if el.object_type:
        _sub(node, "objectType", el.object_type)
    if el.scale is not None:
        _sub(node, "scale", str(el.scale))
    if el.schema_uri:
        _sub(node, "schemaUri", el.schema_uri)
    if el.store_output_automatically:
        _sub(node, "storeOutputAutomatically", "true")
    for tv in el.transform_values:
        item = _sub(node, "transformValues")
        for action in tv.actions:
            a = _sub(item, "transformValueActions")
            if action.assign_to_reference:
                _sub(a, "assignToReference", action.assign_to_reference)
            for parameter in action.input_parameters:
                ip = _sub(a, "inputParameters")
                _sub(ip, "name", parameter.name)
                _value_el(ip, "value", parameter.value)
            if action.name:
                _sub(a, "name", action.name)
            if action.output_field_api_name:
                _sub(a, "outputFieldApiName", action.output_field_api_name)
            _sub(a, "transformType", action.transform_type)
            if action.value is not None:
                _value_el(a, "value", action.value)
        if tv.description:
            _sub(item, "transformValueDescription", tv.description)
        if tv.label:
            _sub(item, "transformValueLabel", tv.label)
        if tv.name:
            _sub(item, "transformValueName", tv.name)


def _write_custom_error(root: ET.Element, el: CustomError, xy) -> None:
    node = _sub(root, "customErrors")
    # No connector: the IR refuses `next` on a Custom Error, the same as Wait.
    _write_common(node, el, xy)
    for msg in el.messages:
        item = _sub(node, "customErrorMessages")
        _sub(item, "errorMessage", msg.error_message)
        if msg.field_selection:
            _sub(item, "fieldSelection", msg.field_selection)
        if msg.is_field_error:
            _sub(item, "isFieldError", "true")


# The real actionType a stage step needs, confirmed against a real dev org -
# every ordinary ActionCall actionType (flow, apex, emailSimple, submit,
# chatterPost) is flatly refused here. step_subtype is the one fact the IR
# asks for; this is how it becomes the tag Salesforce actually wants.
_STAGE_STEP_ACTION_TYPE = {
    "BackgroundStep": "stepBackground",
    "InteractiveStep": "stepInteractive",
}


def _write_stage_step(parent: ET.Element, step) -> None:
    node = _sub(parent, "stageSteps")
    _sub(node, "name", step.name)
    _sub(node, "actionName", step.action_name)
    _sub(node, "actionType", _STAGE_STEP_ACTION_TYPE[step.step_subtype])
    for a in step.assignees:
        assignee = _sub(node, "assignees")
        _value_el(assignee, "assignee", a.assignee)
        _sub(assignee, "assigneeType", a.assignee_type)
    _sub(node, "canAssigneeEdit", _bool(step.can_assignee_edit))
    if step.description:
        _sub(node, "description", step.description)
    _sub(node, "entryConditionLogic", step.entry_condition_logic)
    _conditions_el(node, step.entry_conditions, tag="entryConditions")
    _sub(node, "exitConditionLogic", step.exit_condition_logic)
    _conditions_el(node, step.exit_conditions, tag="exitConditions")
    for parameter in step.input_parameters:
        ip = _sub(node, "inputParameters")
        _sub(ip, "name", parameter.name)
        _value_el(ip, "value", parameter.value)
    _sub(node, "label", step.label)
    _sub(node, "requiresAsyncProcessing", _bool(step.requires_async_processing))
    _sub(node, "runAsUser", _bool(step.run_as_user))
    _sub(node, "shouldLock", _bool(step.should_lock))
    _sub(node, "stepSubtype", step.step_subtype)


def _write_orchestrated_stage(root: ET.Element, el: OrchestratedStage, xy) -> None:
    node = _sub(root, "orchestratedStages")
    _write_common(node, el, xy)
    _connector(node, "connector", el.next)
    _sub(node, "exitConditionLogic", el.exit_condition_logic)
    _conditions_el(node, el.exit_conditions, tag="exitConditions")
    for step in el.stage_steps:
        _write_stage_step(node, step)


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
    for assignment in el.output_assignments:
        oa = _sub(node, "outputAssignments")
        _sub(oa, "assignToReference", assignment.assign_to_reference)
        _sub(oa, "name", assignment.name)
    if el.store_output_automatically:
        _sub(node, "storeOutputAutomatically", "true")


def _write_collection_filter(root: ET.Element, el: CollectionFilter, xy) -> None:
    node = _sub(root, "collectionProcessors")
    _write_common(node, el, xy)
    _sub(node, "assignNextValueToReference", el.current_item)
    _sub(node, "collectionProcessorType", "FilterCollectionProcessor")
    _sub(node, "collectionReference", el.collection_reference)
    _sub(node, "conditionLogic", el.condition_logic)
    for cond in el.conditions:
        c = _sub(node, "conditions")
        _sub(c, "leftValueReference", cond.left)
        _sub(c, "operator", cond.operator)
        if cond.right is not None:
            _value_el(c, "rightValue", cond.right)
    _connector(node, "connector", el.next)
    _sub(node, "elementSubtype", "FilterCollectionProcessor")


def _write_collection_sort(root: ET.Element, el: CollectionSort, xy) -> None:
    node = _sub(root, "collectionProcessors")
    _write_common(node, el, xy)
    _sub(node, "collectionProcessorType", "SortCollectionProcessor")
    _sub(node, "collectionReference", el.collection_reference)
    _connector(node, "connector", el.next)
    _sub(node, "elementSubtype", "SortCollectionProcessor")
    for option in el.sort_options:
        so = _sub(node, "sortOptions")
        _sub(so, "doesPutEmptyStringAndNullFirst",
             _bool(option.does_put_empty_string_and_null_first))
        _sub(so, "sortField", option.sort_field)
        _sub(so, "sortOrder", option.sort_order)


_WRITERS = {
    ActionCall: ("actionCalls", _write_action_call),
    Assignment: ("assignments", _write_assignment),
    CollectionFilter: ("collectionProcessors", _write_collection_filter),
    CollectionSort: ("collectionProcessors", _write_collection_sort),
    CustomError: ("customErrors", _write_custom_error),
    Decision: ("decisions", _write_decision),
    Loop: ("loops", _write_loop),
    OrchestratedStage: ("orchestratedStages", _write_orchestrated_stage),
    RecordCreate: ("recordCreates", _write_record_create),
    RecordDelete: ("recordDeletes", _write_record_delete),
    GetRecords: ("recordLookups", _write_get_records),
    RecordUpdate: ("recordUpdates", _write_record_update),
    Screen: ("screens", _write_screen),
    Wait: ("waits", _write_wait),
    Subflow: ("subflows", _write_subflow),
    Transform: ("transforms", _write_transform),
}

# The order Salesforce emits Flow children in. Alphabetical, which interleaves
# the two resource tags among the elements - they are written by name below
# rather than from a bucket, since nothing connects to them.
_RESOURCE_TAGS = ("choices", "constants", "dynamicChoiceSets", "formulas")

_ROOT_ORDER = [
    "actionCalls",
    "assignments",
    "choices",
    "collectionProcessors",
    "constants",
    "customErrors",
    "decisions",
    "dynamicChoiceSets",
    "formulas",
    "loops",
    "orchestratedStages",
    "recordCreates",
    "recordDeletes",
    "recordLookups",
    "recordUpdates",
    "screens",
    "subflows",
    "transforms",
    "waits",
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
        if tag == "choices":
            for choice in flow.choices:
                _write_choice(root, choice)
            continue
        if tag == "dynamicChoiceSets":
            for choice_set in flow.dynamic_choice_sets:
                _write_choice_set(root, choice_set)
            continue
        if tag == "constants":
            for constant in flow.constants:
                node = _sub(root, "constants")
                if constant.description:
                    _sub(node, "description", constant.description)
                _sub(node, "name", constant.name)
                _sub(node, "dataType", constant.data_type)
                _value_el(node, "value", constant.value)
            continue
        if tag == "formulas":
            for formula in flow.formulas:
                node = _sub(root, "formulas")
                if formula.description:
                    _sub(node, "description", formula.description)
                _sub(node, "name", formula.name)
                _sub(node, "dataType", formula.data_type)
                _sub(node, "expression", formula.expression)
                if formula.scale is not None:
                    _sub(node, "scale", str(formula.scale))
            continue
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
    if flow.start.filter_formula:
        _sub(start, "filterFormula", flow.start.filter_formula)
    if flow.start.filters:
        _sub(start, "filterLogic", flow.start.filter_logic)
        _filters(start, flow.start.filters)
    if flow.start.flow_run_as_user:
        _sub(start, "flowRunAsUser", flow.start.flow_run_as_user)
    if flow.start.object:
        _sub(start, "object", flow.start.object)
    if flow.start.only_when_changed_to_meet_criteria:
        _sub(start, "doesRequireRecordChangedToMeetCriteria", "true")
    if flow.start.record_trigger_type:
        _sub(start, "recordTriggerType", flow.start.record_trigger_type)
    if flow.start.schedule:
        node = _sub(start, "schedule")
        _sub(node, "frequency", flow.start.schedule.frequency)
        _sub(node, "startDate", flow.start.schedule.start_date)
        _sub(node, "startTime", flow.start.schedule.start_time)
    # Alphabetical among the start's children, as everywhere else, which puts
    # scheduledPaths between recordTriggerType and triggerType.
    for path in flow.start.scheduled_paths:
        node = _sub(start, "scheduledPaths")
        _sub(node, "name", path.name)
        if path.label:
            _sub(node, "label", path.label)
        _connector(node, "connector", path.next)
        if path.max_batch_size is not None:
            _sub(node, "maxBatchSize", str(path.max_batch_size))
        if path.offset_number is not None:
            _sub(node, "offsetNumber", str(path.offset_number))
        if path.offset_unit:
            _sub(node, "offsetUnit", path.offset_unit)
        # The one value this tag takes. An async path carries nothing else, so
        # there is no ordering question about the rest.
        if path.run_asynchronously:
            _sub(node, "pathType", "AsyncAfterCommit")
        if path.record_field:
            _sub(node, "recordField", path.record_field)
        if path.time_source:
            _sub(node, "timeSource", path.time_source)
    if flow.start.trigger_type:
        _sub(start, "triggerType", flow.start.trigger_type)

    _sub(root, "status", flow.status)

    for template in flow.text_templates:
        node = _sub(root, "textTemplates")
        if template.description:
            _sub(node, "description", template.description)
        _sub(node, "name", template.name)
        if template.is_viewed_as_plain_text:
            _sub(node, "isViewedAsPlainText", "true")
        _sub(node, "text", template.text)

    for var in flow.variables:
        # The order Salesforce itself emits: the inherited description and name
        # first, then the variable's own fields alphabetically.
        node = _sub(root, "variables")
        if var.description:
            _sub(node, "description", var.description)
        _sub(node, "name", var.name)
        _sub(node, "dataType", var.data_type)
        _sub(node, "isCollection", _bool(var.is_collection))
        _sub(node, "isInput", _bool(var.is_input))
        _sub(node, "isOutput", _bool(var.is_output))
        if var.object_type:
            _sub(node, "objectType", var.object_type)
        if var.scale is not None:
            _sub(node, "scale", str(var.scale))
        if var.value is not None:
            _value_el(node, "value", var.value)

    # ET.indent rather than minidom.toprettyxml. minidom puts a blank line
    # between elements, which had to be filtered out again - and that filter
    # dropped every blank line, including the ones inside a text template or a
    # screen's display text. A paragraph break is content, not formatting.
    # ET.indent leaves any element that has text alone.
    ET.indent(root, space="    ")
    body = ET.tostring(root, encoding="unicode")
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + body + "\n"
