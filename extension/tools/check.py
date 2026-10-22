"""Static checks for the extension package (stdlib only).

  - manifest is MV3 with exactly the allowed permissions and host patterns
  - BACKEND_URL in lib/config.js matches host_permissions and CSP connect-src
  - every file the manifest and panel/index.html reference exists
  - no eval / new Function / remote script in the extension's own code
    (vendored ui/vendor/mermaid.min.js is reported, not failed: see
    docs/store/ARCHITECTURE.md section 5)
  - the Salesforce session cookie is never sent to the backend module
"""

import json
import re
import sys
from pathlib import Path

EXT = Path(__file__).resolve().parents[1]
errors = []


def err(msg):
    errors.append(msg)


manifest = json.loads((EXT / "manifest.json").read_text(encoding="utf-8"))
if manifest.get("manifest_version") != 3:
    err("manifest_version must be 3")
if sorted(manifest.get("permissions", [])) != ["cookies", "sidePanel", "storage"]:
    err(f"unexpected permissions: {manifest.get('permissions')}")
if "optional_permissions" in manifest or "content_scripts" in manifest:
    err("no optional_permissions or content_scripts expected in v0.1")

config = (EXT / "lib" / "config.js").read_text(encoding="utf-8")
m = re.search(r'BACKEND_URL\s*=\s*"(https://[^"/]+)(/[^"]*)?"', config)
if not m:
    err("BACKEND_URL not found / not https")
else:
    backend = m.group(1)
    hosts = manifest["host_permissions"]
    if f"{backend}/*" not in hosts:
        err(f"host_permissions missing {backend}/*")
    sf = {"https://*.salesforce.com/*", "https://*.force.com/*", "https://*.salesforce-setup.com/*"}
    if set(hosts) != sf | {f"{backend}/*"}:
        err(f"host_permissions has unexpected entries: {hosts}")
    csp = manifest["content_security_policy"]["extension_pages"]
    if backend not in csp:
        err("CSP connect-src missing backend origin")
    if "unsafe-eval" in csp or "unsafe-inline" in csp.split("script-src")[1].split(";")[0]:
        err("CSP must not allow unsafe-eval/unsafe-inline scripts")

refs = [manifest["background"]["service_worker"], manifest["side_panel"]["default_path"]]
refs += list(manifest["icons"].values()) + list(manifest["action"]["default_icon"].values())
for r in refs:
    if not (EXT / r).is_file():
        err(f"missing file referenced by manifest: {r}")

panel = EXT / "panel"
if (panel / "index.html").is_file():
    html = (panel / "index.html").read_text(encoding="utf-8")
    for src in re.findall(r'<(?:script|img|link)[^>]*?(?:src|href)="([^":]+)"', html):
        if not (panel / src).is_file():
            err(f"panel/index.html references missing file: {src}")
    if re.search(r"<script(?![^>]*\bsrc=)", html):
        err("panel/index.html has an inline <script>")
    if re.search(r'\son[a-z]+="', html):
        err("panel/index.html has inline event handlers")
    if re.search(r'<(?:script|img|link|iframe)[^>]*?(?:src|href)="https?:', html):
        err("panel/index.html loads a remote resource")

own_js = [p for p in EXT.rglob("*.js") if "vendor" not in p.parts and "ui" not in p.parts[-3:-1]]
own_js += [EXT / "panel" / "ui" / "app.js", EXT / "panel" / "ui" / "lwc-preview.js"]
for p in own_js:
    if not p.is_file():
        continue
    text = p.read_text(encoding="utf-8")
    if re.search(r"\beval\s*\(|new\s+Function\s*\(|importScripts\s*\(", text):
        err(f"eval/new Function/importScripts in {p.relative_to(EXT)}")
    if re.search(r"""['"`]https?://(?!salesforce-debugtool\.herokuapp\.com)[^'"`]*\.(js|mjs)['"`]""", text):
        err(f"remote script URL in {p.relative_to(EXT)}")

api_js = (panel / "api.js").read_text(encoding="utf-8")
if re.search(r"\bsid\b|getSession|cookies", api_js.replace("sessionStorage", "")):
    err("panel/api.js (backend path) must not touch the session id or cookies")

mer = EXT / "panel" / "ui" / "vendor" / "mermaid.min.js"
if mer.is_file():
    n = len(re.findall(r"\beval\b", mer.read_text(encoding="utf-8", errors="ignore")))
    print(f"note: vendored mermaid.min.js contains {n} occurrence(s) of the word eval (not executed in normal use)")

if errors:
    print("FAILED")
    for e in errors:
        print(" -", e)
    sys.exit(1)
print("extension checks passed")
