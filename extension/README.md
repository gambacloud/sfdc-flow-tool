# SFDC Build Tool: Chrome extension (skeleton)

MV3 side-panel extension. Design: [docs/store/ARCHITECTURE.md](../docs/store/ARCHITECTURE.md). Store pack: `docs/store/`.

## Load it

1. `chrome://extensions` -> Developer mode -> **Load unpacked** -> select this `extension/` folder.
2. Open a Salesforce tab (Lightning, Setup, or My Domain) while logged in.
3. Click the toolbar icon. The side panel opens and the top bar shows the detected org (`Org: <host>.my.salesforce.com`, org id, which cookie was used).

Other tabs show a hint instead. Salesforce calls (lists, retrieve, validate, deploy, org summary) run in the panel with the browser `sid`; everything else goes to `BACKEND_URL` (placeholder `https://backend.example.com`, so those calls fail until it is set).

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
- Permission-set merge on plan approve (`/api/plan/approve` reads the existing XML from the org): the extension would need to fetch it and send it to the backend.
- Org Summary still needs `metadata-kb-worker.js` (missing from the repo, see ARCHITECTURE section 5).
- `import/object-fields` stays on the backend (the pick cache lives with the parsed object there).

Not covered here: final icons.
