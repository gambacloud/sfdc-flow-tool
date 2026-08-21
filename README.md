# SFDC Flow Tool

Describe business logic in plain language, get a Salesforce Flow that deploys.
Or open a flow that already exists, ask what it does, and change it.

```
description ─┐
             ├─► IR ─► diagram ─► your approval ─► Flow XML ─► checkOnly ─► deploy
existing flow┘
```

The same pipeline also builds Custom Objects, Custom Fields, and Apex
Classes — describe several at once ("an Invoice object with an Amount field,
and a flow that totals it") and a planner works out how many pieces the
request needs, in what order, before generating each one. See
[Building more than a Flow](#building-more-than-a-flow).

## Install

Needs **Python 3.11+**. Node is optional — only for the Salesforce CLI.

```bash
git clone https://github.com/gambacloud/sfdc-flow-tool.git
cd sfdc-flow-tool; python -m venv .venv; .venv/Scripts/python.exe -m pip install -r requirements.txt
```

On macOS or Linux the interpreter is `.venv/bin/python` instead.

### 1. An LLM key

Create a file called `.env` next to `forge.py`:

```
GEMINI_API_KEY=your-key
```

`ANTHROPIC_API_KEY=sk-ant-...` or `OLLAMA_API_KEY=...` (Ollama Cloud) work too —
whichever key is present is the one used. `.env` is gitignored.

Use the file rather than a shell variable — a variable set with `$env:` or
`export` only lives in that one shell, so it silently does nothing in the next
one. A variable that *is* set still wins over the file.

**Never put a key in a source file.** It is the one place that gets committed.

### 2. An org (optional, but needed to validate or deploy)

```bash
npm install -g @salesforce/cli
sf org login web --alias dev
```

SFDC Flow Tool reads credentials from the CLI, so no token is ever typed into
the app or sent over HTTP. Without the CLI you can still design flows and
export the XML; `forge.py` will also take a session id typed at a hidden
prompt. Start the tool from a shell where `sf` resolves — PATH differs between
PowerShell and Git Bash.

### 3. Check it works

```bash
.venv/Scripts/python.exe -m pytest tests -q
```

~990 tests, none of which need a key, a network, or an org.

## Use it

### Web UI

```bash
.venv/Scripts/python.exe server.py
```

Open `http://localhost:8000`. Describe a flow, or pick one from the org. You
get a rendered diagram, an **Explain** tab that reads the flow back in prose,
and tabs for the generated Markdown, Flow XML, and IR. Change it by asking.
Approve, validate, deploy — with a link straight into Flow Builder when it
lands.

A third mode, **Build multiple things**, takes a request that needs more than
one Flow — plus, optionally, Objects, Fields, and Apex Classes — and reviews
every piece as one plan: an expandable card per step (a diagram for a Flow, a
field table for an Object/Field, the source for an Apex Class), approved and
deployed together as a single transaction.

### CLI

```bash
.venv/Scripts/python.exe forge.py "when an opportunity is won, mark its account hot" --org dev
```

| Flag | Effect |
|---|---|
| `--org [ALIAS]` | Credentials from the `sf` CLI instead of a prompt |
| `--out DIR` | Write `.flow-meta.xml`, `.md`, and `.ir.json` |
| `--no-validate` | Design only; never contacts the org |
| `--deploy` | Offer to deploy after validation passes |
| `--activate` | Deploy Active. Without it, flows deploy as Drafts |
| `--provider` | `gemini`, `anthropic`, or `ollama` (default: whichever key is set) |
| `--model` | Override the provider's default model |
| `--effort` | `medium` or `high` (default `medium`) |

## How it works

An LLM asked to write Flow XML directly gets it wrong almost every time —
empty `<object>` tags, element names with spaces, connectors pointing at
elements that don't exist. It looks plausible and fails validation.

So the model never writes XML. It produces an **IR**: a Pydantic document that
makes invalid flows unrepresentable — a path that ends is `next: null`, not a
reference to an "End" element Flow XML doesn't have; a condition is
`(left, operator, typed value)`, not a formula string; every connector target
is checked before any metadata exists.

Both the diagram you approve and the XML that deploys come from that same IR,
so they cannot disagree. When Salesforce does reject something, the error goes
back to the model and the corrected flow comes back through approval again.

Nothing reaches the org before you approve it, enforced by the server rather
than the browser: every change bumps the flow's version, and validate/deploy
refuse unless the version you approved is still current.

## Opening an existing flow

The tool retrieves the metadata, parses it into IR, and draws it — from there
it behaves like any other flow: explain, refine, approve, redeploy.

A flow using orchestration stages, transforms, or other constructs the IR
can't hold is **refused, not drawn approximately**, naming what it found.
Drawing it anyway would show a diagram of a different flow than the one in the
org, and editing from that diagram would delete those parts on deploy.

`parse(generate(ir)) == ir` is asserted for every element type in
`tests/test_roundtrip.py` — that property is what makes an edit round-trip
safe.

A **refusal** means the IR cannot hold the flow. A **warning** means it will
deploy but you may not have meant it — an element nothing reaches, for
example. Warnings sit above the diagram in the approval document. The line
between the two isn't taste: unreachable elements were a refusal until a live,
Active flow in Salesforce's own sample apps turned out to have one — anything
the org accepts has to be openable.

## Checking the metadata is right

```bash
.venv/Scripts/python.exe verify.py --org dev
```

The test suite proves the IR agrees with itself; it can't prove the XML is
right, because Salesforce is the only authority on that. `verify.py` builds
every construct through the IR and round-trips it under `checkOnly` (**shapes**),
and separately records what the org actually does with things the IR refuses
(**guards** — no org needed):

```
  pause
    ok    a Pause with no time to resume at
            the org deploys, and the flow never resumes
    ok    a Pause whose parameter is misspelled
            the org deploys - it took AlarmTimeX without a word
```

Salesforce validates far less than it looks like — condition logic is never
parsed (`banana` deploys), and a component output assigned to a
non-existent variable deploys, runs, and throws the value away. The guards are
why several validators exist at all.

Read-only — `checkOnly` validates and discards, nothing is created, updated or
deployed. Standard objects and components only, so it works in any org. About
a minute and a half, five cases at a time. The guards and round trips also run
in the ordinary test suite, so only the org half is new here.

## Measuring the gap

```bash
.venv/Scripts/python.exe survey.py --org dev
```

Retrieves every flow in the org, parses each, and reports what actually blocks
the ones it can't take:

```
47 flows:  31 parse, 16 refused (65% covered)

What blocks them:

  seen  frees
     9      6  ############################  child:visibilityRule
     4      1  ############                  element:collectionProcessors

  seen  = flows this appears in
  frees = flows that would parse if only this were supported

Biggest single win: supporting child:visibilityRule would unblock 6 of 47 flows on its own.
```

**Plan from `frees`, not `seen`** — a flow blocked by several things is freed
by none of them individually. Flows nobody in the org authored (Salesforce's
own, or anything with a package namespace) are counted but excluded from the
recommendation.

Read-only. `--save DIR` keeps retrieved files for offline re-runs with `--dir`;
`--json` writes the full report. No org handy? `harvest.py` pulls flows from
public GitHub repos into a corpus:

```bash
.venv/Scripts/python.exe harvest.py trailheadapps
.venv/Scripts/python.exe survey.py --dir corpus -v
```

A demo corpus skews toward screen components and away from record-triggered
automation — treat its zeros (nothing needing a scheduled path, say) as a gap
in the corpus, not evidence the feature is rare. Prefer a real org's numbers
when you have them.

The line to watch is **"parsed but did NOT survive a round trip"** — a flow
that looks editable but would lose something on the way back out. It should
always be empty.

## What it covers

Record-triggered, platform-event-triggered, autolaunched, screen, and
orchestrator flows:

| | |
|---|---|
| Assignment | Set variable values, add to or remove from a collection, count one |
| Decision | Branch on structured conditions, combined with `and`, `or`, or an expression like `1 OR (2 AND 3)` |
| Get / Create / Update / Delete Records | Get can name its fields and store the records three ways — kept, into a variable, or a field at a time; Create can save the new Id |
| Loop | Reads the item as the loop's own name, or into a variable you name |
| Collection Filter / Sort | Reshapes a collection already in memory, no query |
| **Scheduled paths** | Run a branch later — three days after the trigger, a day before a date field, or straight away in its own transaction |
| **Pause** | Stop and resume at a time, at a date on a record, or when a platform event arrives |
| Subflow | Calls another flow, in or out of its own transaction |
| **Action** | Email alerts, Send Email, Apex invocables, Chatter posts — anything with an `actionType` |
| **Custom Error** | Rejects the record being saved, optionally pinned to a field — record-triggered flows only |
| **Screen** | Text, inputs, pickers, sections and columns, conditional visibility, validation rules, LWC/Aura components |
| **Choices** | Fixed options, options built from records/a collection/a picklist, or a free-text "other" answer |
| **Text templates, constants, formulas** | Reusable text with merge fields; a fixed value; a value recomputed from an expression |
| **Orchestration Stage** | A chain of Background/Interactive steps — a step runs an autolaunched or screen flow, an Interactive one assigned to a user, group, queue, or resource |

Formula **fields** on an object work anywhere a reference does
(`$Record.Margin__c`); formula **resources** defined inside a flow work too.
A formula's expression is the one free-form string the org does not check at
all — a bad one deploys clean, so the approval document quotes it verbatim
because a person is the only check there is.

**Scheduled path vs. Pause** are the same request — "do this later" — answered
by whichever started the flow: a scheduled path only works after-save on a
record trigger, a Pause only on a plain autolaunched flow. Each is refused
everywhere the other belongs, and the IR names the other one when it refuses.

**Custom condition logic** — a decision outcome, record filter, or entry
condition can combine with `1 OR (2 AND 3)` instead of plain and/or. Salesforce
checks none of it (an unclosed bracket or a number past the end of the list
both deploy), so the IR does. The one thing left deliberately alone: a
condition the expression never names, which is odd but not broken.

### Deliberately out of scope

**Migrated Workflow Rules and Process Builder platform-event processes**
(`processType: Workflow` / `CustomEvent`) — legacy migration artefacts nobody
authors today. A survey still counts them, marked `(out of scope)`, so the
count is evidence for the decision rather than a recommendation. What people
build today — an autolaunched flow with `triggerType: PlatformEvent` — **is**
supported.

**Approval steps, MuleSoft steps, and flow-based entry/exit criteria** in an
Orchestrator — real, but each its own mechanism the way Custom Error's
trigger-type restriction is: an Approval or MuleSoft step needs a whole
separate confirmed shape, and criteria-by-flow requires the referenced flow
to be `processType: EvaluationFlow` with a Boolean output variable literally
named `isOrchestrationConditionMet`. Only Background and Interactive steps
with structured entry/exit conditions are modelled.

**Password fields and object-provided screen fields** are refused rather than
approximated, the same as everything else this build doesn't model: every
screen field shares one shape, so an unmodelled type would otherwise draw as a
plain input box and deploy as one.

Four references have no connector for anything else to catch, so the IR checks
them by name instead: a screen input's own name (shared with elements,
variables, choices, and choice sets — **one namespace**); a picker's option
list; a component output's landing variable (**a missing one deploys and
silently discards the value**); and a visibility rule's condition (**a rule
naming something undefined also deploys**, and the field just never appears).

## Building more than a Flow

The **Build multiple things** mode adds three sibling artifact types, each
with its own IR, validated the same way the Flow IR is — invalid shapes are
unrepresentable before a single byte of metadata exists:

- **Custom Object** — label, plural label, a Text or AutoNumber name field,
  sharing model.
- **Custom Field** — Text, Number, Checkbox, Picklist, Lookup, or
  MasterDetail, each restricted to the properties that actually apply to it
  (a Picklist needs values, a Lookup needs a target object, a MasterDetail
  can't be marked optional). `__c` is appended automatically.
- **Apex Class** — the one IR that's mostly free text (`body`), since Apex is
  code, not a structural graph. Two things are still checked before a repair
  round: brace/paren/bracket balance, and that the class declared in `body`
  is actually named `api_name`.

A planner (`flowtool/planner.py`) reads the request first and decides how
many steps it needs and in what order — a request that only needs one Flow
still produces a one-step plan, so this isn't a separate mode's worth of extra
ceremony for the common case. Each step then runs through its own generator
(`FlowGenerator`, `CustomObjectGenerator`, `CustomFieldGenerator`,
`ApexClassGenerator`), and every approved step deploys together in one
Metadata API transaction — the reason a Flow can reference a field from the
same request: it doesn't exist in the org until this deploy creates it.

**What this doesn't do yet.** Apex validation is heuristic only — brace
balance and a name check, not a real compile. A class that passes the
heuristic can still fail to deploy on an unresolved symbol or a type error;
that only surfaces once you validate against the org. `verify_object_apex.py`
(a repo-root dev script, not part of the shipped tool — same idea as
`verify.py`) checks real Object/Field/Apex/bundle shapes against a live org,
including two shapes that specifically deploy an Object, its Field, and an
Apex Class together in one transaction.

## Layout

| Module | Role |
|---|---|
| `flowtool/ir.py` | The Flow IR — the single source of truth |
| `flowtool/xmlgen.py` | Flow IR → Flow XML, deterministic, with auto-layout |
| `flowtool/parse.py` | Flow XML → IR, or a refusal naming what it can't model |
| `flowtool/mermaid.py` | Flow IR → Mermaid + Markdown |
| `flowtool/ir_object.py` | The Custom Object / Custom Field IR |
| `flowtool/xmlgen_object.py` | Object/Field IR → metadata XML |
| `flowtool/ir_apex.py` | The Apex Class IR, plus its heuristic syntax checks |
| `flowtool/xmlgen_apex.py` | Apex Class IR → `.cls` + `.cls-meta.xml` |
| `flowtool/planner.py` | Request → an ordered list of typed steps, run through the matching generator |
| `flowtool/llm.py` | Text → IR, bring-your-own-key, self-repairing — the generic engine behind every IR type above |
| `flowtool/sfdc.py` | Metadata API: list, retrieve, validate, deploy — single- or multi-type |
| `flowtool/orgs.py` | Reads org credentials from the `sf` CLI |
| `flowtool/config.py` | Loads `.env` |
| `forge.py` | The Flow pipeline, as a CLI |
| `server.py` | The Flow pipeline and the multi-artifact plan pipeline, over HTTP |
| `survey.py` | Measures how much of an org this build can model |
| `verify.py` | Checks every Flow metadata shape against a real org |
| `verify_object_apex.py` | Same, for Object/Field/Apex shapes and mixed-type bundles |
| `harvest.py` | Collects flows from public repos to survey against |
| `diagnose.py` | Isolates where org authentication breaks |

### Adding a provider

Implement `complete_json` and `complete_text`, then register it in
`PROVIDERS`. Validation happens once, centrally — not the provider's concern.

The real work is the schema dialect: providers disagree on which JSON Schema
keywords they accept (Anthropic takes `const`, Gemini needs a single-value
`enum` and rejects `default`/`discriminator`). `flowtool/llm.py` has an adapter
per dialect; tests assert each emits only documented keywords and doesn't eat
field names on the way through.

Providers also cap response schema *size* — Gemini counts every `$ref`
inlined, so a definition referenced from six places costs six times, and the
IR is past it: the schema goes as prompt text instead rather than being
pruned, since anything missing from the schema can't be emitted at all. The
cap is undocumented; over it, the API says only "Request contains an invalid
argument." `tests/test_gemini_budget.py` records what it actually counts, and
`GEMINI_PROPERTY_BUDGET` is where to lower it if it moves.

## Troubleshooting

| Symptom | Cause |
|---|---|
| `No LLM key found` | `.env` missing, misnamed, or in the wrong directory — the message prints the full path it wants |
| `sf CLI is not on PATH` / org list empty | Started from a shell where `sf` doesn't resolve |
| `INVALID_SESSION_ID` | Run `python diagnose.py --org dev`; it isolates which layer is failing |
| A flow won't open | The refusal names the construct — a gap in the IR, not a broken flow |
| `Request contains an invalid argument` (Gemini) | Response schema over Gemini's undocumented size cap. Handled automatically; if you see it, lower `GEMINI_PROPERTY_BUDGET` in `flowtool/llm.py` |
| `RESOURCE_EXHAUSTED` (Gemini) | Free tier is 20 requests/day/model. Wait, switch `--model`, or add billing |
