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

`ANTHROPIC_API_KEY=sk-ant-...` works too — whichever key is present is the one
used. `.env` is gitignored.

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

408 tests, none of which need a key, a network, or an org.

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
| `--provider` | `gemini` or `anthropic` (default: whichever key is set) |
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

A flow using waits, formula resources, or other constructs the IR can't hold is
**refused, not drawn approximately**, naming what it found. Skipping those parts
would show a diagram of a different flow than the one in the org, and editing
from that diagram would delete them on deploy.

`parse(generate(ir)) == ir` is asserted for every element type in
`tests/test_roundtrip.py`. That property is what makes an edit round-trip safe.

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
     9      6  ############################  element:waits
     4      1  ############                  element:formulas
     2      0  ######                        child:queriedFields
     1      0  ###                           screen_field:ComponentInstance

  seen  = flows this appears in
  frees = flows that would parse if only this were supported

Biggest single win: supporting element:waits would unblock 6 of 47 flows on its own.
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
corpus — Salesforce's own [sample apps](https://github.com/trailheadapps) carry
real flows under `force-app/main/default/flows/`. Fifteen of them is what showed
that the top three blockers were not features at all but three unmodelled
attributes of `variables`, and that the screen components planned next would
have freed nothing. Pool several checkouts into one directory and survey it in
one go — but watch for two orgs using the same flow name, which silently
overwrites.

The line to watch is **"parsed but did NOT survive a round trip"**. That means a
flow looks editable and isn't: something would be lost on the way back out. It
should always be empty.

## What it covers

Record-triggered, autolaunched, and screen flows:

| | |
|---|---|
| Assignment | Set variable values |
| Decision | Branch on structured conditions |
| Get / Create / Update / Delete Records | Get can name its fields and store into a variable; Create can save the new Id |
| Loop | |
| Subflow | |
| **Action** | Email alerts, Send Email, Apex invocables, Chatter posts — anything with an `actionType` |
| **Screen** | Display text, input fields, long text areas, and pickers, with defaults |
| **Choices** | Fixed options, or options built from records or a picklist |
| **Text templates** | Reusable text with merge fields, for an email body or a message |
| **Constants and formulas** | A fixed value, or one recomputed from an expression |

Formula **fields** on an object work anywhere a reference does (`$Record.Margin__c`).
Formula **resources** defined inside a flow work too.

A formula's expression is the one free-form string in the IR, and the one thing
nothing verifies. An expression calling a function that does not exist, and
referencing a resource that does not exist, was accepted by the org under
`checkOnly` — so neither the IR nor the validation step will catch a bad
formula. That is why the approval documentation quotes every expression
verbatim: for a formula, a person reading it is the only check there is.

### Deliberately out of scope

**Migrated Workflow Rules** (`processType: Workflow`). They are a legacy
migration artefact, not something anyone authors today, and supporting them
would mean carrying their shape forever. A survey keeps counting them — the
count is the evidence for the decision — but marks them `(out of scope)` and
never recommends them. Before that, the headline recommended building them
every single run.

**Screen flows** are built as far as stage 2 of three:

1. ✅ `processType: Flow`, plus screens carrying display text, input fields, and
   long text areas. Covers most of what a simple screen flow does.
2. ✅ Choices and dynamic choice sets — radio buttons, dropdowns, checkboxes and
   multi-selects, with options written out or built from records or a picklist.
3. LWC and Aura extensions on a screen.

Everything the IR cannot yet hold is refused rather than approximated, which is
what makes a partial implementation safe to ship: a `ComponentInstance` field has
exactly the same shape as a text input, so it is refused **by name** rather than
read as one and silently deployed as one.

Two references have no connector to travel along, so nothing else would catch
them, and both are enforced in the IR:

- A screen input is read by its own name (`{!Customer_Email}`), so screen fields,
  elements, variables, choices and choice sets share **one namespace**.
- A picker names the options it offers. One that names an option the flow never
  defines is structurally fine and shows the user an empty list.

Where the metadata's own rules were not obvious, the org settled them under
`checkOnly` rather than being guessed: a multi-select field's data type is
`String`, not `Multipicklist`, and a picklist choice set's own data type must be
`Picklist`.

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
