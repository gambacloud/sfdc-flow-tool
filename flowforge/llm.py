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
import os
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Protocol

from pydantic import ValidationError

from .ir import Flow

DEFAULT_MAX_REPAIRS = 3


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

    def complete_json(
        self, system: str, messages: List[Message], schema: Dict[str, Any]
    ) -> Dict[str, Any]: ...


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
- A Decision's `next` is its default (else) path. Each outcome has its own `next`.
- A Loop's `first_element` is the first element inside the loop body; its `next` \
is what runs after the loop finishes.
- Conditions are structured: a left reference, an operator, and a typed right \
value. Never write a condition as a formula string.

## Rules Salesforce enforces

- Element and outcome names must be valid API names: start with a letter, then \
letters, digits, and single underscores. No spaces, no trailing underscore, no \
double underscore.
- Update Records has two mutually exclusive modes. Either update a record already \
in a variable (`input_reference` alone), or find records by criteria and set \
values (`object` + `filters` + `fields`). Never combine `input_reference` with \
`fields` or `filters` - Salesforce rejects the deploy.
- Create Records likewise takes either `input_reference` or `fields`, not both.
- A record retrieved with `store_output_automatically: true` is read-only. You \
cannot assign into its fields. To change values on it, use Update Records by \
criteria filtered on its Id.
- Record-triggered flows must set `object`, `record_trigger_type`, and \
`trigger_type` on start. Autolaunched flows leave all three empty.
- Reference the triggering record as `$Record` (for example `$Record.Amount`), \
and a retrieved record by its element name (for example `Get_Account.Id`).

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
            raise LLMError(
                "No Anthropic credentials found. Set one of:\n"
                "    $env:ANTHROPIC_API_KEY = 'sk-ant-...'   (PowerShell)\n"
                "    ant auth login                          (OAuth profile)"
            ) from exc
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
        return json.loads(text)


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
        max_tokens: int = 16000,
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

    def _available_models(self) -> List[str]:
        try:
            return sorted(
                m.name.removeprefix("models/")
                for m in self._client.models.list()
                if m.name
            )
        except Exception:  # listing is a nicety; never mask the original error
            return []

    def complete_json(
        self, system: str, messages: List[Message], schema: Dict[str, Any]
    ) -> Dict[str, Any]:
        from google.genai import errors, types

        # Gemini names the assistant role "model", not "assistant".
        contents = [
            types.Content(
                role="user" if message.role == "user" else "model",
                parts=[types.Part(text=message.content)],
            )
            for message in messages
        ]

        config = types.GenerateContentConfig(
            system_instruction=system,
            response_mime_type="application/json",
            response_json_schema=gemini_schema(schema),
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
            raise LLMError(f"Gemini rejected the request: {exc.message}") from exc
        except errors.ServerError as exc:
            raise LLMError(f"Gemini server error: {exc.message}") from exc
        except errors.APIError as exc:
            raise LLMError(f"Gemini API error: {exc.message}") from exc

        text = response.text
        if not text:
            # Usually a safety block or an output-token cutoff; say which.
            reason = "unknown"
            if response.candidates:
                reason = str(response.candidates[0].finish_reason)
            raise LLMError(f"Gemini returned no JSON (finish reason: {reason}).")
        return json.loads(text)


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

        for attempt in range(self.max_repairs + 1):
            payload = self.provider.complete_json(SYSTEM_PROMPT, conversation, self._schema)
            try:
                flow = Flow.model_validate(payload)
            except ValidationError as exc:
                last_error = _readable_errors(exc)
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

            return GenerationResult(flow=flow, messages=conversation, repairs=attempt)

        raise LLMError(
            f"Could not get valid IR after {self.max_repairs + 1} attempts. "
            f"Last errors:\n{last_error}"
        )

    def generate(self, request: str) -> GenerationResult:
        return self._validated([Message(role="user", content=request)])

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
