# SFDC Flow Tool

Describe business logic in plain language, get a Salesforce Flow that deploys.
Or open a flow that already exists, ask what it does, and change it.

```
description ─┐
             ├─► IR ─► diagram ─► your approval ─► Flow XML ─► checkOnly ─► deploy
existing flow┘
```

## Install

Needs **Python 3.11+**. Node is optional — only for the Salesforce CLI.

```bash
git clone https://github.com/gambacloud/sfdc-flow-tool.git
```

```bash
cd sfdc-flow-tool; python -m venv .venv; .venv/Scripts/python.exe -m pip install -r requirements.txt
```

On macOS or Linux the interpreter is `.venv/bin/python` instead.

### 1. An LLM key

Create a file called `.env` next to `forge.py`:

```
GEMINI_API_KEY=your-key
```

`ANTHROPIC_API_KEY=sk-ant-...` or `OLLAMA_API_KEY=...` (Ollama Cloud) work too
— whichever key is present is the one used. `.env` is gitignored.

Use the file rather than a shell variable. A variable set with `$env:` or
`export` only lives in that one shell, so setting it in one command and running
the tool in the next silently does nothing. An environment variable that *is*
set still wins over the file.

**Never put a key in a source file.** It is the one place that gets committed.

### 2. An org (optional, but needed to validate or deploy)

```bash
npm install -g @salesforce/cli
```

```bash
sf org login web --alias dev
```

SFDC Flow Tool reads credentials from the CLI, so no token is ever typed into the app
or sent over HTTP. Without the CLI you can still design flows and export the XML;
`forge.py` will also take a session id typed at a hidden prompt.

Start SFDC Flow Tool from a shell where `sf` resolves — PATH differs between
PowerShell and Git Bash.

### 3. Check it works

```bash
.venv/Scripts/python.exe -m pytest tests -q
```

788 tests, none of which need a key, a network, or an org.

## Use it

### Web UI

```bash
.venv/Scripts/python.exe server.py
```

Open `http://localhost:8000`.

Describe a flow, or pick one from the org. You get a rendered diagram, an
**Explain** tab that reads the flow back to you in prose, and tabs for the
generated Markdown, Flow XML, and IR. Change it by asking. Approve, validate,
deploy — with a link straight into Flow Builder when it lands.

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
| `--effort` | `low` … `max` (default `high`) |

## How it works

An LLM asked to write Flow XML directly gets it wrong almost every time — empty
`<object>` tags, element names with spaces, connectors pointing at elements that
don't exist. The XML looks plausible and fails validation.

So the model never writes XML. It produces an **IR**: a Pydantic document that
makes invalid flows unrepresentable. A path that ends is `next: null`, not a
reference to an "End" element that Flow XML doesn't have. A condition is
`(left, operator, typed value)`, not a formula string. Every connector target is
checked before any metadata exists.

Both the diagram you approve and the XML that deploys come from that same IR, so
they cannot disagree. When Salesforce does reject something, the error goes back
to the model and the corrected flow comes back through approval again.

Nothing reaches the org before you approve it, and the gate is enforced by the
server rather than the browser: every change bumps the flow's version, and
validate and deploy refuse unless the version you approved is still current.

## Opening an existing flow

SFDC Flow Tool retrieves the metadata, parses it back into IR, and draws it. From
there it behaves like any other flow — explain, refine, approve, redeploy.

A flow using orchestration stages, collection filters, transforms, or other
constructs the IR can't hold is **refused, not drawn approximately**, naming
what it found. Skipping those parts would show a diagram of a different flow
than the one in the org, and editing from that diagram would delete them on
deploy.

`parse(generate(ir)) == ir` is asserted for every element type in
`tests/test_roundtrip.py`. That property is what makes an edit round-trip safe.

### Refusals and warnings are different things

A **refusal** means the IR cannot hold the flow. A **warning** means it will
deploy and you may not have meant it — an element nothing reaches, for example.
Warnings appear at the top of the approval document, above the diagram, and the
model is told about them once so it can fix a connector it forgot.

The line between them is not taste. Unreachable elements were a refusal until a
live, Active flow in Salesforce's own sample apps turned out to have one, so the
tool could not open a flow that was working in production. **Anything the org
accepts has to be openable**; the most a tool can do about it is say so.

## Checking the metadata is right

```bash
.venv/Scripts/python.exe verify.py --org dev
```

The test suite proves the IR agrees with itself. It cannot prove the XML is
right, because the only authority on that is Salesforce. `verify.py` asks:

- **shapes** — every construct the compiler emits, built through the IR,
  round-tripped, then sent to the org under `checkOnly`. A failure means what we
  generate is not what Salesforce takes.
- **guards** — things the IR refuses, each recording what the org does with the
  same flow. These need no org and run anyway.

The guards are the more valuable half, because most of them read like this:

```
  pause
    ok    a Pause with no time to resume at
            the org deploys, and the flow never resumes
    ok    a Pause whose parameter is misspelled
            the org deploys - it took AlarmTimeX without a word
```

Salesforce validates far less than it looks like it does. Condition logic is
never parsed — `banana` deploys. A component output assigned to a variable that
does not exist deploys, runs, and throws the value away. Those are not edge
cases; they are the reason several validators exist, and without this file the
reason is a comment nobody can re-check.

Read-only: `checkOnly` means Salesforce validates and discards. Nothing is
created, updated or deployed, and no flow of yours is touched. The cases use
standard objects and standard components only, so they work in any org. Expect
about a minute and a half — each case is a full round trip to the Metadata API,
five at a time.

The guards and the round trips also run in the ordinary test suite, so they
cannot rot between org runs; the org half is the part `verify.py` adds.

## Measuring the gap

```bash
.venv/Scripts/python.exe survey.py --org dev
```

Retrieves every flow in the org in one call, parses each, and reports what
actually blocks the ones it can't take:

```
47 flows:  31 parse, 16 refused (65% covered)

What blocks them:

  seen  frees
     9      6  ############################  child:visibilityRule
     4      1  ############                  element:collectionProcessors
     2      0  ######                        screen_field:RegionContainer
     1      0  ###                           element:orchestratedStages

  seen  = flows this appears in
  frees = flows that would parse if only this were supported

Biggest single win: supporting child:visibilityRule would unblock 6 of 47 flows on its own.
```

The two columns differ because a flow blocked by several things is freed by
none of them individually. **`frees` is the one to plan from** — the first real
survey's headline recommended a blocker that appeared in three flows and would
have unblocked none. Flows nobody in the org authored (Salesforce's own, or
anything with a package namespace) are counted but kept out of the
recommendation.

Read-only — nothing is written to the org. `--save DIR` keeps the retrieved
files so later runs can go offline with `--dir`, and `--json` writes the whole
report for tracking coverage over time.

**No org handy?** `--dir` reads any SFDX checkout, so public repos work as a
corpus. `harvest.py` collects the flows out of them — Salesforce's own
[sample apps](https://github.com/trailheadapps) are the obvious place to start:

```bash
.venv/Scripts/python.exe harvest.py trailheadapps
```

```bash
.venv/Scripts/python.exe survey.py --dir corpus -v
```

That is 91 flows from 12 of the 29 repos in the org; the other 17 ship no flow
metadata at all. It takes one GitHub API call per repo, unauthenticated, so 60
repos an hour — over the limit it says so per repo and skips what it already
has, so running it again an hour later picks up where it stopped.

The corpus drove nine changes in a row, taking coverage from 0 to 72 flows. It
showed that the top three blockers were not features at all but three unmodelled
attributes of `variables`.

It also showed the limit of the `frees` column. Screen components arrive as four
blockers that always travel together, so each one scored `frees: 0` and the
report said "no single addition unblocks anything" — correct, and useless as a
plan. **Read `frees` alongside the cheapest-flows list underneath it**, which
counts how many additions each refused flow needs and names them. Four blockers
that appear on the same line of that list are one piece of work, not four.

One directory per repo, which `harvest.py` does and the survey relies on.
Pooling them flat keeps only one of any two flows sharing a name, and
`purealoe` and `purealoe-lwc` both ship an `IrrigationManagement` that is not
the same flow. Five of these 91 vanished that way, and coverage read five points
higher than it was.

Sample apps are showcases — far heavier on screen components than a working org,
and much lighter on record-triggered automation. Prefer a real org's numbers
when you have them.

**And read the zeroes.** Not one of the 91 flows contains a scheduled path or a
Pause element, so both scored `frees: 0` and the survey never once suggested
them. That is not evidence they are rare; it is evidence that a demo app has
nothing to wait for. "Chase it in three days" is among the first things anyone
asks a flow to do. A corpus tells you what is *in* it, and a gap in the corpus
looks exactly like a gap in the product — so the survey ranks what to build only
among things somebody has actually written down.

The line to watch is **"parsed but did NOT survive a round trip"**. That means a
flow looks editable and isn't: something would be lost on the way back out. It
should always be empty.

## What it covers

Record-triggered, platform-event-triggered, autolaunched, and screen flows:

| | |
|---|---|
| Assignment | Set variable values, add to or remove from a collection, count one |
| Decision | Branch on structured conditions, combined with `and`, `or`, or an expression like `1 OR (2 AND 3)` |
| Get / Create / Update / Delete Records | Get can name its fields and store the records three ways — kept, into a variable, or a field at a time; Create can save the new Id |
| Loop | Reads the item as the loop's own name, or into a variable you name |
| **Scheduled paths** | Run a branch later — three days after the trigger, a day before a date field, or straight away in its own transaction |
| **Pause** | Stop and resume at a time, at a date on a record, or when a platform event arrives |
| Subflow | |
| **Action** | Email alerts, Send Email, Apex invocables, Chatter posts — anything with an `actionType`, in the current transaction or its own |
| **Screen** | Display text, input fields, long text areas, and pickers, with defaults |
| **Screen layout** | Sections and columns, conditional visibility, validation rules, help text |
| **Choices** | Fixed options, or options built from records or a picklist |
| **Screen components** | LWC and Aura components, with their inputs and outputs |
| **Text templates** | Reusable text with merge fields, for an email body or a message |
| **Constants and formulas** | A fixed value, or one recomputed from an expression |

Formula **fields** on an object work anywhere a reference does (`$Record.Margin__c`).
Formula **resources** defined inside a flow work too.

A formula's expression is the one free-form string the org does not check at
all. An expression calling a function that does not exist, and referencing a
resource that does not exist, was accepted under `checkOnly` — so neither the IR
nor the validation step will catch a bad formula. That is why the approval
documentation quotes every expression verbatim: for a formula, a person reading
it is the only check there is.

### Two ways to do something later, and no choice about which

Salesforce has both, and allows each in exactly the place the other is refused:

| | Scheduled path | Pause |
|---|---|---|
| Lives on | the flow's start | its own element |
| Allowed in | a record-triggered flow, after save | a plain autolaunched flow |
| Refused in | anything else, by the org | a screen flow, and any record-triggered flow |

So "chase it in three days" is a scheduled path when a record change started it,
and a Pause when Apex or another flow did. The request is identical and the
answer is not, which is why the IR names the other one when it refuses:
reaching for a Pause in a record-triggered flow is a reasonable mistake.

### Custom condition logic

Conditions normally combine with `and` or `or`. They can also combine with an
expression over their positions — `1 OR (2 AND 3)`, numbered from 1 in the order
listed — on a decision outcome, on record filters, and on a flow's entry
conditions.

Salesforce checks none of it. An expression naming a condition past the end of
the list, an unclosed bracket, and the literal string `banana` all pass
`checkOnly` and deploy. So the IR parses the expression and refuses anything it
cannot read or whose numbers do not line up.

The case that actually happens is renumbering. Editing a flow renumbers its
conditions, so an outcome that loses its second condition leaves
`1 OR (2 AND 3)` pointing one place past the end — a flow that still deploys and
takes the wrong branch. That is what this check is for.

One thing is deliberately allowed: a condition the expression never names. It is
evaluated and ignored, which is odd but not broken, and refusing it would be
guessing at intent rather than following a rule. The approval document says so
in the margin instead, and a person decides.

The expression is never reformatted on the way back out. `1 or 2` returns as
`1 or 2`, because a reviewer comparing against the org version reads it
character by character.

### Deliberately out of scope

**Migrated Workflow Rules** (`processType: Workflow`) and **migrated Process
Builder platform-event processes** (`processType: CustomEvent`). Both are legacy
migration artefacts, not something anyone authors today, and supporting them
would mean carrying their shape forever. A survey keeps counting them — the
count is the evidence for the decision — but marks them `(out of scope)` and
never recommends them. Before that, the headline recommended building them every
single run.

`CustomEvent` is worth spelling out, because the name makes it look like the
platform-event trigger and it is not — the org refuses to let one carry that
trigger at all: *"Flows of type CustomEvent can't have the trigger type
PlatformEvent."* Both in the public corpus are Process Builder migrations, and
the event they listen for is recorded only inside `processMetadataValues`, which
is Process Builder's own commentary and is neither read nor written here. Parsing
one and deploying it back would drop the record of which event starts it — a flow
that looks editable and loses something on the way out.

What people build today is an autolaunched flow with `triggerType:
PlatformEvent`, and **that is supported**: set the start's `object` to the event
and read its fields as `$Record`. It takes no `record_trigger_type`, because an
event is only ever published.

**Screen flows** are complete as far as the planned stages went: text and
inputs, choices, LWC and Aura components, and then sections, columns,
conditional visibility, validation rules and help text.

Sections are the one place the shape of the IR changed rather than grew. A
section holds columns and a column holds fields, so a screen field became a
tree — and every flow-level check written before that walked the field list
exactly one level deep. A field inside a column would have had its name
unchecked for collisions, its choices unresolved and its component outputs
unchecked for anywhere to land, all silently. `Screen.all_fields()` exists so
that nothing has to remember the nesting twice.

Sections nest exactly two deep, which is Salesforce's rule and not a
simplification: *"A RegionContainer screen field can't be a child of a Region
screen field."*

What remains unmodelled — password fields, object-provided fields — is refused
rather than approximated. That is what makes a partial implementation safe to
ship: every screen field has the same children, so an unmodelled type read as an
input box would draw as one and deploy as one.

Four references have no connector to travel along, so nothing else would catch
them, and all three are enforced in the IR:

- A screen input is read by its own name (`{!Customer_Email}`), so screen fields,
  elements, variables, choices and choice sets share **one namespace**.
- A picker names the options it offers. One that names an option the flow never
  defines is structurally fine and shows the user an empty list.
- A component output names the variable it lands in. **Salesforce deploys a
  missing one without complaining** and then discards the value — `checkOnly`
  passes, the component runs, and the result goes nowhere.
- A field shown conditionally reads other resources by name. A rule naming
  something the flow never defines **also deploys**, and the field then never
  appears — which is indistinguishable from one you meant to hide.

Where the metadata's own rules were not obvious, the org settled them under
`checkOnly` rather than being guessed: a multi-select field's data type is
`String`, not `Multipicklist`; a picklist choice set's own data type must be
`Picklist`; and a component returns its outputs either through
`outputParameters` or through `storeOutputAutomatically`, never both — *"You
can't use the storeOutputAutomatically field with the outputParameters field."*

The org is stricter about components than about anything else, which is the one
place that helps: it checks the component name, every input name and every
output name against the component's real signature and rejects each by name. A
component the model invents fails validation rather than deploying. The IR still
tells it not to invent one.

## Layout

| Module | Role |
|---|---|
| `flowtool/ir.py` | The IR — the single source of truth |
| `flowtool/xmlgen.py` | IR → Flow XML, deterministic, with auto-layout |
| `flowtool/parse.py` | Flow XML → IR, or a refusal naming what it can't model |
| `flowtool/mermaid.py` | IR → Mermaid + Markdown |
| `flowtool/llm.py` | Text → IR, bring-your-own-key, self-repairing |
| `flowtool/sfdc.py` | Metadata API: list, retrieve, validate, deploy |
| `flowtool/orgs.py` | Reads org credentials from the `sf` CLI |
| `flowtool/config.py` | Loads `.env` |
| `forge.py` | The pipeline, as a CLI |
| `server.py` | The pipeline, over HTTP |
| `survey.py` | Measures how much of an org this build can model |
| `verify.py` | Checks every metadata shape against a real org |
| `harvest.py` | Collects flows from public repos to survey against |
| `diagnose.py` | Isolates where org authentication breaks |

### Adding a provider

Implement `complete_json` and `complete_text`, then register it in
`PROVIDERS`. Validation is not your concern — it happens once, centrally.

The real work is the schema dialect. Providers disagree on which JSON Schema
keywords they accept: Anthropic takes `const`, Gemini needs a single-value `enum`
and rejects `default` and `discriminator`. `flowtool/llm.py` has an adapter per
dialect, and the tests assert each one emits only keywords that provider
documents — and that neither eats your field names on the way through.

Providers also cap how *large* a response schema may be. Gemini's cap counts the
schema with every `$ref` inlined, so a definition referenced from six places
costs six times; the IR is past it, and the schema is sent as prompt text
instead. It is **not** pruned to fit: anything missing from the schema cannot be
emitted by the model, so refining an imported flow would delete exactly those
parts on the way back out. Output is validated against the IR either way.

The cap is undocumented — over it, the API says only "Request contains an invalid
argument", naming neither the schema nor the size. `tests/test_gemini_budget.py`
records what it actually counts, and `GEMINI_PROPERTY_BUDGET` is where to lower
it if it moves.

## Troubleshooting

| Symptom | Cause |
|---|---|
| `No LLM key found` | `.env` missing, misnamed, or in the wrong directory — the message prints the full path it wants |
| `sf CLI is not on PATH` | Started from a shell where `sf` doesn't resolve |
| Org list empty | Same |
| `INVALID_SESSION_ID` | Run `python diagnose.py --org dev`; it isolates which layer is failing |
| A flow won't open | The refusal names the construct. That's a gap in the IR, not a broken flow |
| `Request contains an invalid argument` (Gemini) | The response schema is over Gemini's undocumented size cap. Handled automatically; if you see it, lower `GEMINI_PROPERTY_BUDGET` in `flowtool/llm.py` |
| `RESOURCE_EXHAUSTED` (Gemini) | Free tier is 20 requests per day per model. Wait, switch `--model`, or add billing |
