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
import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Protocol

from pydantic import ValidationError

from .ir import Flow

DEFAULT_MAX_REPAIRS = 3

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

    def add(
        self,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cached_input_tokens: int = 0,
        thinking_tokens: int = 0,
    ) -> None:
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
- Reference the triggering record as `$Record` (for example `$Record.Amount`), \
and a retrieved record by its element name (for example `Get_Account.Id`).
- Element names, variable names and screen field names share one namespace. \
Each name can be used once in the flow.

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
- A screen's `fields` are, in order, what the user sees. There are three kinds:
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
- Read what the user entered by the field's own name: a field named \
`Customer_Email` is referenced as `Customer_Email`, not as `Screen1.Customer_Email`.
- Lookups and custom LWC or Aura components are not available yet. If the \
request needs one, use the closest kind above and say so in the flow's \
`description` - do not invent a field type.

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
        effort: str = "high",
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

    def complete_json(
        self, system: str, messages: List[Message], schema: Dict[str, Any]
    ) -> Dict[str, Any]:
        import anthropic

        try:
            response = self._client.messages.create(
                model=self.model,
                max_tokens=self.max_tokens,
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
        import anthropic

        try:
            response = self._client.messages.create(
                model=self.model,
                max_tokens=self.max_tokens,
                system=[
                    {"type": "text", "text": system,
                     "cache_control": {"type": "ephemeral"}}
                ],
                messages=[{"role": m.role, "content": m.content} for m in messages],
                thinking={"type": "adaptive"},
                output_config={"effort": self.effort},
            )
        except anthropic.APIStatusError as exc:
            raise LLMError(f"Anthropic API error {exc.status_code}: {exc.message}") from exc

        self._record(response)
        if response.stop_reason == "refusal":
            raise LLMError("The model declined this request.")
        return "".join(b.text for b in response.content if b.type == "text").strip()


# --------------------------------------------------------------------------
# Gemini provider
# --------------------------------------------------------------------------

# Gemini exposes coarser thinking levels than Anthropic's effort scale, so the
# top two collapse onto HIGH.
_GEMINI_THINKING = {
    "low": "LOW",
    "medium": "MEDIUM",
    "high": "HIGH",
    "xhigh": "HIGH",
    "max": "HIGH",
}

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
        effort: str = "high",
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

        key = api_key or os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
        if not key:
            raise LLMError(
                "No Gemini credentials found. Set one of:\n"
                "    $env:GEMINI_API_KEY = '...'   (PowerShell)\n"
                "    $env:GOOGLE_API_KEY = '...'"
            )
        self._client = genai.Client(api_key=key)
        self.model = model
        self.effort = effort
        self.max_tokens = max_tokens
        self.usage = Usage()
        self._warned_about_budget = False

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
            response = self._client.models.generate_content(
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
            response = self._client.models.generate_content(
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
# Generator
# --------------------------------------------------------------------------


@dataclass
class GenerationResult:
    flow: Flow
    messages: List[Message]
    repairs: int


class FlowGenerator:
    """
    Turns a request into a validated Flow, repairing its own mistakes.

    The conversation is kept so a refinement continues from the same context
    rather than re-deriving the flow from scratch.
    """

    def __init__(self, provider: Provider, max_repairs: int = DEFAULT_MAX_REPAIRS):
        self.provider = provider
        self.max_repairs = max_repairs
        # Handed to the provider raw; each one adapts it to its own dialect.
        self._schema = Flow.model_json_schema()

    def _validated(self, messages: List[Message]) -> GenerationResult:
        conversation = list(messages)
        last_error: Optional[str] = None
        previous_names: Optional[set] = None

        for attempt in range(self.max_repairs + 1):
            log.info(
                "attempt %s/%s (%s messages)",
                attempt + 1, self.max_repairs + 1, len(conversation),
            )
            payload = self.provider.complete_json(SYSTEM_PROMPT, conversation, self._schema)

            # A model repairing a reachability error can "win" by deleting the
            # orphaned elements instead of adding the connector that was asked
            # for - a smaller flow trivially satisfies "everything is
            # reachable". That is never the fix, so catch it before it reaches
            # Flow.model_validate, which cannot tell it from a deliberate
            # simplification. Compare counts, not just names - a repair that
            # renames an element (e.g. to fix an invalid API name) changes its
            # name without shrinking the flow, and that is fine.
            #
            # Only elements the previous complaint actually named count. A flow
            # can shrink for good reasons - the user asked for a step to go, or
            # two updates were merged into one - and those elements are ones no
            # error mentioned. Rejecting every shrink cost a repair round on a
            # legitimate "remove the urgency check" and told the model to put
            # back exactly what it had been asked to delete.
            #
            # The baseline is the previous attempt in this loop, not the flow
            # being refined, so a refinement that removes something is compared
            # against nothing and passes on its first attempt.
            current_names = {
                e.get("name")
                for e in payload.get("elements", []) or []
                if isinstance(e, dict) and e.get("name")
            }
            dropped = set()
            if previous_names is not None and len(current_names) < len(previous_names):
                dropped = {
                    name for name in previous_names - current_names
                    if last_error and name in last_error
                }
            previous_names = current_names

            if dropped:
                last_error = (
                    f"elements {sorted(dropped)} are missing from this attempt, but "
                    "were present in the one before it, and the error you were "
                    "given names them. Deleting an element is not a fix for a "
                    "validation error about it - it just hides the problem. "
                    "Restore them and fix what the error actually named."
                )
                log.warning("attempt %s rejected: %s", attempt + 1, last_error)
                if attempt == self.max_repairs:
                    break
                conversation.append(
                    Message(role="assistant", content=json.dumps(payload, indent=1))
                )
                conversation.append(Message(role="user", content=last_error))
                continue

            try:
                flow = Flow.model_validate(payload)
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

            log.info(
                "valid IR after %s attempt%s: %s, %s element%s",
                attempt + 1, '' if attempt == 0 else 's', flow.api_name,
                len(flow.elements), '' if len(flow.elements) == 1 else 's',
            )
            return GenerationResult(flow=flow, messages=conversation, repairs=attempt)

        raise LLMError(
            f"Could not get valid IR after {self.max_repairs + 1} attempts. "
            f"Last errors:\n{last_error}"
        )

    def generate(self, request: str) -> GenerationResult:
        return self._validated([Message(role="user", content=request)])

    def adopt(self, flow: Flow, origin: str = "an existing flow in the org") -> GenerationResult:
        """
        Start a conversation from a flow that already exists, so a refinement
        edits it rather than designing something new from its description.
        """
        return GenerationResult(
            flow=flow,
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
