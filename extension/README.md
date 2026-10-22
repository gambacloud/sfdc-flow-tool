# SFDC Build Tool: Chrome extension (skeleton)

MV3 side-panel extension. Design: [docs/store/ARCHITECTURE.md](../docs/store/ARCHITECTURE.md). Store pack: `docs/store/`.

## Load it

1. `chrome://extensions` -> Developer mode -> **Load unpacked** -> select this `extension/` folder.
2. Open a Salesforce tab (Lightning, Setup, or My Domain) while logged in.
3. Click the toolbar icon. The side panel opens and the top bar shows the detected org (`Org: <host>.my.salesforce.com`, org id, which cookie was used).

Other tabs show a hint instead. Salesforce calls (lists, retrieve, validate, deploy, org summary) run in the panel with the browser `sid`; everything else goes to `BACKEND_URL` (`https://salesforce-debugtool.herokuapp.com/flow-tool`, see below).

## Layout

| Path | What |
|---|---|
| `manifest.json` | Permissions: `cookies`, `storage`, `sidePanel`; hosts: Salesforce domains + backend |
| `background/` | Service worker, `sid.js` (org id prefix matching, as in Salesforce Inspector Reloaded) |
| `lib/` | `config.js` (BACKEND_URL), `hosts.js` (pure host helpers), `vendor/fflate.js` (MIT, zip) |
| `panel/` | Side panel shell: `bootstrap.js`, `api.js` (replaces the web UI's `api()`), `org-client.js` (route table + local job polling), `org/` (`sfdc.js` port of `flowtool/sfdc.py`, `xml.js`, `zip.js`) |
| `panel/ui/`, `panel/index.html` | **Generated** copy of `flowtool/static` (patched). Do not edit; run `python extension/tools/sync_ui.py` |
| `tools/` | `check.py` (static checks), `sync_ui.py`, `make_icons.py`. Not shipped |

## Dev commands

```bash
python extension/tools/sync_ui.py   # regenerate the UI copy after changing flowtool/static
python extension/tools/check.py     # manifest / CSP / no-eval / no-remote-script checks
node --test extension/tests/org.test.mjs   # envelopes, faults, deploy status, zips, api router (mocked fetch)
node extension/tools/manual_cdp_test.mjs   # real Chrome + forge-scratch: lists + checkOnly validate (HEADED=1 to watch)
```

## How org calls are served

`panel/api.js` asks `org-client.js` first. Matching paths are answered locally with the same response shapes as `server.py`, including the `start` + `/status` pairs (kept as in-memory jobs), so `panel/ui/app.js` is unchanged apart from the patches in `tools/sync_ui.py`. The sid is read through the service worker and re-read once on `INVALID_SESSION_ID`. Redirects are never followed, cookies are never sent (`credentials: "omit"`).

Backend contract the extension relies on (sent to the `backend-split` session; adjust `org-client.js` if it differs):

| Call | Purpose |
|---|---|
| `GET /api/session/{id}/bundle`, `GET /api/plan/session/{id}/bundle` | `{version, files, types, api_version}`; plan may add `components:[{type,name,object?,step}]` (setup links) and `prompt_templates` |
| `POST /api/session/{id}/deploy-result`, `POST /api/plan/session/{id}/deploy-result` | `{success,status,failures,check_only,version}`; best effort, plan variant may return `{plan}` |
| `POST /api/import/parse` | `{kind,api_name,files,provider,model,effort,api_key,api_version}` -> what `/api/import/status` returned when done; Apex/trigger send `files={body}` |
| `POST /api/survey/score` | `{flows:{name:xml}}` -> `{report}` |

## Follow-ups (not in this change)

- Agent publish (`/api/plan/agent/publish/*`, needs the SFAP JWT exchange) and the portal builder (`/api/portal/*`): answer 501 "not available in the extension yet".
- Org Summary still needs `metadata-kb-worker.js` (missing from the repo, see ARCHITECTURE section 5).

Not covered here: final icons.

Backend confirmed (branch `claude/nostalgic-chandrasekhar-50a051`): bundles carry `needs_existing_permission_set` (extension fetches the set and re-POSTs `{existing_permission_set_xml}` to the bundle path); large objects return `choose_fields` with `pick_id: null` and the extension re-POSTs `import/parse` with `fields`; plan `deploy-result` also gets `component_failures`.

## Backend and extension id

`BACKEND_URL` is `https://salesforce-debugtool.herokuapp.com/flow-tool`: this repo is a pinned pip dependency of the `salesforce-debugtool` Heroku app and mounted at `/flow-tool`. `manifest.json` `host_permissions` and the CSP `connect-src` carry the origin; `tools/check.py` keeps the three in step.

`manifest.json` pins a `key`, so the extension id is the same for a local unpacked load, the Unlisted store item and the CORS allow-list: `eimhkifgfamgkmmoidhlhbdjbhigmifp`. The matching private key lives outside the repo (`%USERPROFILE%\.claude\ext-keys\sfdc-build-tool.pem`); keep it, it is needed to upload a package signed with the same id if the store ever asks for it. The Chrome Web Store assigns its own id to the store item unless the key is supplied in the first upload: confirm the id on the dashboard and, if it differs, use that id in the CORS setting too.

The only Heroku setting the shared app needs:

```
heroku config:set FLOWTOOL_CORS_ORIGINS=chrome-extension://eimhkifgfamgkmmoidhlhbdjbhigmifp -a salesforce-debugtool
```

`FLOWTOOL_HOSTED` must stay **unset** on that app: it also serves the existing web UI, whose users send OAuth org tokens, and hosted mode would reject them. The extension never sends org fields, so it works without it.

LLM-spending endpoints are rate limited per client (`FLOWTOOL_RATE_PER_MIN`, default 60; `FLOWTOOL_RATE_PER_DAY`, default 2000; 0 disables). A 429 carries a plain message, which the panel shows like any other error. The privacy policy page is served at `/flow-tool/privacy`.

Requests from the extension origin (`FLOWTOOL_CORS_ORIGINS`) to the LLM endpoints must carry their own `api_key`; otherwise the backend answers 400 "Add your own AI key in the extension settings." and never falls back to the server's env keys. The web UI is unaffected.
