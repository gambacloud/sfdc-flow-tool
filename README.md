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

151 tests, none of which need a key, a network, or an org.

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

A flow using screens, waits, formula resources, or other constructs the IR can't
hold is **refused, not drawn approximately**, naming what it found. Skipping
those parts would show a diagram of a different flow than the one in the org, and
editing from that diagram would delete them on deploy.

`parse(generate(ir)) == ir` is asserted for every element type in
`tests/test_roundtrip.py`. That property is what makes an edit round-trip safe.

## What it covers

Record-triggered and autolaunched flows:

| | |
|---|---|
| Assignment | Set variable values |
| Decision | Branch on structured conditions |
| Get / Create / Update / Delete Records | |
| Loop | |
| Subflow | |
| **Action** | Email alerts, Send Email, Apex invocables, Chatter posts — anything with an `actionType` |

Formula **fields** on an object work anywhere a reference does (`$Record.Margin__c`).
Formula **resources** defined inside a flow do not. Screen flows are out of scope.

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
| `diagnose.py` | Isolates where org authentication breaks |

### Adding a provider

Implement `complete_json` and `complete_text`, then register it in
`PROVIDERS`. Validation is not your concern — it happens once, centrally.

The real work is the schema dialect. Providers disagree on which JSON Schema
keywords they accept: Anthropic takes `const`, Gemini needs a single-value `enum`
and rejects `default` and `discriminator`. `flowtool/llm.py` has an adapter per
dialect, and the tests assert each one emits only keywords that provider
documents — and that neither eats your field names on the way through.

## Troubleshooting

| Symptom | Cause |
|---|---|
| `No LLM key found` | `.env` missing, misnamed, or in the wrong directory — the message prints the full path it wants |
| `sf CLI is not on PATH` | Started from a shell where `sf` doesn't resolve |
| Org list empty | Same |
| `INVALID_SESSION_ID` | Run `python diagnose.py --org dev`; it isolates which layer is failing |
| A flow won't open | The refusal names the construct. That's a gap in the IR, not a broken flow |
