"""
NL request -> an ordered list of typed steps -> one generator call per step.

This is the piece that turns "an Invoice object with an Amount field and a
flow that totals it" into three coordinated generations instead of three
separate manual ones. Nothing here is structurally new: the planner's own
output is just another Pydantic model (Plan), produced through the same
schema-constrained generate + repair loop every other artifact type uses (see
llm.py's IRGenerator) - and each step, once planned, is handed off to the
exact generator Phase 1-3 already built (FlowGenerator, CustomObjectGenerator,
CustomFieldGenerator, ApexClassGenerator), unmodified.

A request that only needs one Flow produces a one-step plan. That is
deliberate, not a special case: it is what keeps the existing Flow-only
behaviour working through this same path rather than needing a separate one.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Dict, List, Type, Union

from pydantic import BaseModel, Field, model_validator

from .ir_apex import ApexClass
from .ir_object import CustomField, CustomObject
from .ir import Flow
from .llm import (
    ApexClassGenerator,
    CustomFieldGenerator,
    CustomObjectGenerator,
    DEFAULT_MAX_REPAIRS,
    FlowGenerator,
    GenerationResult,
    IRGenerationResult,
    IRGenerator,
    Message,
    Provider,
)

ArtifactType = str  # "object" | "field" | "apex" | "flow" - see PlanStep.artifact_type

_KNOWN_STEP_TYPES = {"object", "field", "apex", "flow"}


class PlanStep(BaseModel):
    artifact_type: str = Field(description="One of: object, field, apex, flow")
    name: str = Field(description="Short, unique label for this step, e.g. 'Invoice object'")
    brief: str = Field(
        description="A self-contained request for this step's generator - write "
        "it as if the other steps do not exist yet, naming any object/field api "
        "names another step will create, since that generator sees only this text"
    )
    depends_on: List[str] = Field(
        default_factory=list,
        description="Names of steps that must be generated before this one - "
        "for example a field step depends on the object step that creates its "
        "object, and a flow step depends on any field it reads or writes",
    )

    @model_validator(mode="after")
    def valid_shape(self) -> "PlanStep":
        if self.artifact_type not in _KNOWN_STEP_TYPES:
            raise ValueError(
                f"step {self.name!r}: artifact_type must be one of "
                f"{sorted(_KNOWN_STEP_TYPES)}, got {self.artifact_type!r}"
            )
        if self.name in self.depends_on:
            raise ValueError(f"step {self.name!r} cannot depend on itself")
        return self


class Plan(BaseModel):
    steps: List[PlanStep] = Field(min_length=1)

    @model_validator(mode="after")
    def names_unique_and_dependencies_resolve(self) -> "Plan":
        names = [s.name for s in self.steps]
        if len(names) != len(set(names)):
            dupes = sorted({n for n in names if names.count(n) > 1})
            raise ValueError(f"duplicate step names: {dupes}")
        known = set(names)
        for step in self.steps:
            unknown = [d for d in step.depends_on if d not in known]
            if unknown:
                raise ValueError(
                    f"step {step.name!r} depends on unknown step(s): {unknown}"
                )
        return self

    def ordered(self) -> List[PlanStep]:
        """
        Steps with every dependency placed before its dependents - a flat
        topological sort. execute_plan below actually runs steps by layers()
        (parallel where nothing depends on nothing), not this flat order;
        ordered() stays as the simpler sequential-order utility, and the one
        thing layers() reuses its failure mode from: a cycle is a bug in the
        planner's own output, since names_unique_and_dependencies_resolve
        already guarantees every depends_on entry names a real step, so the
        only way either can fail is a genuine cycle.
        """
        by_name = {s.name: s for s in self.steps}
        order: List[PlanStep] = []
        visiting: set = set()
        visited: set = set()

        def visit(step: PlanStep) -> None:
            if step.name in visited:
                return
            if step.name in visiting:
                raise ValueError(f"circular dependency involving step {step.name!r}")
            visiting.add(step.name)
            for dep_name in step.depends_on:
                visit(by_name[dep_name])
            visiting.discard(step.name)
            visited.add(step.name)
            order.append(step)

        for step in self.steps:
            visit(step)
        return order

    def layers(self) -> List[List[PlanStep]]:
        """
        Steps grouped into dependency-respecting waves: every step in one
        layer can run in parallel, because every step it depends on is
        already finished by the end of an earlier layer. A step with no
        dependencies is in layer 0; otherwise a step's layer is one past the
        deepest layer among the steps it depends on, so it never starts
        before the last thing it needs actually finishes.

        Order within a layer follows the plan's own step order, the same
        stability ordered() keeps, so which step happens to run first among
        equals isn't arbitrary run to run.
        """
        by_name = {s.name: s for s in self.steps}
        depth_of: Dict[str, int] = {}

        def depth(name: str, visiting: frozenset) -> int:
            if name in depth_of:
                return depth_of[name]
            if name in visiting:
                raise ValueError(f"circular dependency involving step {name!r}")
            step = by_name[name]
            d = 0
            for dep_name in step.depends_on:
                d = max(d, 1 + depth(dep_name, visiting | {name}))
            depth_of[name] = d
            return d

        for step in self.steps:
            depth(step.name, frozenset())

        result: List[List[PlanStep]] = []
        for step in self.steps:
            d = depth_of[step.name]
            while len(result) <= d:
                result.append([])
            result[d].append(step)
        return result


PLANNER_SYSTEM_PROMPT = """\
You break a Salesforce implementation request into an ordered list of typed \
steps. You do not write any metadata yourself - each step is handed to a \
specialised generator afterward, and that generator sees only the step's own \
`brief`, not the rest of the plan or your reasoning about it.

## Step types

- `object` - a new Custom Object.
- `field` - a new Custom Field, on a standard object or a custom one from \
another step.
- `apex` - a new Apex Class.
- `flow` - a new Flow.

## Writing a brief

Each `brief` must be a complete, self-contained request for that one step - \
write it as if the other steps had never been mentioned. If a field step adds \
a field to an object another step creates, name that object's intended \
api_name in the brief exactly, since the field generator has no other way to \
know it. The same goes for a flow or Apex class that reads or writes a field \
another step creates.

## Ordering and dependencies

`depends_on` lists the names of steps that must be generated first. An object \
always precedes a field added to it. A field a Flow or Apex class references \
precedes that step. Do not add a dependency that is not actually needed - it \
only slows the plan down for no reason.

## Keep the plan minimal

One step per distinct thing the request actually asks for, no more. A request \
that only needs a single Flow (or a single object, field, or class) is a \
plan with exactly one step - do not invent extra steps to make the plan look \
more thorough. Do not add validation, logging, or components the request did \
not ask for, the same restraint every individual generator is asked for.
"""


class PlannerGenerator(IRGenerator[Plan]):
    """Turns an implementation request into a validated Plan."""

    def __init__(self, provider: Provider, max_repairs: int = DEFAULT_MAX_REPAIRS):
        super().__init__(provider, Plan, PLANNER_SYSTEM_PROMPT, max_repairs)

    def _describe(self, plan: Plan) -> str:
        kinds = ", ".join(f"{s.artifact_type}:{s.name}" for s in plan.steps)
        return f"{len(plan.steps)} step(s): {kinds}"

    def generate(self, request: str) -> IRGenerationResult[Plan]:
        return self._validated([Message(role="user", content=request)])


# --------------------------------------------------------------------------
# Running a plan
# --------------------------------------------------------------------------

_GENERATOR_BY_TYPE: Dict[str, Type[IRGenerator]] = {
    "flow": FlowGenerator,
    "object": CustomObjectGenerator,
    "field": CustomFieldGenerator,
    "apex": ApexClassGenerator,
}

# A cap on how many steps in one layer run at once, not "as many as fit."
# More concurrent requests against the same free-tier quota is exactly what
# made rate limits worse to begin with (see llm.py's Gemini 429 handling) -
# this bounds the downside while still shortening a wide plan's wall time.
_MAX_PARALLEL_STEPS = 3

# What a generator's own .generate() returns, per artifact type - FlowGenerator
# keeps its pre-existing GenerationResult (the .flow attribute server.py
# already relies on); the others return the generic IRGenerationResult (.value)
# from Phase 1. StepResult.value below normalises the two into one attribute.
StepValue = Union[Flow, CustomObject, CustomField, ApexClass]


@dataclass
class StepResult:
    step: PlanStep
    value: StepValue
    repairs: int
    # The conversation that produced `value` - kept so a later repair round
    # (feeding Salesforce's own deploy failures back in) can continue it with
    # generator.refine()/.repair_from_salesforce() instead of starting over
    # with no memory of what was already tried.
    messages: List[Message]


def _run_one_step(provider: Provider, step: PlanStep, max_repairs: int) -> StepResult:
    generator_cls = _GENERATOR_BY_TYPE[step.artifact_type]
    generator = generator_cls(provider, max_repairs=max_repairs)
    raw = generator.generate(step.brief)
    value = raw.flow if isinstance(raw, GenerationResult) else raw.value
    return StepResult(step=step, value=value, repairs=raw.repairs, messages=raw.messages)


def execute_plan(
    provider: Provider, plan: Plan, max_repairs: int = DEFAULT_MAX_REPAIRS,
    parallel: bool = False,
) -> List[StepResult]:
    """
    Run every step of a validated Plan through its matching generator, layer
    by layer (see Plan.layers). A step never starts until every step it
    depends on has actually finished, since its `brief` may name an api_name
    that only exists once that earlier step has run.

    `parallel` (off by default) lets independent steps within one layer run
    concurrently instead of one at a time - a model call is blocking network
    I/O, so this can shorten a wide plan's wall time. It was tried on by
    default and rolled back: several requests racing the same rate-limited
    quota at once meant more of them hit a 429 and paid llm.py's backoff
    wait, which in practice cost more than the wall-clock time it saved, and
    it was suspected of hurting result quality too - plausible, since more
    concurrent load also means more retries/repairs happening under time
    pressure. Off by default until that trade-off is actually measured;
    still available to opt back into and compare.

    Concurrency, when enabled, is capped at _MAX_PARALLEL_STEPS rather than
    one thread per step in the layer - the exact contention above, just
    bounded rather than unbounded. Each step gets a fresh generator and
    conversation - nothing here threads context between generators, because
    a step's `brief` is written to be self-contained (that is the planner's
    job, decided once, not something to redo per step here). The shared
    `provider` itself is safe under concurrent use either way: Usage.add()
    and GeminiProvider's key rotation are both guarded against exactly this.

    Returns results in the plan's original order, not the order steps
    finished in, so a caller can zip them back up against plan.steps directly.
    """
    by_name: Dict[str, StepResult] = {}
    for layer in plan.layers():
        workers = min(len(layer), _MAX_PARALLEL_STEPS) if parallel else 1
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(_run_one_step, provider, step, max_repairs): step
                for step in layer
            }
            for future in as_completed(futures):
                step = futures[future]
                by_name[step.name] = future.result()

    return [by_name[step.name] for step in plan.steps]


def _rerun_step(
    provider: Provider, previous: StepResult, apply, max_repairs: int = DEFAULT_MAX_REPAIRS,
) -> StepResult:
    """
    Shared machinery behind repair_step and refine_step: unwrap a StepResult
    back into whatever shape its generator's own methods expect
    (GenerationResult for Flow, IRGenerationResult for everything else),
    call `apply(generator, prior)` to actually continue the conversation,
    then re-wrap the answer as a StepResult.
    """
    generator_cls = _GENERATOR_BY_TYPE[previous.step.artifact_type]
    generator = generator_cls(provider, max_repairs=max_repairs)

    if previous.step.artifact_type == "flow":
        prior = GenerationResult(
            flow=previous.value, messages=previous.messages, repairs=previous.repairs
        )
    else:
        prior = IRGenerationResult(
            value=previous.value, messages=previous.messages, repairs=previous.repairs
        )

    raw = apply(generator, prior)
    value = raw.flow if isinstance(raw, GenerationResult) else raw.value
    return StepResult(
        step=previous.step, value=value, repairs=raw.repairs, messages=raw.messages
    )


def repair_step(
    provider: Provider, previous: StepResult, failures: List[str],
    max_repairs: int = DEFAULT_MAX_REPAIRS,
) -> StepResult:
    """
    Re-run one step's generator with Salesforce's own deploy failures fed
    back in - reactive, after the org has rejected it. Continues the
    conversation that produced the step rather than starting fresh, via the
    repair_from_salesforce every generator already supports (FlowGenerator's
    own, or the one CustomObjectGenerator/CustomFieldGenerator/
    ApexClassGenerator inherit from IRGenerator).
    """
    return _rerun_step(
        provider, previous,
        lambda generator, prior: generator.repair_from_salesforce(prior, failures),
        max_repairs,
    )


def refine_step(
    provider: Provider, previous: StepResult, instruction: str,
    max_repairs: int = DEFAULT_MAX_REPAIRS,
) -> StepResult:
    """
    Re-run one step's generator with a change the person asked for - the
    proactive sibling to repair_step: "make this different" rather than
    "Salesforce rejected this." Same underlying refine() every generator
    supports, same conversation continuation.
    """
    return _rerun_step(
        provider, previous,
        lambda generator, prior: generator.refine(prior, instruction),
        max_repairs,
    )
