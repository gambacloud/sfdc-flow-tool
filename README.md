# FlowForge

Describe business logic in plain language. Get a Salesforce Flow that deploys.

```
description -> IR -> graph -> your approval -> Flow XML -> checkOnly -> deploy
```

## Why it works this way

An LLM asked to write Flow XML directly gets it wrong almost every time — empty
`<object>` tags, element names with spaces, connectors pointing at elements that
don't exist. The XML looks plausible and fails validation.

So the model never writes XML. It produces an **IR**: a Pydantic document that
makes invalid flows unrepresentable. A path that ends is `next: null`, not a
reference to an "End" element that Flow XML doesn't have. A condition is
`(left, operator, typed value)`, not a formula string. Every connector target is
checked before any metadata exists.

Both the diagram you approve and the XML that deploys are generated from that
same IR, so they cannot disagree.

When Salesforce does reject something, the error goes back to the model and the
corrected flow comes back through approval again.

## Setup

```bash
git clone <this repo> && cd sfdc-flow-forge
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements.txt
```

Put your LLM key in a `.env` file next to `forge.py`:

```
GEMINI_API_KEY=your-key
# or: ANTHROPIC_API_KEY=sk-ant-...
```

Whichever key is present is the provider that gets used; override with
`--provider gemini|anthropic` and `--model <id>`.

`.env` is gitignored. Use the file rather than a shell variable — a variable set
with `$env:` or `export` only lives in that one shell, so setting it in one
command and running the tool in the next silently does nothing. An environment
variable that *is* set still wins over the file.

**Never put a key in a source file.** It is the one place that gets committed.

For org access, either install the Salesforce CLI:

```bash
npm install -g @salesforce/cli
```

```bash
sf org login web --alias dev
```

…or skip it and paste a session id at the prompt (hidden input, never stored).

## Use

```bash
.venv/Scripts/python.exe forge.py "when an opportunity is won, mark its account hot" --org dev
```

You get a Mermaid graph and a summary, then `approve` / `refine` / `quit`.
Refining edits the IR, so the graph and the XML stay in step. Only after you
approve does anything reach the org, and only as `checkOnly` — deploying needs
`--deploy` **and** a second confirmation.

| Flag | Effect |
|---|---|
| `--org [ALIAS]` | Credentials from the `sf` CLI instead of a prompt |
| `--out DIR` | Write `.flow-meta.xml`, `.md`, and `.ir.json` |
| `--no-validate` | Design only; never contacts the org |
| `--deploy` | Offer to deploy after validation passes |
| `--provider` | `gemini` or `anthropic` (default: whichever key is set) |
| `--model` | Override the provider's default model |
| `--effort` | `low` … `max` (default `high`) |

### Adding a provider

Implement `complete_json(system, messages, schema)` and register it in
`forge.PROVIDERS`. Validation is not your concern — it happens once, centrally,
so every provider goes through the same gate.

The one real work is the schema dialect. Providers disagree on which JSON Schema
keywords they accept: Anthropic takes `const`, Gemini does not and needs a
single-value `enum`; Gemini rejects `default` and `discriminator` outright.
`flowforge/llm.py` has an adapter per dialect, and the tests assert that each one
emits only keywords that provider documents — and that neither of them eats your
field names on the way through.

## Scope

Record-triggered and autolaunched flows: Assignment, Decision, Get/Create/Update/
Delete Records, Loop, Subflow. Screen flows are not supported.

## Layout

| Module | Role |
|---|---|
| `flowforge/ir.py` | The IR — the single source of truth |
| `flowforge/xmlgen.py` | IR → Flow XML, deterministic, with auto-layout |
| `flowforge/mermaid.py` | IR → Mermaid + Markdown |
| `flowforge/llm.py` | Text → IR, bring-your-own-key, self-repairing |
| `flowforge/sfdc.py` | Metadata API: package, validate, deploy |
| `flowforge/orgs.py` | Reads org credentials from the `sf` CLI |
| `forge.py` | The pipeline |
| `diagnose.py` | Isolates where org authentication breaks |

```bash
.venv/Scripts/python.exe -m pytest tests -q
```

The tests run without an API key, a network, or an org: the LLM is scripted and
the org is stubbed. They cover the IR's invariants, the XML the compiler emits,
the self-repair loop, and the approval gates.
