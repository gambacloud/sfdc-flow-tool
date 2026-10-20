# SFDC Build Tool: Chrome extension (skeleton)

MV3 side-panel extension. Design: [docs/store/ARCHITECTURE.md](../docs/store/ARCHITECTURE.md). Store pack: `docs/store/`.

## Load it

1. `chrome://extensions` -> Developer mode -> **Load unpacked** -> select this `extension/` folder.
2. Open a Salesforce tab (Lightning, Setup, or My Domain) while logged in.
3. Click the toolbar icon. The side panel opens and the top bar shows the detected org (`Org: <host>.my.salesforce.com`, org id, which cookie was used).

Other tabs show a hint instead. The web UI below the bar will call `BACKEND_URL` (placeholder `https://backend.example.com`, so backend calls fail until it is set); anything that touches Salesforce shows "not implemented in the extension yet".

## Layout

| Path | What |
|---|---|
| `manifest.json` | Permissions: `cookies`, `storage`, `sidePanel`; hosts: Salesforce domains + backend |
| `background/` | Service worker, `sid.js` (org id prefix matching, as in Salesforce Inspector Reloaded) |
| `lib/` | `config.js` (BACKEND_URL), `hosts.js` (pure host helpers) |
| `panel/` | Side panel shell: `bootstrap.js`, `api.js` (replaces the web UI's `api()`), `org-client.js` (stubs + route table) |
| `panel/ui/`, `panel/index.html` | **Generated** copy of `flowtool/static` (patched). Do not edit; run `python extension/tools/sync_ui.py` |
| `tools/` | `check.py` (static checks), `sync_ui.py`, `make_icons.py`. Not shipped |

## Dev commands

```bash
python extension/tools/sync_ui.py   # regenerate the UI copy after changing flowtool/static
python extension/tools/check.py     # manifest / CSP / no-eval / no-remote-script checks
```

Not covered here: porting `flowtool/sfdc.py` etc. (org-client.js stubs), the missing `metadata-kb-worker.js`, final icons.
