"""
Natural language -> Flow IR.

The model's only job is producing a valid IR instance. It never writes XML and
never sees XML. Everything it gets wrong is caught by Pydantic before a single
byte of metadata exists, and the validation error is fed back to it verbatim.

Providers are bring-your-own-key and return raw JSON; validation lives here so
every provider goes through the same gate.
"""

from __future__ import annotations

import json
import logging
import math
import os
import random
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Generic, List, Optional, Protocol, Type, TypeVar

from pydantic import BaseModel, ValidationError

from .ir import Flow
from .ir_apex import ApexClass, ApexTrigger, heuristic_errors, heuristic_trigger_errors
from .ir_lwc import LightningComponent
from .ir_lwc import heuristic_errors as lwc_heuristic_errors
from .ir_object import CustomField, CustomObject

DEFAULT_MAX_REPAIRS = 3

# Stamped onto the api_name of every flow this tool designs from scratch, the
# way a managed package namespaces its components - so it stays identifiable
# in a flow list long after the fact. Not applied to a flow opened from the
# org and refined (see FlowGenerator.adopt/refine): that flow already has an
# identity, and prefixing it would deploy as a new flow instead of updating
# the one the user opened.
GENERATED_NAME_PREFIX = "GC_"

log = logging.getLogger("flowtool")


@dataclass
class Usage:
    """
    Running token cost for a provider. Cached input is counted separately
    because it is billed at about a tenth of the rate, so folding it into the
    input total would overstate what a session actually costs.
    """

    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0
    thinking_tokens: int = 0
    # A plan can now run several steps' generations concurrently in separate
    # threads (see planner.execute_plan), all updating the one Usage a
    # session's provider carries - `+=` is not atomic, so without this a
    # lost update under real concurrency would silently under-report cost.
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    def add(
        self,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cached_input_tokens: int = 0,
        thinking_tokens: int = 0,
    ) -> None:
        with self._lock:
            self.calls += 1
            self.input_tokens += input_tokens or 0
            self.output_tokens += output_tokens or 0
            self.cached_input_tokens += cached_input_tokens or 0
            self.thinking_tokens += thinking_tokens or 0

    def as_dict(self) -> Dict[str, int]:
        return {
            "calls": self.calls,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cached_input_tokens": self.cached_input_tokens,
            "thinking_tokens": self.thinking_tokens,
        }

    def __str__(self) -> str:
        parts = [
            f"{self.calls} call{'' if self.calls == 1 else 's'}",
            f"in {self.input_tokens:,}",
            f"out {self.output_tokens:,}",
        ]
        if self.cached_input_tokens:
            parts.append(f"cached {self.cached_input_tokens:,}")
        if self.thinking_tokens:
            parts.append(f"thinking {self.thinking_tokens:,}")
        return ", ".join(parts)


class LLMError(RuntimeError):
    pass


@dataclass
class Message:
    role: str  # "user" | "assistant"
    content: str


class Provider(Protocol):
    """
    Return the model's JSON object for the given conversation. Implementations
    must request schema-constrained output where the provider supports it, but
    are never trusted to have produced a valid Flow - that is checked here.
    """

    name: str
    usage: Usage

    def complete_json(
        self, system: str, messages: List[Message], schema: Dict[str, Any]
    ) -> Dict[str, Any]: ...

    def complete_text(self, system: str, messages: List[Message]) -> str:
        """Prose, for explaining a flow rather than building one."""
        ...


# --------------------------------------------------------------------------
# System prompt
# --------------------------------------------------------------------------

# Rules that Salesforce enforces at deploy time. Each one here was either
# learned from a real deploy failure or is a constraint the IR cannot express
# structurally. Keep this list tied to observed failures - speculative rules
# make the model more cautious without making it more correct.
SYSTEM_PROMPT = """\
You translate a description of business logic into a Salesforce Flow IR document.

You produce IR only. You never write Flow XML - a compiler generates that from \
your IR, and it is not your concern.

## How the IR works

- `start.next` must name the first element. Without it nothing is connected to \
the Start element and Salesforce refuses to run the flow.
- Elements are connected by `next`, which names another element.
- `next: null` means the path ends there. There is no "End" element; a path \
that ends simply has no next.
- Every element must be reachable from start by following connectors.
- A Decision's `next` is its default (else) path. Each outcome has its own \
`next`, and that is what carries control when the outcome's conditions are met. \
Elements do not run in list order: an element only runs if some connector points \
at it. If a Decision is meant to lead to an element, set that outcome's `next` to \
the element's name.
- A Loop's `first_element` is the first element inside the loop body; its `next` \
is what runs after the loop finishes.
- Conditions are structured: a left reference, an operator, and a typed right \
value. Never write a condition as a formula string.

## Rules Salesforce enforces

- Element and outcome names must be valid API names: start with a letter, then \
letters, digits, and single underscores. No spaces, no trailing underscore, no \
double underscore.
- Update Records takes one of three shapes: `input_reference` alone (update the \
record as it stands), `object` + `filters` + `fields` (find records by criteria \
and set values), or `input_reference` + `fields` (take the record from a \
reference and set values on it). `filters` never goes with `input_reference`.
- A reference must be writable to set fields on it. `$Record` is. The output of \
a Get Records with `store_output_automatically` is not - to change values on a \
record you looked up, update by criteria filtered on its Id instead.
- Create Records takes either `input_reference` alone, or `object` plus \
`fields`. A variable already carries its object, so `input_reference` never \
comes with `object` or `fields`.
- A record retrieved with `store_output_automatically: true` is read-only. You \
cannot assign into its fields. To change values on it, use Update Records by \
criteria filtered on its Id.
- Record-triggered flows must set `object`, `record_trigger_type`, and \
`trigger_type` on start. Autolaunched flows leave all three empty.
- A flow that runs when a platform event is published sets `object` to the \
event (`Order_Placed__e`) and `trigger_type` to `PlatformEvent`, and leaves \
`record_trigger_type` empty - an event is only ever published, so there is no \
create-or-update to choose between. Its fields are read as `$Record` like any \
other triggering record.
- Reference the triggering record as `$Record` (for example `$Record.Amount`), \
and a retrieved record by its element name (for example `Get_Account.Id`).
- Element names, variable names and screen field names share one namespace. \
Each name can be used once in the flow.
- A variable may carry a `value`, which is what it holds before anything \
assigns to it. Use it only when the request calls for a starting value; an \
unset variable is already empty.
- `text_templates` hold a block of text with merge fields written `{!name}`, \
for an email body or a message shown to the user. Write the wording out in the \
template rather than assembling it from string assignments.
- `constants` hold a fixed value that never changes. `formulas` hold a value \
recomputed from an expression each time it is read, written in Salesforce \
formula syntax with other resources referenced as `{!name}`.
- A formula's `expression` is the one free-form string you write, and nothing \
checks it before the org does. Keep it to what the request actually needs. \
Conditions on a Decision are still structured - never move logic into a formula \
to avoid writing a condition.

## Waiting inside the flow

A `Wait` element (Pause) stops the flow and Salesforce resumes it later. It is \
allowed in **only one kind of flow**, and the org says so twice over: not in a \
screen flow, and not in a record-triggered one. So a Pause belongs to a plain \
autolaunched flow with no `object` and no `trigger_type`.

**If the request starts with a record changing, you want a scheduled path, not \
a Pause.** They are the same idea - do this later - in the two forms Salesforce \
allows, and which one is right is decided by what started the flow.

- A Pause has no `next`. It leaves through each wait event's own `next`, or \
through `default_next` when the flow resumed for some other reason.
- `AlarmEvent` resumes at a moment in time: one `input_parameters` entry named \
`AlarmTime` whose value is a DateTime reference.
- `DateRefAlarmEvent` resumes relative to a date field on a record: \
`SalesforceObject`, `BaseDateTimeFieldName`, `RecordId`, and optionally \
`TimeOffset` with `TimeOffsetUnit`.
- Salesforce checks none of this. A Pause with no `AlarmTime`, or with the name \
misspelled, deploys and then never resumes.

## Doing something later

"Three days after", "an hour before it is due", "next week" - that is a \
`scheduled_path` on the start, not a loop and not a wait. The flow finishes its \
immediate run, and Salesforce comes back later and starts again at the path's \
`next`.

- Only on a record-triggered flow with `trigger_type: RecordAfterSave`. \
Salesforce refuses them on a before-save or before-delete trigger, because \
nothing has been committed yet.
- `time_source: RecordTriggerEvent` counts from the moment the record changed. \
`RecordField` counts from a date field on the record, which `record_field` must \
name - that is the one that lets a path run *before* something, with a negative \
`offset_number`.
- `offset_number` and `offset_unit` go together. Units are Minutes, Hours, \
Days, Months.
- `run_asynchronously: true` is a different thing: it runs straight away but in \
its own transaction, for work that cannot happen inside the trigger - a callout, \
usually. Nothing else may be set on such a path.
- Give each path its own element to run. Two paths may lead to the same element, \
but the immediate branch and a scheduled branch are separate paths through the \
flow.

## Combining conditions

An outcome's `condition_logic`, and the `filter_logic` on record elements, is \
`and` when every condition must hold and `or` when any one will do. Prefer \
those two: they say what they mean and cannot drift.

When the request genuinely mixes the two - "if it is urgent, or if it is over \
the limit and not already approved" - write an expression over the condition \
numbers instead: `1 OR (2 AND 3)`. Conditions are numbered from 1, in the order \
you list them, using AND, OR, NOT and brackets.

Two things to be careful of, because Salesforce checks neither and deploys \
either without complaint:

- Every number must match a condition that exists. Three conditions means the \
numbers 1, 2 and 3 - `4` is not an error to the org, it is a flow that \
evaluates wrongly.
- **When you change the conditions, renumber the expression.** Removing the \
second of three conditions leaves the third as the new number 2, and any \
expression still naming 3 is now pointing past the end.

Use every condition you list. One the expression never names is evaluated and \
then ignored, which is almost always a mistake in the expression.

## Filtering or sorting a collection already in memory

`CollectionFilter` and `CollectionSort` reshape a collection you already have - \
the output of a Loop's collection, a Get Records with `store_output_automatically`, \
or another collection variable. They do not query the database; for that, filter \
on Get Records instead.

- Both read `{!ElementName}` as their result, the same as any element with \
automatic output storage - there is no separate output field to set.
- A filter needs `current_item`: a name for "the item being tested", used only \
while its `conditions` are evaluated - write them as `{current_item}.Field`. It \
is not the final result and nothing else in the flow can reference it.
- A sort takes one or more `sort_options`, each a field and an order. List more \
than one only when the request asks to break ties on a second field.

## Screens

Set `process_type` to `Flow` when the logic needs a person to see or type \
something. Leave it `AutoLaunchedFlow` otherwise - that is the default and \
covers both record-triggered and autolaunched flows.

- A screen flow is started by a user, so its start has no `object`, \
`record_trigger_type` or `trigger_type`, and there is no `$Record`. Take what \
the flow needs from screen inputs or from input variables instead.
- Only a screen flow may contain a Screen. A record-triggered or autolaunched \
flow runs with nobody watching, and Salesforce rejects a screen in one.
- A screen's `next` is what runs when the user clicks Next. A screen is not the \
end of the flow unless you mean it to be - if the flow continues after the user \
fills the screen in, set the screen's `next` to the element that continues it.
- A screen's `fields` are, in order, what the user sees. The kinds are:
  - `DisplayText` - text shown to the user. `field_text` is the text itself. \
No `data_type`.
  - `InputField` - a box the user types in. `field_text` is its label, and \
`data_type` says what it holds (String, Number, Currency, Date, DateTime, \
Boolean).
  - `LargeTextArea` - a multi-line box. `field_text` is its label, no `data_type`.
  - `RadioButtons` and `DropdownBox` - pick one from a list.
  - `MultiSelectCheckboxes` and `MultiSelectPicklist` - pick several. Their \
`data_type` is still the type of one option, usually `String`; Salesforce joins \
the several answers into it.
  - `ComponentInstance` - a custom LWC or Aura component. See below.
  - `RegionContainer` and `Region` - a section and a column. See below.
- Any field can carry `help_text`, a `visibility` rule that decides whether it \
is shown at all, and - if it collects something - a `validation` rule the \
answer must satisfy.
- `visibility` reads other fields on the same screen by name, and it is worth \
being careful with: Salesforce deploys a rule reading a field that does not \
exist, and the field then simply never appears.
- `validation` needs both a `formula_expression` that must be true and an \
`error_message` to show when it is not. Not on a `DisplayText` - it collects \
nothing.

## Sections and columns

Put fields side by side with a `RegionContainer` holding `Region`s:

- The container is the section. `region_container_type` is \
`SectionWithoutHeader`, or `SectionWithHeader` with `field_text` as the heading.
- Each Region is a column and needs one `input_parameters` entry named `width`, \
a string from `"1"` to `"12"`. The widths in one section should add up to 12.
- The fields go inside the Regions. A section holds only columns, a column \
holds only ordinary fields, and that is as deep as it goes - Salesforce refuses \
a section inside a column.
- Only reach for this when the request actually asks for a layout. A plain list \
of fields is the normal thing and reads better than a one-column section.
- Read what the user entered by the field's own name: a field named \
`Customer_Email` is referenced as `Customer_Email`, not as `Screen1.Customer_Email`.

## Components on a screen

A `ComponentInstance` field places a component the org already has. \
`extension_name` names it as `namespace:name`, and it has no `field_text` - \
the component labels itself.

**Never invent a component.** The org checks the name, the input names and the \
output names against the component's real signature, and rejects all three by \
name. Use one only when the user names it, when an imported flow already had \
it, or when it is one of the standard ones: `flowruntime:lookup` (a record \
lookup box), `flowruntime:slider`, `flowruntime:address`, \
`forceContent:fileUpload`. If the request seems to need a component you cannot \
name, build it from the ordinary field kinds above and say so in the flow's \
`description`.

- `input_parameters` pass values in, keyed by the properties the component \
declares.
- The component's outputs come back one of two ways, never both: \
`output_parameters` assigns each one to a variable you have defined, or \
`store_output_automatically` keeps them all under the field's own name, read as \
`{!fieldName.outputName}`.
- A variable named in `output_parameters` must exist. Salesforce deploys a \
missing one without complaining and then throws the value away.

## Action Calls

An `ActionCall` is any invocable action: an Email Alert, Send Email, an Apex \
`@InvocableMethod`, Post to Chatter, Submit for Approval, a Quick Action, an \
External Service operation. `action_name` is checked against the org's real \
actions the same way a component's signature is.

**Never invent an action name.** An Email Alert's name, an Apex class's \
invocable name, and an External Service operation are all org-specific - \
guessing one produces a flow that deploys and then fails to run, or does not \
deploy at all. Use one only when the user names it, or when an imported flow \
already had it. `emailSimple` (`action_type` `emailSimple`) is the one \
exception: it is a standard action present in every org, so it is always safe \
to reach for when the request needs a simple notification email and does not \
name a specific Email Alert or template. If the request needs an action you \
cannot name - a specific Apex integration, an approval process, an External \
Service - do not guess at its name. Either ask for it in a way the flow can \
express (an Email Alert the user names, `emailSimple` for a plain email), or \
build the closest thing that will actually deploy (an Email Alert, a Create \
Task, a Chatter post) and say so plainly in the flow's `description`, naming \
what was substituted and for what.

## Options for a picker

The four picker types show options, and every option must be defined as a \
resource that the field names in `choice_references`. There are two kinds.

- `choices` - a fixed option you write out. `choice_text` is what the user sees; \
`value` is what selecting it stores, and defaults to the text. Use these when \
the options are a known short list.
- `dynamic_choice_sets` - options built when the flow runs, in one of two ways, \
never both:
  - From records: `object`, `display_field` (what the user sees), and \
`value_field` (what is stored, usually `Id`). Optionally `filters`, `sort_field`, \
`sort_order` and `limit`. Use this for "pick an Account", "choose one of the \
open Cases".
  - From a picklist: `picklist_object` and `picklist_field`, with the choice \
set's own `data_type` set to `Picklist`. Use this when the options should be \
exactly the values already defined on a picklist field, so they stay in step \
with the field.

Choices and choice sets are resources, not elements: nothing connects to them \
and they need no `next`. Their names share the one namespace with elements, \
variables and screen fields.

A picker with no `choice_references` shows the user an empty list, so the IR \
refuses it. Define the options first, then name them.

## Not your decision

Leave `status` and `api_version` alone. They are deployment policy and are set \
by the tool, not by you. Never set `status` to Active.

## How to work

Use the labels the user's own domain uses, not generic ones. Give each element a \
label a Salesforce admin would recognise on the canvas.

Prefer the smallest flow that does what was asked. Do not add error handling, \
logging, or extra branches that were not requested.

When the request is ambiguous in a way that changes the logic, pick the reading a \
careful Salesforce admin would take and state the assumption in the flow's \
`description`. Do not invent fields or objects you were not given - if you must \
guess an API name, say so in the description.
"""


EXPLAIN_PROMPT = """\
You explain Salesforce Flows to admins, reading the flow's IR document.

The IR is the same structure as the flow itself: `start` is the trigger, \
`elements` are the steps, and `next` is the connector between them. A `next` of \
null means the path ends there. A Decision's own `next` is its default path; \
each outcome has its own.

Write for someone who will have to maintain this. Lead with what the flow is \
for, then walk the paths in the order they run. Name the objects and fields it \
touches. Where the flow does something a reader would not expect from its name, \
say so.

Be concrete and brief. No headings for a short flow, no restating the JSON, no \
preamble about what you are about to do.
"""


KB_CHAT_PROMPT = """\
You answer questions about a Salesforce org, reading a Markdown knowledge-base \
document generated from its metadata (objects, fields, flows, Apex, LWC/Aura). \
The document is fixed context, not something the user can edit - treat it as \
the only source of truth about this org.

Cite the file paths or section headings the document uses when you point to \
something specific, so the answer can be checked against the source. If the \
document does not contain what is being asked, say so plainly rather than \
guessing or answering from general Salesforce knowledge.

Be concrete and brief. No restating the question, no preamble.
"""


OBJECT_SYSTEM_PROMPT = """\
You translate a description of data structure into a Salesforce Custom Object IR \
document.

You produce IR only, never metadata XML - a compiler generates that from your IR.

- `api_name` is suffixed `__c` automatically; write it without the suffix or \
with it, either is fine.
- `label` is singular ("Invoice"), `plural_label` is plural ("Invoices").
- `record_name_type` is `Text` for an ordinary Name field, or `AutoNumber` for \
one Salesforce generates - which requires `record_name_display_format` \
(for example `INV-{0000}`).
- Prefer `sharing_model: ReadWrite` unless the request specifically asks for \
tighter or looser sharing.
- Do not invent fields here - a Custom Object in this IR is the object shell \
alone. Fields are a separate request.
"""


FIELD_SYSTEM_PROMPT = """\
You translate a description of a data field into a Salesforce Custom Field IR \
document.

You produce IR only, never metadata XML - a compiler generates that from your IR.

- `api_name` is suffixed `__c` automatically; write it without the suffix or \
with it, either is fine.
- `object_api_name` names the object this field is added to, and here the \
suffix is **not** automatic - get it right: a standard object is written \
exactly as Salesforce names it, with no suffix at all ("Account", "Case", \
"Contact", "Opportunity"). A custom object - one this same request is also \
creating, or one that already exists in the org - must be written with its \
`__c` suffix ("Invoice__c"). Writing a standard object with a `__c` suffix \
names an object that does not exist, and the deploy fails on a field that \
looks otherwise correct.
- `type` is one of Text, Number, Checkbox, Picklist, Lookup, MasterDetail. \
Each carries only the properties that belong to it - do not set `length` on \
anything but Text, `precision`/`scale` on anything but Number, \
`picklist_values` on anything but Picklist, or `reference_to` on anything but \
Lookup/MasterDetail.
- A Picklist needs at least one value in `picklist_values`, written exactly as \
the user should see them.
- A Lookup or MasterDetail needs `reference_to` naming the target object. A \
MasterDetail field is always required by Salesforce - never set `required` on \
one.
- A Checkbox always needs a `default_value` of `"true"` or `"false"` - \
Salesforce refuses one left blank.
- Do not invent a target object for a Lookup/MasterDetail that the request did \
not name or imply.
"""


APEX_SYSTEM_PROMPT = """\
You translate a description of server-side logic into a Salesforce Apex Class \
IR document.

You produce IR only, never metadata XML - a compiler generates the deployable \
files from your IR. Unlike the Flow, Object and Field IRs, `body` is not \
structured - it is the class source itself, written out in full.

- `api_name` is the class name, and the class you declare in `body` (`public \
class Foo { ... }`) must use exactly that name - the deployed file is named \
after api_name, so a mismatch fails to deploy.
- Write complete, compilable Apex: balanced braces, a real class declaration, \
no placeholders or "TODO" left where logic belongs.
- Prefer the smallest class that does what was asked. Do not add error \
handling, logging, or methods that were not requested.
- Do not invent a reference to a field, object or another class the request \
did not name or clearly imply - a guessed API name compiles today and breaks \
the moment it is wrong.
- No compiler runs against this output before it is checked here - only a \
brace-balance and class-name sanity check. Write it as if it must be correct \
the first time.
"""

TRIGGER_SYSTEM_PROMPT = """\
You translate a description of a change to a Salesforce Apex Trigger into an \
ApexTrigger IR document.

You produce IR only, never metadata XML - a compiler generates the deployable \
files from your IR. Like the Apex Class IR, `body` is not structured - it is \
the trigger source itself, written out in full, including its `trigger <Name> \
on <Object> (<events>)` declaration.

- `api_name` is the trigger name, and the trigger you declare in `body` \
(`trigger Foo on Account (before insert) { ... }`) must use exactly that name \
- the deployed file is named after api_name, so a mismatch fails to deploy.
- The object and event list live only in that declaration line - there is no \
separate field for them, so do not invent one.
- Write complete, compilable Apex: balanced braces, a real trigger \
declaration, no placeholders or "TODO" left where logic belongs.
- Prefer the smallest change that does what was asked. Do not add error \
handling, logging, or logic that was not requested.
- Do not invent a reference to a field, object or class the request did not \
name or clearly imply - a guessed API name compiles today and breaks the \
moment it is wrong.
- No compiler runs against this output before it is checked here - only a \
brace-balance and declaration sanity check. Write it as if it must be correct \
the first time.
"""


LWC_SYSTEM_PROMPT = """\
You translate a description of a UI component into a Salesforce Lightning Web \
Component IR document.

You produce IR only, never a .js-meta.xml file - a compiler generates the \
deployable files (including the meta.xml sidecar) from your IR's structured \
`is_exposed`/`targets`/`api_version` fields. `js` and `html` are not \
structured, though - they are the component's source files themselves, \
written out in full, the same way ApexClass's `body` is.

- `api_name` is the component name in camelCase (e.g. `contactCard`), \
starting with a lowercase letter, letters and digits only - no underscores or \
spaces. It doubles as both the file/folder name and the HTML tag \
(`myComponent` -> `<c-my-component>`), so get the case right.
- `js` must `export default class <PascalCase api_name> extends \
LightningElement { ... }` (import `LightningElement` from `lwc`, along with \
`api`/`track`/`wire` as needed) - the class name must be the exact PascalCase \
of `api_name`.
- `html` must be a single root `<template> ... </template>` containing the \
component's markup.
- `css` is optional - only write it if the component needs styling beyond \
Salesforce's base styling.
- Set `is_exposed: true` and list the relevant entries in `targets` (e.g. \
`lightning__RecordPage`, `lightning__AppPage`, `lightning__HomePage`) only if \
the request implies the component should be placed via App Builder/Flow \
Builder; a component meant to be used only from other components' markup \
should stay unexposed with no targets.
- If the component calls into Apex (`@wire`/imperative `import someMethod \
from '@salesforce/apex/SomeClass.someMethod'`), only reference a class and \
method the request actually named or implied - never invent one.
- Write complete, working code: balanced braces/tags, no placeholders or \
"TODO" left where logic belongs. Prefer the smallest component that does what \
was asked - do not add error handling, loading states, or styling that were \
not requested.
- No compiler runs against this output before it is checked here - only a \
brace/tag-balance and name-matching sanity check. Write it as if it must be \
correct the first time.
"""


# --------------------------------------------------------------------------
# Anthropic provider
# --------------------------------------------------------------------------


class AnthropicProvider:
    """Uses schema-constrained structured outputs, so the shape is guaranteed."""

    name = "anthropic"

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: str = "claude-opus-5",
        effort: str = "medium",
        max_tokens: int = 16000,
    ):
        try:
            import anthropic
        except ImportError as exc:
            raise LLMError(
                "The anthropic package is required for this provider:\n"
                "    pip install anthropic"
            ) from exc

        key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        self._client = anthropic.Anthropic(api_key=key) if key else anthropic.Anthropic()
        self.model = model
        self.effort = effort
        self.max_tokens = max_tokens
        self.usage = Usage()

    def _record(self, response) -> None:
        usage = getattr(response, "usage", None)
        if usage is None:
            return
        self.usage.add(
            input_tokens=getattr(usage, "input_tokens", 0),
            output_tokens=getattr(usage, "output_tokens", 0),
            cached_input_tokens=getattr(usage, "cache_read_input_tokens", 0) or 0,
        )
        log.info("%s %s -> %s", self.name, self.model, self.usage)

    # The SDK resolves credentials lazily, so listing is the first place a
    # missing key shows up - as the same TypeError the completion path handles.
    _NO_KEY = (
        "No Anthropic credentials found. Set one of:\n"
        "    $env:ANTHROPIC_API_KEY = 'sk-ant-...'   (PowerShell)\n"
        "    ant auth login                          (OAuth profile)"
    )

    def list_models(self) -> List[str]:
        try:
            return [m.id for m in self._client.models.list(limit=100)]
        except TypeError as exc:
            if "authentication method" not in str(exc):
                raise
            raise LLMError(self._NO_KEY) from exc

    def _create(self, **kwargs):
        """
        `messages.create`, with credential/rate-limit/model errors turned into
        LLMError. Shared by complete_json and complete_text so a fix to one
        covers both - complete_text used to skip all of this and let a
        missing key surface as a raw TypeError instead of a message anyone
        could act on, which FastAPI then turned into an opaque 500.
        """
        import anthropic

        try:
            return self._client.messages.create(
                model=self.model, max_tokens=self.max_tokens, **kwargs
            )
        except TypeError as exc:
            # The SDK resolves credentials lazily, so a missing key surfaces
            # here as a TypeError rather than at construction.
            if "authentication method" not in str(exc):
                raise
            raise LLMError(self._NO_KEY) from exc
        except anthropic.AuthenticationError as exc:
            raise LLMError("Anthropic rejected the API key.") from exc
        except anthropic.NotFoundError as exc:
            raise LLMError(
                f"Model {self.model!r} was not found. Check the id, or pass --model."
            ) from exc
        except anthropic.RateLimitError as exc:
            retry_after = exc.response.headers.get("retry-after", "60")
            raise LLMError(f"Rate limited. Retry in {retry_after}s.") from exc
        except anthropic.APIConnectionError as exc:
            raise LLMError(f"Could not reach the Anthropic API: {exc}") from exc
        except anthropic.APIStatusError as exc:
            raise LLMError(f"Anthropic API error {exc.status_code}: {exc.message}") from exc

    def complete_json(
        self, system: str, messages: List[Message], schema: Dict[str, Any]
    ) -> Dict[str, Any]:
        response = self._create(
            # The system prompt is byte-identical across requests, so it caches.
            system=[
                {
                    "type": "text",
                    "text": system,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            messages=[{"role": m.role, "content": m.content} for m in messages],
            thinking={"type": "adaptive"},
            output_config={
                "effort": self.effort,
                "format": {"type": "json_schema", "schema": strict_schema(schema)},
            },
        )
        self._record(response)

        if response.stop_reason == "refusal":
            raise LLMError("The model declined this request.")
        if response.stop_reason == "max_tokens":
            raise LLMError(
                "The model ran out of output tokens before finishing the IR. "
                "Raise max_tokens or split the flow into subflows."
            )

        text = next((b.text for b in response.content if b.type == "text"), None)
        if not text:
            raise LLMError("The model returned no JSON.")

        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise LLMError(f"The model returned malformed JSON: {exc}") from exc

    def complete_text(self, system: str, messages: List[Message]) -> str:
        response = self._create(
            system=[
                {"type": "text", "text": system,
                 "cache_control": {"type": "ephemeral"}}
            ],
            messages=[{"role": m.role, "content": m.content} for m in messages],
            thinking={"type": "adaptive"},
            output_config={"effort": self.effort},
        )
        self._record(response)
        if response.stop_reason == "refusal":
            raise LLMError("The model declined this request.")
        return "".join(b.text for b in response.content if b.type == "text").strip()


# --------------------------------------------------------------------------
# Gemini provider
# --------------------------------------------------------------------------

_GEMINI_THINKING = {
    "medium": "MEDIUM",
    "high": "HIGH",
}

# Seconds to wait between retries of a Gemini 503 ("high demand"), one entry
# per attempt. Short and few: this is a fallback for a transient blip, not a
# substitute for the user retrying by hand if the model is down for a while.
_GEMINI_SERVER_ERROR_BACKOFF = (2, 5, 10)

# How many times to wait out a 429 once every configured key has hit it -
# rotating keys only helps for as long as there is an untried one; after
# that, the only thing left to do with a free-tier quota (as low as 5
# requests/minute) is wait for it to reset, which is exactly what Google's
# own "retry in Ns" tells us to do.
_GEMINI_RATE_LIMIT_RETRIES = 2

# Google's message reads "...Please retry in 31.559251234s." - this is the
# fallback wait when that can't be parsed out of it (an API change, an
# unexpected message shape), not the common case.
_GEMINI_RATE_LIMIT_FALLBACK_WAIT = 20.0

_RETRY_DELAY_RE = re.compile(r"retry in\s+([\d.]+)\s*s", re.IGNORECASE)


def _parse_retry_delay(message: Optional[str]) -> float:
    match = _RETRY_DELAY_RE.search(message or "")
    if not match:
        return _GEMINI_RATE_LIMIT_FALLBACK_WAIT
    # Google's own number is precise to the microsecond; round up so a retry
    # fired right at the edge doesn't land a moment early and repeat the wait.
    return math.ceil(float(match.group(1)))

# Gemini caps how large a response_json_schema may be, and it counts the schema
# with every $ref inlined - so a definition referenced from six places costs six
# times. Over the cap the whole request is rejected with a flat
# "Request contains an invalid argument", which names neither the schema nor the
# size, so the cause has to be recorded here instead.
#
# Measured against the API by bisection: 220 expanded properties is accepted,
# 221 is rejected. Neither the byte size, the number of $defs, nor the nesting
# depth predicts it - a 23,000-character schema with 216 expanded properties is
# fine while an 18,000-character one with 221 is not. The margin below the
# measured limit is deliberate: the cap is undocumented and may move.
GEMINI_PROPERTY_BUDGET = 210

# Substrings that mark a model as something other than a text generator. Gemini
# lists images, speech, video and embeddings alongside the models that can write
# a flow, and several of them accept generateContent.
_GEMINI_NOT_TEXT = (
    "embedding", "image", "tts", "-live", "lyria", "veo", "imagen",
    "nano-banana", "aqa", "learnlm",
    # Accept generateContent but are built for something else entirely.
    "robotics", "computer-use",
)


_VERSION = re.compile(r"(\d+(?:\.\d+)?)")


def _descending(name: str):
    """
    Sort key that puts later-looking names first.

    Version numbers compare as numbers, including the decimal: split on digits
    alone and "3.5" becomes 3, ".", 5, which sorts below "3-preview" because
    "-" precedes "." in ASCII. That put gemini-3.5 under gemini-3.
    """
    parts = _VERSION.split(name)
    return [
        (-float(part), "") if _VERSION.fullmatch(part) else (0.0, part)
        for part in parts
    ]


def _unfenced(text: str) -> str:
    """
    Strip a ```json fence if there is one.

    A response schema guarantees bare JSON; the prompt-text fallback only asks
    for it, and a model asked for JSON in prose sometimes fences it anyway.
    """
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped
    body = stripped.split("\n", 1)[1] if "\n" in stripped else ""
    return body.rsplit("```", 1)[0].strip()


def expanded_property_count(schema: Dict[str, Any]) -> int:
    """
    Properties in the schema as Gemini counts them: every $ref replaced by what
    it points at, so shared definitions are counted once per reference.

    A definition already being expanded is not expanded again, so a recursive
    schema terminates rather than counting forever.
    """
    defs = schema.get("$defs", {}) or {}

    def walk(node: Any, seen: frozenset) -> int:
        if isinstance(node, list):
            return sum(walk(item, seen) for item in node)
        if not isinstance(node, dict):
            return 0
        ref = node.get("$ref")
        if ref:
            name = ref.split("/")[-1]
            if name in seen or name not in defs:
                return 0
            return walk(defs[name], seen | {name})
        total = 0
        for key, value in node.items():
            if key == "$defs":
                continue
            if key == "properties" and isinstance(value, dict):
                total += len(value)
                for sub in value.values():
                    total += walk(sub, seen)
                continue
            total += walk(value, seen)
        return total

    return walk({k: v for k, v in schema.items() if k != "$defs"}, frozenset())


def _gemini_keys() -> List[str]:
    """
    Every Gemini API key this build knows about, in order: GEMINI_API_KEY (or
    GOOGLE_API_KEY as the older name for the same thing), then GEMINI_API_KEY2,
    GEMINI_API_KEY3, ... for as long as they are set, no gaps. Numbered past
    the first because the free-tier rate limit is per Google Cloud project -
    a second key only helps if it belongs to a different project.
    """
    keys = []
    primary = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if primary:
        keys.append(primary)
    n = 2
    while True:
        extra = os.environ.get(f"GEMINI_API_KEY{n}")
        if not extra:
            break
        keys.append(extra)
        n += 1
    return keys


class GeminiProvider:
    """
    Uses `response_json_schema`, which accepts real JSON Schema, rather than
    `response_schema`, which only accepts a subset of OpenAPI and mangles
    documents with `$defs` and unions.
    """

    name = "gemini"

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: str = "gemini-3.6-flash",
        effort: str = "medium",
        # Gemini counts thinking towards the output cap, so this needs more
        # headroom than a provider that bills thinking separately.
        max_tokens: int = 48000,
    ):
        try:
            from google import genai
        except ImportError as exc:
            raise LLMError(
                "The google-genai package is required for this provider:\n"
                "    pip install google-genai"
            ) from exc

        keys = [api_key] if api_key else _gemini_keys()
        if not keys:
            raise LLMError(
                "No Gemini credentials found. Set one of:\n"
                "    $env:GEMINI_API_KEY = '...'   (PowerShell)\n"
                "    $env:GOOGLE_API_KEY = '...'"
            )
        self._clients = [genai.Client(api_key=key) for key in keys]
        # Starting from a random key, not always the first, so that many
        # separate requests (a fresh provider each - see build_provider in
        # server.py) spread their load across every configured key from the
        # start, instead of key 1 alone absorbing every request until it
        # trips a rate limit and only then spilling over to key 2. Rotation
        # on a hit (see _generate) still always moves forward from wherever
        # this started.
        self._key_index = random.randrange(len(self._clients))
        self.model = model
        self.effort = effort
        self.max_tokens = max_tokens
        self.usage = Usage()
        self._warned_about_budget = False
        # Set while _generate is waiting out a rate limit or a transient
        # server error, cleared the moment it stops - a caller polling a
        # background job (server.py) reads this to tell the browser "still
        # working, here's why" instead of a silent wait that looks the same
        # as a hang. None the rest of the time.
        self.retry_status: Optional[Dict[str, Any]] = None
        # A plan can run several steps concurrently against this one
        # provider (see planner.execute_plan) - guards _key_index, which a
        # bare `+=` would corrupt under real concurrent access, the same
        # concern Usage.add() has. Held only around the state mutation
        # itself, never around the network call, so concurrent generations
        # still genuinely run in parallel.
        self._key_lock = threading.Lock()

    @property
    def _client(self):
        with self._key_lock:
            return self._clients[self._key_index]

    def _generate(self, **kwargs):
        """
        Runs generate_content, rotating to the next untried Gemini key on a
        rate limit and retrying with backoff on a server error. Free-tier
        limits are per Google Cloud project, not per key - two keys under the
        same project share one quota - so key rotation only helps when
        GEMINI_API_KEY2 (GEMINI_API_KEY3, ...) genuinely belongs to a
        different project. Once every key has hit the limit (including the
        common case of just one key configured), the only thing left to do
        is wait out the delay Google's own error names and try again - see
        _GEMINI_RATE_LIMIT_RETRIES. Anything else is raised straight through;
        the caller's own except clauses handle those.
        """
        from google.genai import errors

        server_error_attempt = 0
        keys_tried = 0
        rate_limit_attempt = 0
        while True:
            try:
                result = self._client.models.generate_content(**kwargs)
                self.retry_status = None
                return result
            except errors.ClientError as exc:
                rate_limited = exc.code == 429 or exc.status == "RESOURCE_EXHAUSTED"
                if not rate_limited:
                    self.retry_status = None
                    raise

                keys_tried += 1
                if keys_tried < len(self._clients):
                    # An untried key remains - wrap around from wherever the
                    # random starting index landed, rather than assuming key
                    # 1 is next just because it's index 0.
                    with self._key_lock:
                        self._key_index = (self._key_index + 1) % len(self._clients)
                    log.warning(
                        "Gemini key rate-limited (%d/%d tried), switching key",
                        keys_tried, len(self._clients),
                    )
                    self.retry_status = {
                        "reason": "rate_limited",
                        "message": f"Rate limited ({keys_tried}/{len(self._clients)} keys "
                                   "tried) - switching key and retrying.",
                    }
                    continue

                # Every key has now hit the limit. Wait out the delay Google
                # itself gave us (parsed from the message; a documented
                # RetryInfo field would be more precise, but the message is
                # what has actually been observed and is stable to parse).
                if rate_limit_attempt >= _GEMINI_RATE_LIMIT_RETRIES:
                    self.retry_status = None
                    raise
                wait = _parse_retry_delay(exc.message)
                rate_limit_attempt += 1
                keys_tried = 0
                log.warning(
                    "Gemini rate limit on every key (attempt %d/%d), retrying in %ss: %s",
                    rate_limit_attempt, _GEMINI_RATE_LIMIT_RETRIES, wait, exc.message,
                )
                self.retry_status = {
                    "reason": "rate_limited",
                    "message": f"Rate limited on every configured key (attempt "
                               f"{rate_limit_attempt}/{_GEMINI_RATE_LIMIT_RETRIES}) - "
                               f"retrying in {wait:.0f}s.",
                    "wait": wait,
                }
                time.sleep(wait)
            except errors.ServerError as exc:
                # "This model is currently experiencing high demand" (503) is
                # Google's own wording for a transient condition, not a real
                # failure - a short retry clears most of them, which matters
                # a lot more here than in a single-generation flow: a plan
                # with several steps loses every step after this one to a
                # failure the very next request would likely not have hit.
                if server_error_attempt >= len(_GEMINI_SERVER_ERROR_BACKOFF):
                    self.retry_status = None
                    raise
                wait = _GEMINI_SERVER_ERROR_BACKOFF[server_error_attempt]
                server_error_attempt += 1
                log.warning(
                    "Gemini server error (attempt %d/%d), retrying in %ss: %s",
                    server_error_attempt, len(_GEMINI_SERVER_ERROR_BACKOFF),
                    wait, exc.message,
                )
                self.retry_status = {
                    "reason": "server_error",
                    "message": f"Gemini is experiencing high demand (attempt "
                               f"{server_error_attempt}/{len(_GEMINI_SERVER_ERROR_BACKOFF)}) - "
                               f"retrying in {wait}s.",
                    "wait": wait,
                }
                time.sleep(wait)

    def _record(self, response) -> None:
        meta = getattr(response, "usage_metadata", None)
        if meta is None:
            return
        self.usage.add(
            input_tokens=getattr(meta, "prompt_token_count", 0) or 0,
            output_tokens=getattr(meta, "candidates_token_count", 0) or 0,
            cached_input_tokens=getattr(meta, "cached_content_token_count", 0) or 0,
            thinking_tokens=getattr(meta, "thoughts_token_count", 0) or 0,
        )
        log.info("%s %s -> %s", self.name, self.model, self.usage)

    def list_models(self) -> List[str]:
        """
        Text models this key can use, newest-looking first.

        The raw list also carries image, speech and embedding models, none of
        which can produce a flow - offering them would only invite a confusing
        failure several seconds later.
        """
        models = []
        for model in self._client.models.list():
            name = (model.name or "").removeprefix("models/")
            if not name or "generateContent" not in (model.supported_actions or []):
                continue
            if any(word in name for word in _GEMINI_NOT_TEXT):
                continue
            models.append(name)

        # The default first, then the Gemini line newest-looking first, then
        # everything else. Plain reverse-alphabetical buries gemini-3.6 under
        # gemma-4, which is the one thing the list must not do.
        def order(name: str):
            return (name != self.model, not name.startswith("gemini-"), _descending(name))

        return sorted(models, key=order)

    def _available_models(self) -> List[str]:
        try:
            return self.list_models()
        except Exception:  # listing is a nicety; never mask the original error
            return []

    def _contents(self, messages: List[Message]):
        from google.genai import types

        # Gemini names the assistant role "model", not "assistant".
        return [
            types.Content(
                role="user" if message.role == "user" else "model",
                parts=[types.Part(text=message.content)],
            )
            for message in messages
        ]

    def complete_text(self, system: str, messages: List[Message]) -> str:
        from google.genai import errors, types

        config = types.GenerateContentConfig(
            system_instruction=system,
            max_output_tokens=self.max_tokens,
            thinking_config=types.ThinkingConfig(
                thinking_level=_GEMINI_THINKING.get(self.effort, "HIGH")
            ),
        )
        try:
            response = self._generate(
                model=self.model, contents=self._contents(messages), config=config
            )
        except errors.APIError as exc:
            raise LLMError(f"Gemini API error: {exc.message}") from exc

        self._record(response)
        text = response.text
        if not text:
            raise LLMError("Gemini returned no text.")
        return text.strip()

    def complete_json(
        self, system: str, messages: List[Message], schema: Dict[str, Any]
    ) -> Dict[str, Any]:
        from google.genai import errors, types

        contents = self._contents(messages)
        dialect = gemini_schema(schema)

        # Over Gemini's cap the request is rejected outright, so the schema goes
        # into the prompt instead of the response_json_schema field. That is a
        # weaker guarantee - the model is asked to follow the shape rather than
        # held to it - but it is the only lossless option. Pruning the schema to
        # fit would make whatever was pruned unrepresentable, and refining an
        # imported flow would then delete exactly those parts on the way back
        # out. Everything is validated against the real IR either way, and the
        # repair loop feeds any mistake back.
        cost = expanded_property_count(dialect)
        constrained = cost <= GEMINI_PROPERTY_BUDGET
        if not constrained:
            # Once per provider, not once per repair round: it is a fact about
            # the build, and repeating it buries the errors that are not.
            if not self._warned_about_budget:
                self._warned_about_budget = True
                log.warning(
                    "schema is %d expanded properties, over Gemini's limit of "
                    "~%d - sending it as text instead of a response schema. "
                    "Output is validated and repaired as usual, but expect more "
                    "repair rounds.",
                    cost, GEMINI_PROPERTY_BUDGET,
                )
            system = (
                f"{system}\n\n## The exact shape to return\n\n"
                "Return a single JSON object matching this JSON Schema. Return "
                "nothing else - no prose, no code fence.\n\n"
                f"{json.dumps(dialect)}"
            )

        config = types.GenerateContentConfig(
            system_instruction=system,
            response_mime_type="application/json",
            response_json_schema=dialect if constrained else None,
            max_output_tokens=self.max_tokens,
            thinking_config=types.ThinkingConfig(
                thinking_level=_GEMINI_THINKING.get(self.effort, "HIGH")
            ),
        )

        try:
            response = self._generate(
                model=self.model, contents=contents, config=config
            )
        except errors.ClientError as exc:
            if exc.status == "NOT_FOUND" or "not found" in str(exc.message).lower():
                available = self._available_models()
                hint = f"\nModels available to this key:\n  " + "\n  ".join(available) \
                    if available else ""
                raise LLMError(f"Model {self.model!r} is not available.{hint}") from exc
            # "Request contains an invalid argument" is all the API says when the
            # response schema is too large - it names neither the schema nor the
            # size. If that happens while a schema was attached, the cap has
            # moved below the budget above, so say so rather than passing on a
            # message nobody can act on.
            if constrained and "invalid argument" in str(exc.message).lower():
                raise LLMError(
                    f"Gemini rejected the response schema ({cost} expanded "
                    f"properties, believed to be within its limit of "
                    f"~{GEMINI_PROPERTY_BUDGET}). The limit is undocumented and "
                    "appears to have moved: lower GEMINI_PROPERTY_BUDGET in "
                    "flowtool/llm.py and the schema will be sent as prompt text "
                    "instead."
                ) from exc
            raise LLMError(f"Gemini rejected the request: {exc.message}") from exc
        except errors.ServerError as exc:
            raise LLMError(f"Gemini server error: {exc.message}") from exc
        except errors.APIError as exc:
            raise LLMError(f"Gemini API error: {exc.message}") from exc

        self._record(response)

        reason = ""
        if response.candidates:
            reason = str(response.candidates[0].finish_reason or "")

        # A truncated response still has text, so this has to be checked before
        # parsing - otherwise json.loads fails on a half-written document and
        # the real cause (the token cap) never surfaces.
        if "MAX_TOKEN" in reason.upper():
            raise LLMError(
                f"Gemini hit its {self.max_tokens}-token output cap before "
                "finishing the IR. Thinking counts towards that cap, so either "
                "lower the effort or raise max_tokens."
            )
        text = response.text
        if not text:
            raise LLMError(f"Gemini returned no JSON (finish reason: {reason or 'unknown'}).")

        try:
            return json.loads(_unfenced(text))
        except json.JSONDecodeError as exc:
            raise LLMError(
                f"Gemini returned malformed JSON (finish reason: {reason or 'unknown'}): "
                f"{exc}"
            ) from exc


# --------------------------------------------------------------------------
# Ollama provider
# --------------------------------------------------------------------------

_OLLAMA_THINK = {
    "medium": "medium",
    "high": "high",
}

# Cloud by default - api.ollama.com, not the local daemon - but OLLAMA_HOST
# still overrides it, so a local Ollama works by setting that one variable
# rather than passing a provider option nothing else exposes.
_OLLAMA_CLOUD_HOST = "https://api.ollama.com"


class OllamaProvider:
    """
    Talks to Ollama Cloud (or a local Ollama) over `/api/chat`. `format` takes
    a raw JSON Schema and is enforced by grammar-constrained decoding - the
    same shape guarantee Anthropic and Gemini give, from an open-weights model
    neither of them is.
    """

    name = "ollama"

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: str = "gpt-oss:120b-cloud",
        effort: str = "medium",
        max_tokens: int = 16000,
        host: Optional[str] = None,
    ):
        try:
            import ollama
        except ImportError as exc:
            raise LLMError(
                "The ollama package is required for this provider:\n"
                "    pip install ollama"
            ) from exc

        key = api_key or os.environ.get("OLLAMA_API_KEY")
        headers = {"Authorization": f"Bearer {key}"} if key else {}
        self._ollama = ollama
        self._client = ollama.Client(
            host=host or os.environ.get("OLLAMA_HOST") or _OLLAMA_CLOUD_HOST,
            headers=headers,
        )
        self.model = model
        self.effort = effort
        self.max_tokens = max_tokens
        self.usage = Usage()

    def _record(self, response) -> None:
        self.usage.add(
            input_tokens=response.prompt_eval_count or 0,
            output_tokens=response.eval_count or 0,
        )
        log.info("%s %s -> %s", self.name, self.model, self.usage)

    def list_models(self) -> List[str]:
        try:
            return sorted(m.model for m in self._client.list().models if m.model)
        except Exception:  # listing is a nicety; never mask the original error
            return []

    def _chat(self, **kwargs):
        try:
            return self._client.chat(
                model=self.model,
                think=_OLLAMA_THINK.get(self.effort, "high"),
                options={"num_predict": self.max_tokens},
                **kwargs,
            )
        except self._ollama.ResponseError as exc:
            if exc.status_code == 401:
                raise LLMError("Ollama rejected the API key.") from exc
            if exc.status_code == 404:
                available = self.list_models()
                hint = (
                    "\nModels available to this key:\n  " + "\n  ".join(available)
                    if available else ""
                )
                raise LLMError(f"Model {self.model!r} was not found.{hint}") from exc
            if exc.status_code == 429:
                raise LLMError("Rate limited by Ollama. Try again shortly.") from exc
            raise LLMError(f"Ollama API error {exc.status_code}: {exc.error}") from exc
        except ConnectionError as exc:
            raise LLMError(f"Could not reach Ollama: {exc}") from exc
        except self._ollama.RequestError as exc:
            raise LLMError(str(exc)) from exc

    def complete_json(
        self, system: str, messages: List[Message], schema: Dict[str, Any]
    ) -> Dict[str, Any]:
        response = self._chat(
            messages=[{"role": "system", "content": system}]
            + [{"role": m.role, "content": m.content} for m in messages],
            # Reuses the Anthropic dialect. Untested against llama.cpp's own
            # schema-to-grammar compiler, but closing every object and
            # dropping the same handful of validation keywords can only
            # narrow what the model may emit - Pydantic still enforces them
            # on the way back in either way, so a wrong guess costs nothing
            # but an extra repair round, never a false accept.
            format=strict_schema(schema),
        )
        self._record(response)

        text = response.message.content
        if not text:
            raise LLMError("The model returned no JSON.")
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise LLMError(f"The model returned malformed JSON: {exc}") from exc

    def complete_text(self, system: str, messages: List[Message]) -> str:
        response = self._chat(
            messages=[{"role": "system", "content": system}]
            + [{"role": m.role, "content": m.content} for m in messages],
        )
        self._record(response)
        text = response.message.content
        if not text:
            raise LLMError("Ollama returned no text.")
        return text.strip()


# --------------------------------------------------------------------------
# Generator
# --------------------------------------------------------------------------

T = TypeVar("T", bound=BaseModel)


@dataclass
class IRGenerationResult(Generic[T]):
    value: T
    messages: List[Message]
    repairs: int


class IRGenerator(Generic[T]):
    """
    Turns a request into a validated instance of `model_cls`, repairing its own
    mistakes: schema-constrained generate, a Pydantic ValidationError fed back
    verbatim, retry, up to `max_repairs` times.

    This is the engine behind FlowGenerator, factored out so a future artifact
    type (a Custom Object, an Apex class, ...) can reuse the same repair loop
    with its own model, system prompt and checks - subclass and override the
    hooks below rather than duplicating the loop.
    """

    def __init__(
        self,
        provider: Provider,
        model_cls: Type[T],
        system_prompt: str,
        max_repairs: int = DEFAULT_MAX_REPAIRS,
    ):
        self.provider = provider
        self.model_cls = model_cls
        self.system_prompt = system_prompt
        self.max_repairs = max_repairs
        # Handed to the provider raw; each one adapts it to its own dialect.
        self._schema = model_cls.model_json_schema()

    # -- hooks a subclass overrides for its own domain -----------------

    def _tracked_names(self, payload: Dict[str, Any]) -> Optional[set]:
        """
        Names to guard against being silently dropped between repair attempts
        (see the anti-shrink check in `_validated`). Return None to disable
        the guard - the default, since it is specific to a graph of named
        elements like a Flow's.
        """
        return None

    _dropped_item_noun = "items"

    def _extra_error(self, payload: Dict[str, Any]) -> Optional[str]:
        """
        A pre-validation sanity check beyond the schema itself - a hook for a
        domain that cannot lean on Pydantic alone (for example a heuristic
        syntax check on generated Apex). Return an error message to send the
        payload back for another attempt, or None to proceed to validation.
        """
        return None

    def _warnings(self, instance: T) -> List[str]:
        """Non-fatal issues worth one round trip. Default: none."""
        return []

    def _describe(self, instance: T) -> str:
        """One line for the success log line."""
        return type(instance).__name__

    # -- the loop itself -------------------------------------------------

    def _validated(self, messages: List[Message]) -> IRGenerationResult[T]:
        conversation = list(messages)
        last_error: Optional[str] = None
        asked_about_warnings = False
        previous_names: Optional[set] = None

        for attempt in range(self.max_repairs + 1):
            log.info(
                "attempt %s/%s (%s messages)",
                attempt + 1, self.max_repairs + 1, len(conversation),
            )
            payload = self.provider.complete_json(
                self.system_prompt, conversation, self._schema
            )

            # A model repairing a reachability error can "win" by deleting the
            # orphaned elements instead of adding the connector that was asked
            # for - a smaller flow trivially satisfies "everything is
            # reachable". That is never the fix, so catch it before it reaches
            # model_validate, which cannot tell it from a deliberate
            # simplification. Compare counts, not just names - a repair that
            # renames an element (e.g. to fix an invalid API name) changes its
            # name without shrinking the flow, and that is fine.
            #
            # Only names the previous complaint actually named count. A flow
            # can shrink for good reasons - the user asked for a step to go, or
            # two updates were merged into one - and those names are ones no
            # error mentioned. Rejecting every shrink cost a repair round on a
            # legitimate "remove the urgency check" and told the model to put
            # back exactly what it had been asked to delete.
            #
            # The baseline is the previous attempt in this loop, not the flow
            # being refined, so a refinement that removes something is compared
            # against nothing and passes on its first attempt.
            current_names = self._tracked_names(payload)
            dropped = set()
            if current_names is not None and previous_names is not None \
                    and len(current_names) < len(previous_names):
                dropped = {
                    name for name in previous_names - current_names
                    if last_error and name in last_error
                }
            if current_names is not None:
                previous_names = current_names

            if dropped:
                last_error = (
                    f"{self._dropped_item_noun} {sorted(dropped)} are missing "
                    "from this attempt, but were present in the one before it, "
                    "and the error you were given names them. Deleting an "
                    "element is not a fix for a validation error about it - it "
                    "just hides the problem. Restore them and fix what the "
                    "error actually named."
                )
                log.warning("attempt %s rejected: %s", attempt + 1, last_error)
                if attempt == self.max_repairs:
                    break
                conversation.append(
                    Message(role="assistant", content=json.dumps(payload, indent=1))
                )
                conversation.append(Message(role="user", content=last_error))
                continue

            extra_error = self._extra_error(payload)
            if extra_error is not None:
                last_error = extra_error
                log.warning(
                    "attempt %s rejected: %s",
                    attempt + 1, last_error.replace(chr(10), ' | '),
                )
                if attempt == self.max_repairs:
                    break
                conversation.append(
                    Message(role="assistant", content=json.dumps(payload, indent=1))
                )
                conversation.append(
                    Message(
                        role="user",
                        content=(
                            f"That was rejected:\n\n{last_error}\n\n"
                            "Return the corrected version."
                        ),
                    )
                )
                continue

            try:
                instance = self.model_cls.model_validate(payload)
            except ValidationError as exc:
                last_error = _readable_errors(exc)
                log.warning(
                    "attempt %s rejected: %s",
                    attempt + 1, last_error.replace(chr(10), ' | '),
                )
                if attempt == self.max_repairs:
                    break
                # Echo back what it produced, then the exact complaint. Feeding
                # the raw validator output beats paraphrasing it - the messages
                # already name the field and the fix.
                conversation.append(
                    Message(role="assistant", content=json.dumps(payload, indent=1))
                )
                conversation.append(
                    Message(
                        role="user",
                        content=(
                            "That IR failed validation:\n\n"
                            f"{last_error}\n\n"
                            "Return the corrected IR."
                        ),
                    )
                )
                continue

            # Things the org allows but that are usually a mistake - a forgotten
            # connector, most often. Not a validation error, because a flow in
            # production can legitimately have one and refusing it would make
            # that flow unopenable. Worth one round trip when we wrote the flow
            # ourselves, and no more: if the model looks at it and produces the
            # same shape again, it meant it.
            notes = self._warnings(instance)
            if notes and not asked_about_warnings and attempt < self.max_repairs:
                asked_about_warnings = True
                joined = "\n\n".join(notes)
                log.info("attempt %s has warnings: %s", attempt + 1,
                         joined.replace(chr(10), ' | ')[:200])
                conversation.append(
                    Message(role="assistant", content=json.dumps(payload, indent=1))
                )
                conversation.append(
                    Message(
                        role="user",
                        content=(
                            "That IR is valid, but it looks unintended:\n\n"
                            f"{joined}\n\n"
                            "Return the corrected IR. If you did mean it, return "
                            "the same IR unchanged."
                        ),
                    )
                )
                continue

            log.info(
                "valid IR after %s attempt%s: %s",
                attempt + 1, '' if attempt == 0 else 's', self._describe(instance),
            )
            return IRGenerationResult(value=instance, messages=conversation, repairs=attempt)

        raise LLMError(
            f"Could not get valid IR after {self.max_repairs + 1} attempts. "
            f"Last errors:\n{last_error}"
        )

    def refine(
        self, previous: IRGenerationResult[T], instruction: str
    ) -> IRGenerationResult[T]:
        """
        Apply a change to a previously generated instance. The conversation
        continues from where it left off, so the model edits what it already
        produced rather than starting over from the description alone.

        FlowGenerator defines its own version of this (see below) because it
        deals in the legacy GenerationResult (`.flow`), not this class's
        IRGenerationResult (`.value`) - that override shadows this one for
        Flow, so this is what CustomObjectGenerator, CustomFieldGenerator and
        ApexClassGenerator get for free without repeating it.
        """
        conversation = list(previous.messages)
        conversation.append(
            Message(
                role="assistant",
                content=previous.value.model_dump_json(exclude_none=True, indent=1),
            )
        )
        conversation.append(Message(role="user", content=instruction))
        return self._validated(conversation)

    def repair_from_salesforce(
        self, previous: IRGenerationResult[T], failures: List[str]
    ) -> IRGenerationResult[T]:
        """Feed real deploy failures back in - the loop that turns an org's
        rejection into a corrected result rather than a dead end."""
        problems = "\n".join(f"- {failure}" for failure in failures)
        return self.refine(
            previous,
            "Salesforce rejected this with these errors:\n\n"
            f"{problems}\n\n"
            "Correct it so the deploy passes. Change only what these errors require.",
        )

    def adopt(self, value: T, origin: str = "an existing component in the org") -> IRGenerationResult[T]:
        """
        Start a conversation from a value that already exists, so a refinement
        edits it rather than designing something new from its description.
        Shared by any artifact type that supports editing an org original
        (Flow, Apex, ...) - the wording is generic on purpose.
        """
        return IRGenerationResult(
            value=value,
            messages=[
                Message(
                    role="user",
                    content=(
                        f"Here is {origin}. Keep everything about it the same "
                        "unless I ask for a change."
                    ),
                )
            ],
            repairs=0,
        )


@dataclass
class GenerationResult:
    flow: Flow
    messages: List[Message]
    repairs: int


class FlowGenerator(IRGenerator[Flow]):
    """
    Turns a request into a validated Flow, repairing its own mistakes.

    The conversation is kept so a refinement continues from the same context
    rather than re-deriving the flow from scratch.
    """

    def __init__(self, provider: Provider, max_repairs: int = DEFAULT_MAX_REPAIRS):
        super().__init__(provider, Flow, SYSTEM_PROMPT, max_repairs)

    def _tracked_names(self, payload: Dict[str, Any]) -> Optional[set]:
        return {
            e.get("name")
            for e in payload.get("elements", []) or []
            if isinstance(e, dict) and e.get("name")
        }

    _dropped_item_noun = "elements"

    def _warnings(self, flow: Flow) -> List[str]:
        return flow.warnings()

    def _describe(self, flow: Flow) -> str:
        return (
            f"{flow.api_name}, {len(flow.elements)} element"
            f"{'' if len(flow.elements) == 1 else 's'}"
        )

    def _validated(self, messages: List[Message]) -> GenerationResult:
        generic = super()._validated(messages)
        return GenerationResult(
            flow=generic.value, messages=generic.messages, repairs=generic.repairs
        )

    def _stamp_name(self, flow: Flow) -> None:
        """
        Naming policy applied to a flow generated from scratch. Factored out so
        another artifact type can define its own policy (a Custom Object's
        `__c` suffix, say) without touching the repair loop.
        """
        if not flow.api_name.startswith(GENERATED_NAME_PREFIX):
            flow.api_name = GENERATED_NAME_PREFIX + flow.api_name

    def generate(self, request: str) -> GenerationResult:
        result = self._validated([Message(role="user", content=request)])
        self._stamp_name(result.flow)
        return result

    def adopt(self, flow: Flow, origin: str = "an existing flow in the org") -> GenerationResult:
        generic = super().adopt(flow, origin)
        return GenerationResult(
            flow=generic.value, messages=generic.messages, repairs=generic.repairs
        )

    def explain(self, flow: Flow, question: Optional[str] = None) -> str:
        """
        Prose about what a flow does. Reads the IR rather than the XML: it is
        the same information, an order of magnitude smaller, and already
        validated.
        """
        ask = question or (
            "Explain what this flow does, in plain language, for a Salesforce "
            "admin who has never seen it. Cover what triggers it, what it does "
            "on each path, and anything about it that looks risky or surprising. "
            "Do not restate the JSON field by field."
        )
        return self.provider.complete_text(
            EXPLAIN_PROMPT,
            [
                Message(
                    role="user",
                    content=(
                        f"{ask}\n\nFlow IR:\n"
                        f"{flow.model_dump_json(exclude_none=True, indent=1)}"
                    ),
                )
            ],
        )

    def refine(self, previous: GenerationResult, instruction: str) -> GenerationResult:
        """
        Apply a change to an existing flow. The model edits the IR, so the graph
        the user sees and the XML that deploys stay in step automatically.
        """
        conversation = list(previous.messages)
        conversation.append(
            Message(
                role="assistant",
                content=previous.flow.model_dump_json(exclude_none=True, indent=1),
            )
        )
        conversation.append(Message(role="user", content=instruction))
        return self._validated(conversation)

    def repair_from_salesforce(
        self, previous: GenerationResult, failures: List[str]
    ) -> GenerationResult:
        """
        Feed real deploy failures back in. This is the loop that turns an org's
        rejection into a corrected flow rather than a dead end.
        """
        problems = "\n".join(f"- {failure}" for failure in failures)
        return self.refine(
            previous,
            "Salesforce rejected the generated flow with these errors:\n\n"
            f"{problems}\n\n"
            "Correct the IR so the deploy passes. Change only what these errors require.",
        )


class CustomObjectGenerator(IRGenerator[CustomObject]):
    """Turns a description of a data structure into a validated CustomObject."""

    def __init__(self, provider: Provider, max_repairs: int = DEFAULT_MAX_REPAIRS):
        super().__init__(provider, CustomObject, OBJECT_SYSTEM_PROMPT, max_repairs)

    def _describe(self, obj: CustomObject) -> str:
        return f"{obj.api_name} ({obj.label})"

    def generate(self, request: str) -> IRGenerationResult[CustomObject]:
        return self._validated([Message(role="user", content=request)])


class CustomFieldGenerator(IRGenerator[CustomField]):
    """Turns a description of a field into a validated CustomField."""

    def __init__(self, provider: Provider, max_repairs: int = DEFAULT_MAX_REPAIRS):
        super().__init__(provider, CustomField, FIELD_SYSTEM_PROMPT, max_repairs)

    def _describe(self, field: CustomField) -> str:
        return f"{field.api_name} ({field.type}) on {field.object_api_name}"

    def generate(self, request: str) -> IRGenerationResult[CustomField]:
        return self._validated([Message(role="user", content=request)])


class ApexClassGenerator(IRGenerator[ApexClass]):
    """
    Turns a description of server-side logic into a validated ApexClass.

    Validation here has no compiler behind it - see ir_apex.py for why - so
    `_extra_error` is what actually catches most mistakes: brace/paren
    balance and a class declaration matching api_name, checked on the raw
    payload before Pydantic even sees it (Pydantic itself only rejects an
    empty body or a bad api_name, same as any other IR).
    """

    def __init__(self, provider: Provider, max_repairs: int = DEFAULT_MAX_REPAIRS):
        super().__init__(provider, ApexClass, APEX_SYSTEM_PROMPT, max_repairs)

    def _extra_error(self, payload: Dict[str, Any]) -> Optional[str]:
        problems = heuristic_errors(payload.get("api_name") or "", payload.get("body") or "")
        if not problems:
            return None
        return "\n".join(f"- {p}" for p in problems)

    def _describe(self, cls: ApexClass) -> str:
        return f"{cls.api_name}, {len(cls.body.splitlines())} lines"

    def generate(self, request: str) -> IRGenerationResult[ApexClass]:
        return self._validated([Message(role="user", content=request)])

    def explain(self, cls: ApexClass, question: Optional[str] = None) -> str:
        """Prose about what an Apex class does, same reasoning as
        FlowGenerator.explain: reads the source directly, no separate IR to
        translate since the body already is the source."""
        ask = question or (
            "Explain what this Apex class does, in plain language, for a "
            "Salesforce admin who has never seen it. Cover what each public "
            "method does and anything about it that looks risky or surprising. "
            "Do not restate the code line by line."
        )
        return self.provider.complete_text(
            EXPLAIN_PROMPT,
            [Message(role="user", content=f"{ask}\n\nApex class {cls.api_name}:\n\n{cls.body}")],
        )


class ApexTriggerGenerator(IRGenerator[ApexTrigger]):
    """
    Turns a description of a change to a trigger into a validated
    ApexTrigger - same reasoning as ApexClassGenerator, see its docstring.
    """

    def __init__(self, provider: Provider, max_repairs: int = DEFAULT_MAX_REPAIRS):
        super().__init__(provider, ApexTrigger, TRIGGER_SYSTEM_PROMPT, max_repairs)

    def _extra_error(self, payload: Dict[str, Any]) -> Optional[str]:
        problems = heuristic_trigger_errors(payload.get("api_name") or "", payload.get("body") or "")
        if not problems:
            return None
        return "\n".join(f"- {p}" for p in problems)

    def _describe(self, trigger: ApexTrigger) -> str:
        return f"{trigger.api_name}, {len(trigger.body.splitlines())} lines"

    def generate(self, request: str) -> IRGenerationResult[ApexTrigger]:
        return self._validated([Message(role="user", content=request)])

    def explain(self, trigger: ApexTrigger, question: Optional[str] = None) -> str:
        ask = question or (
            "Explain what this Apex trigger does, in plain language, for a "
            "Salesforce admin who has never seen it. Cover which object and "
            "events it fires on, what it does on each path, and anything "
            "about it that looks risky or surprising. Do not restate the "
            "code line by line."
        )
        return self.provider.complete_text(
            EXPLAIN_PROMPT,
            [Message(
                role="user",
                content=f"{ask}\n\nApex trigger {trigger.api_name}:\n\n{trigger.body}",
            )],
        )


class LwcGenerator(IRGenerator[LightningComponent]):
    """
    Turns a description of a UI component into a validated
    LightningComponent - same reasoning as ApexClassGenerator: no compiler
    behind this, so `_extra_error` (brace balance on js, a matching exported
    class name, a well-formed <template> root on html) does most of the real
    catching, on the raw payload before Pydantic even sees it.
    """

    def __init__(self, provider: Provider, max_repairs: int = DEFAULT_MAX_REPAIRS):
        super().__init__(provider, LightningComponent, LWC_SYSTEM_PROMPT, max_repairs)

    def _extra_error(self, payload: Dict[str, Any]) -> Optional[str]:
        problems = lwc_heuristic_errors(
            payload.get("api_name") or "", payload.get("js") or "", payload.get("html") or ""
        )
        if not problems:
            return None
        return "\n".join(f"- {p}" for p in problems)

    def _describe(self, component: LightningComponent) -> str:
        return f"{component.api_name}, {len(component.js.splitlines())} lines of js"

    def generate(self, request: str) -> IRGenerationResult[LightningComponent]:
        return self._validated([Message(role="user", content=request)])

    def explain(self, component: LightningComponent, question: Optional[str] = None) -> str:
        ask = question or (
            "Explain what this Lightning Web Component does, in plain "
            "language, for a Salesforce admin who has never seen it. Cover "
            "what it displays/does, any Apex it calls, and anything about it "
            "that looks risky or surprising. Do not restate the code line by "
            "line."
        )
        source = (
            f"js:\n{component.js}\n\nhtml:\n{component.html}"
            + (f"\n\ncss:\n{component.css}" if component.css else "")
        )
        return self.provider.complete_text(
            EXPLAIN_PROMPT,
            [Message(
                role="user",
                content=f"{ask}\n\nLightning Web Component {component.api_name}:\n\n{source}",
            )],
        )


# Constraints the structured-outputs schema compiler does not accept. They stay
# enforced by Pydantic on the way back in, so dropping them here costs nothing -
# a violation becomes a validation error and goes round the repair loop.
_UNSUPPORTED_KEYWORDS = {
    "minItems",
    "maxItems",
    "minLength",
    "maxLength",
    "minimum",
    "maximum",
    "exclusiveMinimum",
    "exclusiveMaximum",
    "multipleOf",
    "format",
}


# Dicts whose keys are author-chosen names rather than schema keywords. Their
# keys must survive filtering untouched - recursing into them as if they were
# schemas would delete every field in the document.
_NAME_KEYED = {"properties", "$defs", "definitions", "patternProperties"}


def _rewrite(schema: Any, rewrite_entry) -> Any:
    """
    Walk a JSON schema, applying `rewrite_entry(key, value)` to each keyword.
    It returns an iterable of (key, value) pairs to keep, or nothing to drop
    the keyword. Name-keyed containers are recursed into by value only.
    """
    if isinstance(schema, list):
        return [_rewrite(item, rewrite_entry) for item in schema]
    if not isinstance(schema, dict):
        return schema

    result: Dict[str, Any] = {}
    for key, value in schema.items():
        if key in _NAME_KEYED and isinstance(value, dict):
            result[key] = {
                name: _rewrite(sub, rewrite_entry) for name, sub in value.items()
            }
            continue
        for new_key, new_value in rewrite_entry(key, value) or ():
            result[new_key] = _rewrite(new_value, rewrite_entry)

    if result.get("type") == "object" or "properties" in result:
        result.setdefault("additionalProperties", False)
    return result


def strict_schema(schema: Any) -> Any:
    """
    Make a Pydantic JSON schema acceptable to schema-constrained decoding:
    every object closed with `additionalProperties: false`, and unsupported
    validation keywords removed.
    """

    def entry(key: str, value: Any):
        if key in _UNSUPPORTED_KEYWORDS:
            return ()
        return ((key, value),)

    return _rewrite(schema, entry)


# Gemini's `response_json_schema` documents the exact keyword set it accepts.
# Anything outside it is rejected, so the dialects genuinely differ - this is
# not the same normalisation with a different name.
_GEMINI_SUPPORTED = {
    "$id",
    "$defs",
    "$ref",
    "$anchor",
    "type",
    "format",
    "title",
    "description",
    "enum",
    "items",
    "prefixItems",
    "minItems",
    "maxItems",
    "minimum",
    "maximum",
    "anyOf",
    "oneOf",
    "properties",
    "additionalProperties",
    "required",
    "propertyOrdering",
}


def gemini_schema(schema: Any) -> Any:
    """
    Adapt a Pydantic JSON schema to Gemini's supported keyword set.

    Two differences from the Anthropic dialect matter:
      - `const` is not supported, so a discriminator becomes a single-value
        `enum`, which constrains the model identically.
      - `default` and `discriminator` are dropped. Pydantic still applies both
        when validating the response, so nothing is actually lost.
    """

    def entry(key: str, value: Any):
        if key == "const":
            return (("enum", [value]),)
        if key in _GEMINI_SUPPORTED:
            return ((key, value),)
        return ()  # default, discriminator, ...

    return _rewrite(schema, entry)


def _readable_errors(exc: ValidationError) -> str:
    lines = []
    for error in exc.errors():
        location = ".".join(str(part) for part in error["loc"]) or "(root)"
        lines.append(f"- {location}: {error['msg']}")
    return "\n".join(lines)
