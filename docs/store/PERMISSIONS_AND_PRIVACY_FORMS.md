# Dashboard "Privacy practices" tab: single purpose, permissions, data usage

Paste-ready answers. They match `extension/manifest.json` (permissions `cookies`, `storage`, `sidePanel`; host permissions for Salesforce domains and one backend origin; no content scripts; no remote code).

## Single purpose

> Help Salesforce administrators and developers create and change Salesforce metadata (Flows, Apex, Lightning components, objects and fields, permission sets) from a plain-language description, review the result, and deploy it to the Salesforce org they are logged into in the browser.

One purpose, with its parts (generate, review, validate, deploy) all serving the same workflow. Everything is on one side panel.

## Permission justifications

**cookies**
> Reads the Salesforce `sid` session cookie of the org shown in the active tab so the extension can call that org's Metadata and REST APIs as the logged-in user, instead of requiring a Connected App. The cookie is read only for Salesforce hosts, is kept in memory in the extension, and is never sent to the developer's servers or any third party.

**storage**
> Stores the user's settings and their own AI provider API key(s) in `chrome.storage.local` on the user's device, so they do not have to be re-entered. Nothing is synced or sent anywhere except the API key being used for the request the user starts.

**sidePanel**
> The tool's whole interface is a side panel that stays open next to the Salesforce tab the user is working in.

**Host permissions: `https://*.salesforce.com/*`, `https://*.force.com/*`, `https://*.salesforce-setup.com/*`**
> Salesforce orgs are served from customer-specific subdomains of these domains (My Domain, Lightning, Setup, Visualforce). The extension needs access to them to (1) read which org the active tab is on, (2) read that org's `sid` cookie, and (3) call that org's Metadata SOAP, REST and Tooling APIs to list, retrieve, validate and deploy metadata the user asked for. No content script is injected into these pages.

**Host permission: `[backend origin, currently placeholder https://backend.example.com]`**
> The developer's server that turns the user's request into Salesforce metadata and checks it, using the user's own AI provider key. It receives requests and metadata, never the Salesforce session id or org URL.

**Remote code**: No. All JavaScript is packaged in the extension. The AI/LLM work runs on the server and returns data (JSON/XML), not code that the extension executes.

## Data usage: what is collected

Tick only these (re-check against the shipped build before submission):

| Category | Collected? | Why |
|---|---|---|
| Personally identifiable information | **No** | Not requested. Free-text requests the user types may contain anything; see policy. |
| Health information | No | |
| Financial and payment information | No | |
| Authentication information | **Yes** | The user's own AI provider API key is sent to the backend with each request so the provider can answer. (The Salesforce session id is not sent.) |
| Personal communications | No | |
| Location | No | |
| Web history | No | |
| User activity | No (see open question 2 below) | |
| Website content | **Yes** | Salesforce metadata the user chooses to open or build (flow definitions, Apex code, etc.) and the text of their request are sent to the backend and the AI provider to produce the result. |

## Data usage: certifications (all three must be true)

- I do not sell or transfer user data to third parties, outside of the approved use cases. **True**: data goes only to the backend (our own) and the AI provider the user chose, to perform the requested function.
- I do not use or transfer user data for purposes that are unrelated to my item's single purpose. **True**.
- I do not use or transfer user data to determine creditworthiness or for lending purposes. **True**.

Limited Use statement (also in the policy): use of information received from Google APIs does not apply (the extension uses no Google user data), but the same limits are adopted.

## Open questions for the owner before ticking boxes

1. Does the hosted backend (Heroku) keep prompts or metadata beyond the life of a session? `Session`/`PlanSession` are in memory (`server.py:295`, `:1945`), and Heroku router logs record request metadata. If any of this is retained, say so in the policy.
2. Does the backend log request content or per-user activity (`flowtool/activity.py` keeps a per-browser, in-memory feed of log lines)? If you add analytics or error reporting later, "User activity"/"Website content" disclosures must be updated first.
