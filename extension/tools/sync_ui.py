"""Regenerate extension/panel/ui/ and extension/panel/index.html from flowtool/static.

The web UI stays the source of truth; the extension carries a patched copy.
Run from anywhere:  python extension/tools/sync_ui.py
Every patch is asserted, so a refactor of app.js that moves a patched spot
fails loudly instead of silently shipping an unpatched copy.
"""

import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "flowtool" / "static"
PANEL = ROOT / "extension" / "panel"
UI = PANEL / "ui"


def sub_once(text: str, pattern: str, repl: str, flags: int = 0) -> str:
    new, n = re.subn(pattern, lambda _m: repl, text, count=1, flags=flags)
    if n != 1:
        sys.exit(f"sync_ui: pattern not found: {pattern[:60]!r}")
    return new


def patch_app_js(js: str) -> str:
    # 1. api(): hand off to the extension bridge (panel/api.js).
    js = sub_once(
        js,
        r"async function api\(path, body\) \{.*?\n\}\n",
        "// Patched by extension/tools/sync_ui.py: the bridge routes Salesforce\n"
        "// calls to the extension and everything else to the hosted backend.\n"
        "async function api(path, body) {\n  return window.ExtApi.request(path, body);\n}\n",
        re.S,
    )
    # 2. The three direct fetches of session XML/artifacts.
    js, n = re.subn(r"\bfetch\(`api/", "window.ExtApi.rawFetch(`api/", js)
    if n != 3:
        sys.exit(f"sync_ui: expected 3 relative fetch(`api/...) calls, found {n}")
    # 3. No OAuth in the extension: the session comes from the browser.
    js = sub_once(
        js,
        r"function startOAuthLogin\(clientId, host\) \{",
        "function startOAuthLogin(clientId, host) {\n  return; // extension: no Connected App / OAuth\n",
    )
    # 4. Org credentials are resolved inside the extension, never by the UI.
    js = sub_once(
        js,
        r"function orgCredentials\(\) \{",
        "function orgCredentials() {\n  return {}; // extension: org + session id are resolved by the org client\n",
    )
    # 5. The org is the active Salesforce tab, so the "connect to an org first"
    #    guards (which wait for OAuth or an sf CLI) must not block the pickers,
    #    and the sf-CLI alias picker stays hidden.
    js = sub_once(
        js,
        r"state\.sfCli = config\.sf_cli;\n    if \(config\.sf_cli\) \{",
        "state.sfCli = true; // extension: the org is the active Salesforce tab\n    if (false) {",
    )
    return js


def patch_index(html: str) -> str:
    html = html.replace('href="static/app.css"', 'href="ui/app.css"')
    html = html.replace('href="static/vendor/slds-subset.css"', 'href="ui/vendor/slds-subset.css"')
    html = html.replace('href="static/guide.pdf"', 'href="ui/guide.pdf"')
    html = sub_once(html, r'<link rel="stylesheet" href="ui/vendor/slds-subset.css">',
                    '<link rel="stylesheet" href="ui/vendor/slds-subset.css">\n'
                    '<link rel="stylesheet" href="panel.css">')
    html = sub_once(
        html, r"<body>",
        '<body>\n'
        '<div id="extOrgBar" data-status="no-tab">\n'
        '  <strong id="extOrgText">Looking for a Salesforce tab...</strong>\n'
        '  <span id="extOrgDetail" class="dim"></span>\n'
        '  <span class="dim">Backend: <span id="extBackend"></span></span>\n'
        '</div>',
    )
    # The local MCP installer download is irrelevant (and unwanted) in the extension.
    html = sub_once(
        html, r'\s*<label class="check" style="display:block">\s*Install MCP for Claude Desktop.*?</label>', "", re.S,
    )
    # Replace the script block: the bootstrap module loads the rest in order.
    html = sub_once(
        html, r'<script src="static/vendor/mermaid\.min\.js"></script>.*?</body>',
        '<script type="module" src="bootstrap.js"></script>\n</body>', re.S,
    )
    return html


def main() -> None:
    if UI.exists():
        shutil.rmtree(UI)
    (UI / "vendor").mkdir(parents=True)
    for name in ("app.css", "lwc-preview.js", "guide.pdf"):
        shutil.copy2(SRC / name, UI / name)
    for name in ("mermaid.min.js", "slds-subset.css"):
        shutil.copy2(SRC / "vendor" / name, UI / "vendor" / name)
    (UI / "app.js").write_text(patch_app_js((SRC / "app.js").read_text(encoding="utf-8")), encoding="utf-8")
    (PANEL / "index.html").write_text(patch_index((SRC / "index.html").read_text(encoding="utf-8")), encoding="utf-8")
    print("synced", UI)


if __name__ == "__main__":
    main()
