"""
Serves the extension's privacy policy as a plain HTML page.

The Markdown source ships in the package (flowtool/legal/PRIVACY_POLICY.md,
kept identical to docs/store/PRIVACY_POLICY.md by a test) and is rendered here
with a tiny converter for the few constructs the policy uses, so the page needs
no extra dependency and no external asset.
"""

from __future__ import annotations

import html
import re
from pathlib import Path

POLICY = Path(__file__).parent / "legal" / "PRIVACY_POLICY.md"

_INLINE = [
    (re.compile(r"`([^`]+)`"), r"<code>\1</code>"),
    (re.compile(r"\*\*(.+?)\*\*"), r"<strong>\1</strong>"),
    (re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)"), r'<a href="\2">\1</a>'),
]

PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Privacy Policy - SFDC Build Tool</title>
<style>
body{max-width:46rem;margin:2rem auto;padding:0 1rem;font:16px/1.6 system-ui,sans-serif;color:#1d2330;background:#fff}
h1{font-size:1.7rem}h2{font-size:1.25rem;margin-top:2rem}
code{background:#f1f3f6;padding:0 .25em;border-radius:3px}
@media (prefers-color-scheme:dark){body{color:#e6e9ef;background:#14171d}code{background:#232833}a{color:#8ab4f8}}
</style></head><body>
%s
</body></html>
"""


def _inline(text: str) -> str:
    out = html.escape(text, quote=False)
    for pattern, repl in _INLINE:
        out = pattern.sub(repl, out)
    return out


def render(markdown: str) -> str:
    out, para, items = [], [], []

    def flush() -> None:
        if para:
            out.append("<p>%s</p>" % _inline(" ".join(para)))
            para.clear()
        if items:
            out.append("<ul>%s</ul>" % "".join("<li>%s</li>" % _inline(i) for i in items))
            items.clear()

    for line in markdown.splitlines():
        stripped = line.strip()
        heading = re.match(r"(#{1,3}) (.+)", stripped)
        if heading:
            flush()
            n = len(heading.group(1))
            out.append("<h%d>%s</h%d>" % (n, _inline(heading.group(2)), n))
        elif stripped.startswith("- "):
            if para:
                flush()
            items.append(stripped[2:])
        elif not stripped:
            flush()
        else:
            if items:
                flush()
            para.append(stripped)
    flush()
    return PAGE % "\n".join(out)


def page() -> str:
    return render(POLICY.read_text(encoding="utf-8"))
