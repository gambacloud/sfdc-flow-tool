# Chrome extension: hybrid architecture and backend split

Status: design note. Line numbers refer to the tree at branch `claude/confident-germain-cfcaf1` (base `efbae65`). Nothing in the Python backend has been changed yet.

## 1. Goal and model

Users whose companies cannot approve a Connected App use the tool through a Chrome extension (MV3, Unlisted on the Chrome Web Store).

- **In the extension:** everything that touches a Salesforce org (list, retrieve, validate/deploy, agent publish, portal/site apply, setup-URL lookups). It authenticates with the browser's existing Salesforce session id (the `sid` cookie). The session id never leaves the browser.
- **On the hosted backend:** only stateless LLM / IR / XML work. It receives plain-language requests, IR, or XML, and returns IR / XML / diagnostics. It never receives an org URL or a session id.
- **LLM keys:** the user's own, kept in `chrome.storage.local`, sent per request to the backend (as the web UI does today with `api_key`) or later called directly from the extension.

Open point owned by the `ext-spike` session: whether the `sid` cookie works as Metadata SOAP `sessionId`, as `Authorization: Bearer` for REST/Tooling, and for the Agentforce SFAP JWT exchange (`flowtool/agent_publish.py:163`). This document assumes yes for the first two; the third is the riskiest.

## 2. The real obstacle: server-held sessions

The backend is not stateless today. Everything the UI does hangs off in-memory objects:

| State | Where |
|---|---|
| `Session` (flow/apex/trigger/lwc under review, `version`, `approved_version`, history, `last_failures`) | `server.py:213`, store `SESSIONS` `server.py:295`, lookup `get_session` `server.py:407` |
| `PlanSession` (multi-step plan) | `PLAN_SESSIONS` `server.py:1945`, `get_plan_session` `server.py:1948` |
| Background jobs (design/validate/deploy/import/survey, polled via `/status`) | `PendingDeploy` `server.py:185`, `PendingLLM` `server.py:200` |

Consequences for the split:

1. **Approval gating** (`session.approved`, `server.py:1566`, `:1615`) is enforced server-side today. In the hybrid model the deploy happens in the extension, so approval becomes a client-side rule plus a signed hand-off (see 4.3). Treat it as a UX guard, not a security boundary: the user already holds the org session.
2. **Deploy files are derived from the session** (`_deploy_files`, `server.py:1501`; plan equivalent around `server.py:2400-2447`). The backend keeps that logic and returns the bundle (`files` map + package.xml `types`) to the extension.
3. **Repair loop** (`/api/repair/start`, `server.py:1593`) reads `session.last_failures`. The extension must send the failure strings back.
4. **Job polling** exists only because of Heroku's 30 s router limit. Salesforce calls running in the extension have no such limit, so org-side `/status` polling disappears. LLM jobs keep the start/poll pattern.

Recommended minimal change: keep `Session`/`PlanSession` on the server (they are LLM context, not org credentials) but remove every `instance_url` / `access_token` / `org` field. Additionally add `GET /api/*/bundle` endpoints that return the deploy bundle. A later phase can make sessions fully client-held (IR snapshot round-trip) so the server holds nothing; `flowtool/share.py` already has a snapshot/restore shape to reuse.

## 3. Endpoints that move to the extension

All of these call `credentials()` (`server.py:587`) to get `(instance_url, token)` and then call `flowtool/sfdc.py`, `agent_publish.py` or `portal_*.py`. `credentials()` itself is deleted from the hosted path; the sf-CLI fallback (`flowtool/orgs.py`) is meaningless on a hosted server and stays only for the local/self-hosted build.

### 3.1 Pure org I/O (move wholesale)

| Endpoint | server.py | Python it calls | Extension replacement |
|---|---|---|---|
| `POST /api/flows` | 924 | `sfdc.list_flows` (sfdc.py:390) | `org.listFlows()` |
| `POST /api/apex-classes` | 945 | `list_apex_classes` (sfdc.py:707) | `org.listApexClasses()` |
| `POST /api/apex-triggers` | 993 | `list_apex_triggers` (sfdc.py:923) | `org.listApexTriggers()` |
| `POST /api/lwc-components` | 1008 | `list_lwc_components` (sfdc.py:765) | `org.listLwc()` |
| `POST /api/permission-sets` | 1023 | `list_permission_sets` (sfdc.py:862) | `org.listPermissionSets()` |
| `POST /api/prompt-templates` | 960 | `prompt_import.list_prompt_templates` | `org.listPromptTemplates()` |
| `POST /api/custom-objects` | 971 | `object_import.list_custom_objects` | `org.listCustomObjects()` |
| `POST /api/custom-fields` | 982 | `object_import.list_custom_fields` | `org.listCustomFields()` |
| `POST /api/survey/start`, `GET /status` | 1046, 1061 | `retrieve_all_flows` (sfdc.py:548) + `survey.py` scoring | extension retrieves, backend scores (4.2) |
| `POST /api/org-summary/start`, `GET /status` | 1081, 1097 | `retrieve_org_summary_zip` (sfdc.py:657) | extension retrieves the zip; parsing already happens in the browser (`app.js:2527`) |
| `POST /api/import/start`, `GET /status` | 1149, 1276 | `retrieve_flow/apex/trigger/lwc/permission_set` (sfdc.py:686, 741, 815, 952, 900), `_retrieve_prompt_template/_custom_object/_custom_field` (server.py:1184, 1257, 1262) | extension retrieves, then POSTs XML to a new backend `import/parse` that returns IR (4.1) |
| `POST /api/import/object-fields` | 1243 | server-side pick cache | picker state moves to the extension |
| `POST /api/validate/start`, `/status` | 1563, 1572 | `validate_bundle` (sfdc.py:986) via `_start_deploy` (server.py:1535) | `org.validate(bundle)` (SOAP `deploy` with `checkOnly`) |
| `POST /api/deploy/start`, `/status` | 1612, 1623 | same, then `component_setup_url` (sfdc.py:465) | `org.deploy(bundle)` + `org.setupUrl()` |
| `POST /api/plan/validate/start`, `/status` | 2450, 2459 | `validate_bundle`, plan variant (server.py:2409) | same `org.validate` |
| `POST /api/plan/deploy/start`, `/status` | 2481, 2492 | `validate_bundle` + `component_setup_url` (server.py:2507) | same `org.deploy` |
| `POST /api/plan/agent/publish/start`, `/status` | 2527, 2554 | `agent_publish.publish_employee_agent` (agent_publish.py:251) | `org.publishAgent()` (needs SFAP JWT; see section 1) |
| `POST /api/portal/{preflight,plan,apply}`, `GET /api/portal/status` | `portal_api.py` via `make_router(credentials)` mounted at `server.py:3191` | `portal_preflight`, `portal_site`, `portal_pages`, `portal_miaw`, `portal_agent` | `org.portal.*`; the planning half (`/plan`) can stay on the backend if it only takes IR (verify when porting) |
| `POST /api/plan/approve` (permission-set branch) | 2333, org read at 2371 | `retrieve_permission_set` (sfdc.py:900) | extension fetches the existing XML and sends it up; backend merges (`merge_permission_set_xml`) |

### 3.2 Mixed endpoints (split in two)

| Endpoint | server.py | Split |
|---|---|---|
| `POST /api/design/start` | 828 | stays; remove nothing (no org). |
| `POST /api/plan/start` / `execute/start` | 2204, 2269 | stay; org-derived context (existing object fields etc.) must be supplied by the extension in the request body, not fetched with a token. |
| `POST /api/approve` | 1488 | stays; also returns the bundle. |
| `POST /api/plan/git/push` | 2882 | GitHub, not Salesforce. Keep on backend only if the user's GitHub token is acceptable to send; otherwise move to the extension (`github.py`, host_permissions for api.github.com would be needed). Out of scope for v1: disable in the extension. |

### 3.3 Stays on the backend (IR/XML only, no org credentials)

`/api/config` (753, drop `clientId`, `orgs`, `sf_cli`), `/api/models` (803), `/api/design`, `/api/lwc` (880), `/api/refine` (1471), `/api/explain` (1422), `/api/kb-chat` (1116), `/api/repair` (1593), `/api/validate`-less XML generation (`xmlgen_*`), `/api/session*` (1650, 1664, 2788, 2829), `/api/reference` (2921), `/api/plan/revise*`, `/api/plan/repair*`, `/api/plan/step/revise*`, `/api/plan/session/.../report` (2937), `/api/share/*` (3116, 3132), `/api/activity` (161).

New backend endpoints needed (names indicative):

- `POST /api/import/parse` : `{kind, xml-or-files}` -> `{session_id, view}`; wraps the current post-retrieve half of `import_start` (server.py:1149).
- `GET  /api/session/{id}/bundle` and `/api/plan/session/{id}/bundle` : `{files, types, api_version}` using `_deploy_files` (server.py:1501) / the plan equivalent. (`/api/plan/session/{id}/package`, server.py:2829, already builds a zip: reuse.)
- `POST /api/survey/score`: `{flows: {name: xml}}` -> report (the retrieval half moves out).
- `POST /api/portal/plan` variant taking preflight results as input.

## 4. Client side (UI) changes

All in `flowtool/static/app.js` (copied into `extension/sidepanel/` and adapted; the original is untouched).

| Item | Location | Change |
|---|---|---|
| `api()` helper | `app.js:353` | Prefix with `BACKEND_URL`; add BYO LLM key header/body field from `chrome.storage.local`. Org-touching paths are routed to `org-client.js` instead (a path-prefix table). The skeleton already does this (`extension/sidepanel/api.js`). |
| `CLIENT_ID` | `app.js:340` | `sessionStorage` is available in the side panel; keep. |
| `startOAuthLogin` | `app.js:3005` | Delete in the extension. No OAuth, no Connected App. `/api/config` `clientId` login buttons are hidden. |
| OAuth fragment handling (`access_token`/`instance_url` from URL) | `app.js:3217-3218` | Delete. |
| `orgCredentials()` | `app.js:3764` | Return `{}`; the extension's org client resolves host + sid itself. All 13 call sites (`app.js:1817, 1880, 1937, 2058, 2119, 2182, 2380, 2508, 2742, 2917, ...`) stop sending credentials. |
| `state.org`, `$("org")` alias picker (`app.js:3521, 3659, 3767, 3855`) | | Replace with the detected org host from the service worker. |
| API-key storage | `app.js:2944, 3843` (`localStorage`) | Move to `chrome.storage.local`. |
| Org-summary polling + base64 zip | `app.js:2508-2530` | Becomes a direct `org.retrieveOrgSummaryZip()` call; no polling. |
| Share links, `?session=` / `?share=` URL params | `app.js:3040`, `:3188` | Side panels have no address bar. Share-unlock needs an in-panel input; defer. |

### 4.1 Session id and org lookup (service worker)

`extension/background/sid.js` ports the approach of Salesforce Inspector Reloaded:
the tab's host is normalised to a cookie-bearing "My Domain" host; `chrome.cookies.getAll({name:"sid", domain})` is queried; the right cookie is the one whose value starts with the org id (15-char `00D...` prefix of the sid, `ORGID!`), because Lightning, Visualforce, `*.my.salesforce-setup.com` and community hosts each carry their own `sid`. Cookies set for other orgs on the same registrable domain are ignored. See the file header for details. The session id is returned only to extension pages (side panel/service worker), never to content scripts or the backend.

### 4.2 Data egress review

What leaves the browser to the backend after the split: user prompts; IR; org metadata XML the user chose to open (flows, Apex, etc.) for LLM refinement; validation error text; org summary markdown when the user asks a KB question; BYO LLM key (per request). What does not: session id, instance URL (strip from XML/reports where present: `report.py` and `survey.py` should be audited), cookies.

### 4.3 Approval

Keep `approved_version` on the server session. `GET .../bundle` requires an approved version and returns `{version, files, types}`; the extension deploys exactly that bundle. Because the server no longer sees the deploy, it cannot learn the result; the extension posts the result to `POST /api/session/{id}/deploy-result` (success flag, failure strings) so the repair loop (`last_failures`, `server.py:236`) keeps working.

## 5. MV3 / CSP findings

The backend's CSP today is `script-src 'self'` and `connect-src 'self'` (`server.py:118-142`), so the UI already runs with no inline scripts. MV3 extension pages default to `script-src 'self'; object-src 'self'` and forbid `unsafe-eval` and remote code.

- **`/static/metadata-kb-worker.js` (`app.js:2527`)**: `new Worker("/static/metadata-kb-worker.js")` uses an absolute path. Two problems: (a) in an extension `/static/...` resolves to the extension root, not the backend, and no such directory exists; (b) **the file is not in the tree** (`flowtool/static/` contains only app.css/app.js/contact-widget.js/guide.pdf/index.html/lwc-preview.js/rating-widget.js/vendor/install; no git history for it either; `flowtool/sfdc.py:665` is the only other reference). So Org Summary parsing is broken in this checkout today, independent of the extension. The extension skeleton does not ship it; the Org Summary feature is flagged unavailable until the worker is added (or rewritten) and referenced via `chrome.runtime.getURL`. Extension workers must also be same-origin files from the package. Ask the owner whether the file lives in another repo or branch.
- **`vendor/mermaid.min.js`**: contains the literal `(0,eval)(e)`, but it sits in a bluebird-style `toFastProperties` helper (`function Aq(e){...return e;(0,eval)(e)}`), after a `return`, so it is unreachable dead code. It also contains the lodash root probe `Function("return this")()`, used only as the last fallback after `globalThis`/`self`/`window`, which exist in extension pages. Neither runs under MV3 CSP, but a static scan (Chrome Web Store review) can still flag the string `eval`; mention it in the review notes. No CDN fetch is involved. To remove the string entirely, patch the bundle or rebuild; not done here.
- **`lwc-preview.js`**: no `eval`, `new Function`, or string `setTimeout`. It rebuilds DOM with `createElement`/`textContent` and only touches `innerHTML` to clear a container (`:308`). MV3-safe as written.
- **`app.js`** has no `eval`/`new Function`. `index.html` loads scripts with `src` only; no inline script/handlers were found in the `<script>` tags.
- **Widgets**: `contact-widget.js:20` loads `html2canvas` from `cdnjs.cloudflare.com` at runtime, which is **remote code and not allowed in MV3**; it also posts to `/api/contact` (`:136`), and `rating-widget.js:120` posts to `/api/rating`. The skeleton does not copy either widget. If wanted later, vendor html2canvas into the package and point the fetches at `BACKEND_URL`.
- `index.html` has no inline `on*=` handlers and loads scripts only via `src`, so the page is already MV3-compatible in that respect.

## 6. Phasing

1. (this PR) Skeleton: manifest, service worker, sid lookup, side panel with org detection, `api()` + org-client stubs, store pack.
2. Port `sfdc.py` list/retrieve/deploy to `extension/lib/` (SOAP + REST in `fetch`, zip building with a vendored zip lib, no `eval`). Gate with ext-spike results.
3. Backend: add `import/parse`, `bundle`, `deploy-result`, `survey/score`; remove credentials from request models (`OrgRequest` server.py:647, `FlowsRequest` :656, `DeployRequest` :684, `SurveyRequest` :688, `ImportRequest` :705).
4. Agent publish + portal builder in the extension.
5. Backend CORS: allow `chrome-extension://<id>` origin; the hosted CSP is unaffected (extension pages are not served by it).
