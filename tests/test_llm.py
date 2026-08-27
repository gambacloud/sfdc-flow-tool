"""
The repair loop, exercised with a scripted provider - no API key, no network.

What matters is that an invalid IR from the model turns into a corrected one
without a human in the loop, and that the model is told exactly what was wrong.
"""

import json
import threading

import pytest

from flowtool.llm import (
    APEX_SYSTEM_PROMPT,
    FIELD_SYSTEM_PROMPT,
    FlowGenerator,
    GeminiProvider,
    LLMError,
    LWC_SYSTEM_PROMPT,
    Message,
    OBJECT_SYSTEM_PROMPT,
    SYSTEM_PROMPT,
    Usage,
    _GEMINI_SERVER_ERROR_BACKOFF,
    gemini_schema,
    strict_schema,
)
from flowtool.ir import Flow

VALID = {
    "api_name": "Won_Deal_Flow",
    "label": "Won Deal Flow",
    "start": {
        "object": "Opportunity",
        "record_trigger_type": "CreateAndUpdate",
        "trigger_type": "RecordAfterSave",
        "next": "Get_Account",
    },
    "elements": [
        {
            "type": "GetRecords",
            "name": "Get_Account",
            "label": "Get Account",
            "object": "Account",
            "next": None,
        }
    ],
}


def _with(**changes):
    payload = json.loads(json.dumps(VALID))
    payload.update(changes)
    return payload


DANGLING = _with(
    elements=[
        {
            "type": "GetRecords",
            "name": "Get_Account",
            "label": "Get Account",
            "object": "Account",
            "next": "Does_Not_Exist",
        }
    ]
)

BAD_NAME = _with(
    start={**VALID["start"], "next": "Get Account"},
    elements=[
        {
            "type": "GetRecords",
            "name": "Get Account",
            "label": "Get Account",
            "object": "Account",
            "next": None,
        }
    ],
)


class ScriptedProvider:
    """
    Returns each queued payload in turn and records what it was asked.

    Locked because a plan can now run independent steps' generations
    concurrently (planner.execute_plan) against the one provider a test
    passes it - without this, two threads racing pop(0)/append at once could
    corrupt call order or, worse, hand one step the payload queued for a
    different step's type, in a way that fails unpredictably rather than
    deterministically. Existing single-threaded callers are unaffected: an
    uncontended lock costs nothing worth noticing.
    """

    name = "scripted"

    def __init__(self, *payloads):
        self.payloads = list(payloads)
        self.calls = []
        self.usage = Usage()
        self._lock = threading.Lock()

    def complete_json(self, system, messages, schema):
        with self._lock:
            self.calls.append(list(messages))
            self.usage.add(input_tokens=100, output_tokens=50)
            return self.payloads.pop(0)


class TypedScriptedProvider:
    """
    Routes a queued payload to whichever artifact type actually asked for
    it, by matching the system prompt (each generator's is a distinct
    constant) rather than call order. Needed wherever a test drives a plan
    with more than one artifact type in the same dependency layer:
    planner.execute_plan runs those concurrently, so a plain
    ScriptedProvider's FIFO queue can't guarantee Object's thread gets
    Object's payload rather than Apex's.
    """

    name = "scripted"

    # Imported lazily inside __init__ rather than at module level: importing
    # flowtool.planner here would make every test_llm.py test pay for
    # loading it, when only tests that actually construct a
    # TypedScriptedProvider(plan=...) need PLANNER_SYSTEM_PROMPT at all.
    _PROMPT_TO_TYPE = {
        SYSTEM_PROMPT: "flow",
        OBJECT_SYSTEM_PROMPT: "object",
        FIELD_SYSTEM_PROMPT: "field",
        APEX_SYSTEM_PROMPT: "apex",
        LWC_SYSTEM_PROMPT: "lwc",
    }

    def __init__(self, **payloads_by_type):
        """Each kwarg (plan=..., flow=..., object=..., field=..., apex=...,
        lwc=...) is either one payload or a list of payloads served in order
        to that type's calls."""
        prompt_to_type = dict(self._PROMPT_TO_TYPE)
        if "plan" in payloads_by_type:
            from flowtool.planner import PLANNER_SYSTEM_PROMPT
            prompt_to_type[PLANNER_SYSTEM_PROMPT] = "plan"
        self._prompt_to_type = prompt_to_type
        self._queues = {
            artifact_type: (value if isinstance(value, list) else [value])
            for artifact_type, value in payloads_by_type.items()
        }
        self.calls = []
        self.usage = Usage()
        self._lock = threading.Lock()

    def complete_json(self, system, messages, schema):
        artifact_type = self._prompt_to_type[system]
        with self._lock:
            self.calls.append((artifact_type, list(messages)))
            self.usage.add(input_tokens=100, output_tokens=50)
            return self._queues[artifact_type].pop(0)


class TestRepairLoop:
    def test_valid_first_try_costs_no_repairs(self):
        provider = ScriptedProvider(VALID)
        result = FlowGenerator(provider).generate("when a deal is won, get the account")
        assert result.repairs == 0
        assert isinstance(result.flow, Flow)
        assert result.flow.api_name == "GC_Won_Deal_Flow"

    def test_dangling_reference_is_repaired(self):
        provider = ScriptedProvider(DANGLING, VALID)
        result = FlowGenerator(provider).generate("...")
        assert result.repairs == 1
        assert result.flow.elements[0].next is None

    def test_invalid_api_name_is_repaired(self):
        provider = ScriptedProvider(BAD_NAME, VALID)
        result = FlowGenerator(provider).generate("...")
        assert result.repairs == 1

    def test_model_is_told_what_was_wrong(self):
        provider = ScriptedProvider(DANGLING, VALID)
        FlowGenerator(provider).generate("...")

        # Second call must carry the rejected IR and the validator's complaint.
        second = provider.calls[1]
        assert second[-2].role == "assistant"
        complaint = second[-1].content
        assert "failed validation" in complaint
        assert "unresolved references" in complaint
        assert "Does_Not_Exist" in complaint

    def test_gives_up_after_the_repair_budget(self):
        provider = ScriptedProvider(DANGLING, DANGLING, DANGLING)
        with pytest.raises(LLMError, match="Could not get valid IR"):
            FlowGenerator(provider, max_repairs=2).generate("...")
        assert not provider.payloads, "should have used its whole budget"

    def test_deleting_the_orphan_instead_of_fixing_it_is_rejected(self):
        # A model repairing "unreachable elements" can trivially satisfy the
        # check by deleting the orphan instead of adding the missing
        # connector. That must not be accepted as a fix.
        shrunk = _with(elements=[])
        provider = ScriptedProvider(DANGLING, shrunk, VALID)
        result = FlowGenerator(provider).generate("...")
        assert result.repairs == 2
        assert [e.name for e in result.flow.elements] == ["Get_Account"]

        complaint = provider.calls[2][-1].content
        assert "Get_Account" in complaint
        assert "not a fix" in complaint

    def test_gives_up_when_the_model_only_ever_shrinks(self):
        shrunk = _with(elements=[])
        provider = ScriptedProvider(DANGLING, shrunk, shrunk)
        with pytest.raises(LLMError, match="Could not get valid IR"):
            FlowGenerator(provider, max_repairs=2).generate("...")
        assert not provider.payloads, "should have used its whole budget"


class TestRefine:
    def test_refinement_continues_the_conversation(self):
        provider = ScriptedProvider(VALID, VALID)
        generator = FlowGenerator(provider)
        first = generator.generate("build it")
        generator.refine(first, "also mark the account hot")

        second = provider.calls[1]
        assert second[0].content == "build it", "lost the original request"
        assert second[-1].content == "also mark the account hot"

    def test_salesforce_errors_are_fed_back(self):
        provider = ScriptedProvider(VALID, VALID)
        generator = FlowGenerator(provider)
        first = generator.generate("build it")
        generator.repair_from_salesforce(
            first, ["[Error] Mark_Hot (Update Records) - You can't use the sObjectInputReference field"]
        )

        instruction = provider.calls[1][-1].content
        assert "Salesforce rejected" in instruction
        assert "sObjectInputReference" in instruction


class TestGeminiServerErrorRetry:
    """
    A Gemini 503 ("high demand") is transient by Google's own description -
    _generate retries it with backoff instead of failing the whole
    generation (and, inside a multi-step plan, every step after it) on what
    is usually a momentary blip.
    """

    def _provider(self, monkeypatch):
        provider = GeminiProvider(api_key="fake-key-for-test")
        monkeypatch.setattr("time.sleep", lambda seconds: None)
        return provider

    def _server_error(self):
        from google.genai import errors
        return errors.ServerError(
            503, {"error": {"message": "high demand", "status": "UNAVAILABLE"}}
        )

    def test_succeeds_after_transient_server_errors(self, monkeypatch):
        provider = self._provider(monkeypatch)
        calls = {"n": 0}

        def flaky(**kwargs):
            calls["n"] += 1
            if calls["n"] < 3:
                raise self._server_error()
            return "ok"

        monkeypatch.setattr(provider._client.models, "generate_content", flaky)
        assert provider._generate() == "ok"
        assert calls["n"] == 3
        # Cleared once it actually succeeds - a poller must not keep showing
        # a stale "retrying" note after the call that mattered came back.
        assert provider.retry_status is None

    def test_retry_status_is_set_while_waiting(self, monkeypatch):
        # Read from inside the flaky call itself: retry_status has to be set
        # *before* the sleep a poller would be racing against, not just
        # noticeable afterwards.
        provider = self._provider(monkeypatch)
        seen = []

        def flaky(**kwargs):
            seen.append(provider.retry_status)
            if len(seen) < 2:
                raise self._server_error()
            return "ok"

        monkeypatch.setattr(provider._client.models, "generate_content", flaky)
        provider._generate()
        assert seen[0] is None, "nothing to report before the first attempt"
        assert seen[1]["reason"] == "server_error"
        assert seen[1]["wait"] == _GEMINI_SERVER_ERROR_BACKOFF[0]
        assert "high demand" in seen[1]["message"].lower()

    def test_gives_up_after_the_retry_budget(self, monkeypatch):
        provider = self._provider(monkeypatch)

        def always_503(**kwargs):
            raise self._server_error()

        monkeypatch.setattr(provider._client.models, "generate_content", always_503)
        from google.genai import errors
        with pytest.raises(errors.ServerError):
            provider._generate()
        # A final, un-retried failure is not "still waiting" - nothing left
        # for a poller to show once the exception has already propagated.
        assert provider.retry_status is None

    def test_a_rate_limit_is_not_treated_as_a_server_error(self, monkeypatch):
        # Rate limiting already has its own handling (key rotation) - this
        # pins down that a 429 isn't accidentally caught by the new 503
        # branch and retried the wrong way.
        provider = self._provider(monkeypatch)
        from google.genai import errors

        def rate_limited(**kwargs):
            raise errors.ClientError(
                429, {"error": {"message": "quota", "status": "RESOURCE_EXHAUSTED"}}
            )

        monkeypatch.setattr(provider._client.models, "generate_content", rate_limited)
        # Only one key configured, so there's nothing to switch to - but a
        # 429 is still waited out (see TestGeminiRateLimit), just not via key
        # rotation. Every retry keeps hitting the same mock, so this still
        # ends in the same error - it just takes the retry budget to get there.
        calls = {"n": 0}

        def counting(**kwargs):
            calls["n"] += 1
            return rate_limited(**kwargs)

        monkeypatch.setattr(provider._client.models, "generate_content", counting)
        with pytest.raises(errors.ClientError):
            provider._generate()
        assert provider.retry_status is None
        assert calls["n"] > 1, "should have retried before giving up"

    def test_retry_status_reports_a_rate_limit_switch(self, monkeypatch):
        provider = GeminiProvider(api_key="fake-key-for-test")
        provider._clients.append(provider._clients[0])  # a second "key" to switch to
        from google.genai import errors

        calls = {"n": 0}

        def rate_limited_once(**kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise errors.ClientError(
                    429, {"error": {"message": "quota", "status": "RESOURCE_EXHAUSTED"}}
                )
            return "ok"

        monkeypatch.setattr(provider._clients[0].models, "generate_content", rate_limited_once)
        monkeypatch.setattr(provider._clients[1].models, "generate_content", rate_limited_once)
        assert provider._generate() == "ok"
        assert provider.retry_status is None  # cleared on the eventual success


class TestGeminiRateLimit:
    """
    A 429 with no untried key left is not a dead end: Google's own message
    names a retry delay ("Please retry in 31.5s"), and free-tier quotas reset
    on their own - so this waits it out instead of failing outright, the
    real-world case that motivated it (a single free-tier key, 5 requests a
    minute, hit mid-plan).
    """

    def _provider(self, monkeypatch, n_keys=1):
        provider = GeminiProvider(api_key="fake-key-for-test")
        for _ in range(n_keys - 1):
            # A genuinely distinct client per slot, not the same object
            # appended twice - each test patches generate_content per client
            # to prove *that* key was actually attempted, which a shared
            # object can't distinguish.
            provider._clients.append(GeminiProvider(api_key="fake-key-for-test")._clients[0])
        monkeypatch.setattr("time.sleep", lambda seconds: None)
        return provider

    def _quota_error(self, message="Please retry in 31.559251234s."):
        from google.genai import errors
        return errors.ClientError(
            429, {"error": {"message": message, "status": "RESOURCE_EXHAUSTED"}}
        )

    def test_parses_the_suggested_delay_from_the_message(self):
        from flowtool.llm import _parse_retry_delay
        assert _parse_retry_delay("Please retry in 31.559251234s.") == 32  # rounded up

    def test_falls_back_to_a_default_when_unparseable(self):
        from flowtool.llm import _GEMINI_RATE_LIMIT_FALLBACK_WAIT, _parse_retry_delay
        assert _parse_retry_delay("quota exceeded, no delay given") == \
            _GEMINI_RATE_LIMIT_FALLBACK_WAIT
        assert _parse_retry_delay(None) == _GEMINI_RATE_LIMIT_FALLBACK_WAIT

    def test_waits_out_the_limit_on_a_single_key_and_succeeds(self, monkeypatch):
        provider = self._provider(monkeypatch, n_keys=1)
        calls = {"n": 0}

        def flaky(**kwargs):
            calls["n"] += 1
            if calls["n"] < 3:
                raise self._quota_error()
            return "ok"

        monkeypatch.setattr(provider._client.models, "generate_content", flaky)
        assert provider._generate() == "ok"
        assert calls["n"] == 3
        assert provider.retry_status is None

    def test_reports_wait_time_while_every_key_is_exhausted(self, monkeypatch):
        provider = self._provider(monkeypatch, n_keys=1)
        seen = []

        def flaky(**kwargs):
            seen.append(provider.retry_status)
            if len(seen) < 2:
                raise self._quota_error("Please retry in 5s.")
            return "ok"

        monkeypatch.setattr(provider._client.models, "generate_content", flaky)
        provider._generate()
        assert seen[0] is None
        assert seen[1]["reason"] == "rate_limited"
        assert seen[1]["wait"] == 5

    def test_gives_up_after_the_rate_limit_retry_budget(self, monkeypatch):
        provider = self._provider(monkeypatch, n_keys=1)

        def always_limited(**kwargs):
            raise self._quota_error()

        monkeypatch.setattr(provider._client.models, "generate_content", always_limited)
        from google.genai import errors
        with pytest.raises(errors.ClientError):
            provider._generate()
        assert provider.retry_status is None

    def test_all_keys_are_tried_before_waiting(self, monkeypatch):
        # Three keys, all rate-limited: every one should be tried (key
        # rotation) before falling back to waiting out the delay - waiting
        # should not pre-empt a key that was never actually attempted.
        provider = self._provider(monkeypatch, n_keys=3)
        provider._key_index = 0  # pin the random start for a deterministic assertion
        attempted = set()

        for i, client in enumerate(provider._clients):
            def make(i=i):
                def fn(**kwargs):
                    attempted.add(i)
                    raise self._quota_error("Please retry in 1s.")
                return fn
            monkeypatch.setattr(client.models, "generate_content", make())

        from google.genai import errors
        with pytest.raises(errors.ClientError):
            provider._generate()
        assert attempted == {0, 1, 2}

    def test_starting_key_index_is_randomised_across_configured_keys(self, monkeypatch):
        # Not a statistical test - proves __init__ actually consults
        # random.randrange with the real key count (3), rather than always
        # starting at index 0 regardless of how many keys are configured.
        # That's what lets several separate requests, each a fresh provider
        # (see build_provider in server.py), spread across every key from
        # the start instead of hammering key 1 alone until it trips.
        import flowtool.llm as llm_module

        monkeypatch.setattr(llm_module, "_gemini_keys", lambda: ["k1", "k2", "k3"])
        seen_n = {}

        def fake_randrange(n):
            seen_n["n"] = n
            return 2

        monkeypatch.setattr("random.randrange", fake_randrange)
        provider = GeminiProvider()
        assert seen_n["n"] == 3
        assert provider._key_index == 2


RAW = Flow.model_json_schema()

# The exact keyword set Gemini's response_json_schema documents.
GEMINI_SUPPORTED = {
    "$id", "$defs", "$ref", "$anchor", "type", "format", "title", "description",
    "enum", "items", "prefixItems", "minItems", "maxItems", "minimum", "maximum",
    "anyOf", "oneOf", "properties", "additionalProperties", "required",
    "propertyOrdering",
}

NAME_KEYED = {"properties", "$defs"}


def keywords(schema, path="root"):
    """Yield (path, keyword) for real schema keywords, skipping field names."""
    if isinstance(schema, dict):
        for key, value in schema.items():
            if key in NAME_KEYED and isinstance(value, dict):
                for name, sub in value.items():
                    yield from keywords(sub, f"{path}.{key}.{name}")
                continue
            yield path, key
            yield from keywords(value, f"{path}.{key}")
    elif isinstance(schema, list):
        for index, value in enumerate(schema):
            yield from keywords(value, f"{path}[{index}]")


def open_objects(schema, path="root"):
    if isinstance(schema, dict):
        if (schema.get("type") == "object" or "properties" in schema) and \
                schema.get("additionalProperties") is not False:
            yield path
        for key, value in schema.items():
            if key in NAME_KEYED and isinstance(value, dict):
                for name, sub in value.items():
                    yield from open_objects(sub, f"{path}.{key}.{name}")
            else:
                yield from open_objects(value, f"{path}.{key}")
    elif isinstance(schema, list):
        for index, value in enumerate(schema):
            yield from open_objects(value, f"{path}[{index}]")


class TestFieldNamesSurvive:
    """
    Regression: an allowlist filter that recurses into `properties` treats field
    names as keywords and deletes the entire document.
    """

    @pytest.mark.parametrize("adapt", [strict_schema, gemini_schema])
    def test_top_level_fields_are_kept(self, adapt):
        assert set(adapt(RAW)["properties"]) == set(RAW["properties"])

    @pytest.mark.parametrize("adapt", [strict_schema, gemini_schema])
    def test_every_definition_keeps_its_fields(self, adapt):
        adapted = adapt(RAW)
        assert set(adapted["$defs"]) == set(RAW["$defs"])
        for name, definition in RAW["$defs"].items():
            if "properties" in definition:
                assert set(adapted["$defs"][name]["properties"]) == set(
                    definition["properties"]
                ), f"{name} lost fields"


class TestAnthropicDialect:
    def test_objects_are_closed(self):
        assert not list(open_objects(strict_schema(RAW)))

    def test_unsupported_constraints_are_gone(self):
        unsupported = {"minItems", "maxItems", "minLength", "maxLength", "minimum", "maximum"}
        found = {k for _, k in keywords(strict_schema(RAW)) if k in unsupported}
        assert not found, found

    def test_const_discriminator_is_preserved(self):
        # Anthropic supports const, so it stays as the tighter constraint.
        assert strict_schema(RAW)["$defs"]["GetRecords"]["properties"]["type"]["const"] == "GetRecords"


class TestGeminiDialect:
    def test_only_supported_keywords_survive(self):
        found = {k for _, k in keywords(gemini_schema(RAW)) if k not in GEMINI_SUPPORTED}
        assert not found, f"Gemini would reject: {found}"

    def test_objects_are_closed(self):
        assert not list(open_objects(gemini_schema(RAW)))

    def test_const_becomes_a_single_value_enum(self):
        # Gemini has no `const`; a one-value enum constrains it identically.
        type_schema = gemini_schema(RAW)["$defs"]["GetRecords"]["properties"]["type"]
        assert type_schema["enum"] == ["GetRecords"]
        assert "const" not in type_schema

    def test_defaults_and_discriminator_are_dropped(self):
        found = {k for _, k in keywords(gemini_schema(RAW)) if k in {"default", "discriminator"}}
        assert not found, found

    def test_the_union_of_element_types_is_intact(self):
        # Counted from the IR rather than hard-coded, so adding an element type
        # cannot make this test pass by describing the old union.
        from typing import get_args

        from flowtool.ir import Element

        expected = len(get_args(get_args(Element)[0]))
        elements = gemini_schema(RAW)["properties"]["elements"]["items"]
        branches = elements.get("anyOf") or elements.get("oneOf")
        assert branches and len(branches) == expected, "lost element types from the union"


class TestShrinkingForGoodReasons:
    """
    The anti-shrink guard exists because a model "repaired" an unreachable
    element by deleting it. But a flow shrinks for legitimate reasons too - the
    user asked for a step to go, two updates were merged - and rejecting every
    shrink turned a valid answer into a wasted repair round with an instruction
    to restore what the user had asked to remove.

    The rule that separates them: only elements the previous error actually
    named are suspicious.
    """

    SMALLER = _with(elements=[
        {
            "type": "GetRecords",
            "name": "Get_Account",
            "label": "Get Account",
            "object": "Account",
            "next": None,
        }
    ])

    def test_a_removal_asked_for_is_accepted_first_try(self):
        """No previous attempt to compare against, so nothing is suspicious."""
        provider = ScriptedProvider(VALID, self.SMALLER)
        generator = FlowGenerator(provider)
        first = generator.generate("build it")
        after = generator.refine(first, "drop the account lookup")
        assert after.repairs == 0
        assert [e.name for e in after.flow.elements] == ["Get_Account"]

    def test_shrinking_past_an_unrelated_error_is_not_evasion(self):
        """
        Attempt 1 fails on a dangling reference to something else entirely;
        attempt 2 fixes it and also drops an element nobody complained about.
        That is the requested change landing, not the error being dodged.
        """
        two_elements = _with(elements=[
            {"type": "GetRecords", "name": "Get_Account", "label": "G",
             "object": "Account", "next": "Nowhere"},
            {"type": "GetRecords", "name": "Get_Contact", "label": "C",
             "object": "Contact", "next": None},
        ])
        provider = ScriptedProvider(two_elements, self.SMALLER)
        result = FlowGenerator(provider).generate("...")
        assert result.repairs == 1, "one real repair, not one wasted on the guard"
        assert [e.name for e in result.flow.elements] == ["Get_Account"]

    def test_deleting_the_element_the_error_named_is_still_caught(self):
        """The original failure must stay caught: the error names Get_Account."""
        provider = ScriptedProvider(DANGLING, _with(elements=[]), VALID)
        result = FlowGenerator(provider).generate("...")
        assert result.repairs == 2
        complaint = provider.calls[2][-1].content
        assert "Get_Account" in complaint
        assert "not a fix" in complaint

    def test_the_complaint_says_the_error_named_them(self):
        provider = ScriptedProvider(DANGLING, _with(elements=[]), VALID)
        FlowGenerator(provider).generate("...")
        complaint = provider.calls[2][-1].content
        assert "names them" in complaint, (
            "the model must be told why this deletion is different from a "
            "legitimate one"
        )
