"""
Render a plan's steps (Object/Field/Apex/Flow) as HTML - shared by
mcp_server.py (an unstyled fragment a calling agent can embed elsewhere, an
Artifact included) and server.py's downloadable plan report (a standalone
document, for handing what's pending approval to a person or another system
outside this tool - print-to-PDF from a browser covers the PDF case without
this project taking on a PDF-rendering dependency it doesn't otherwise need).

One rendering function, not two, so the web UI's download and an MCP client's
view of the same build never drift apart.
"""

from __future__ import annotations

import base64
import html
from pathlib import Path
from typing import Any, List, Optional

from .ir import Flow
from .ir_apex import ApexClass, ApexTrigger
from .ir_lwc import LightningComponent
from .ir_object import CustomField, CustomObject
from .ir_platform_event import PlatformEvent
from .lwc_guide import to_lwc_test_guide
from .mermaid import element_index, to_mermaid, to_test_guide
from .planner import Plan, StepResult

_VENDOR_MERMAID_JS = Path(__file__).parent / "static" / "vendor" / "mermaid.min.js"
_STATIC_DIR = Path(__file__).parent / "static"
_LWC_PREVIEW_JS = _STATIC_DIR / "lwc-preview.js"
_SLDS_SUBSET_CSS = _STATIC_DIR / "vendor" / "slds-subset.css"


def _esc(value: Any) -> str:
    return html.escape(str(value))


def render_html_fragment(steps: List[StepResult]) -> str:
    """
    An unstyled HTML fragment describing every step - no <html>/<head>, so
    whatever renders this can drop it straight into a document of its own.
    """
    parts: List[str] = ["<section>"]
    for result in steps:
        value = result.value
        step = result.step
        parts.append(f'<article data-step-type="{_esc(step.artifact_type)}">')
        parts.append(f"<h3>{_esc(step.name)}</h3>")
        if step.artifact_type == "flow" and step.flow_type:
            parts.append(f'<p class="flow-type">Flow type: {_esc(step.flow_type)}</p>')
        reasoning = getattr(value, "reasoning", None)
        if reasoning:
            parts.append(f'<p class="reasoning">Why: {_esc(reasoning)}</p>')

        if isinstance(value, CustomObject):
            parts.append(
                "<table><tbody>"
                f"<tr><th>API name</th><td>{_esc(value.api_name)}</td></tr>"
                f"<tr><th>Label</th><td>{_esc(value.label)}</td></tr>"
                f"<tr><th>Plural label</th><td>{_esc(value.plural_label)}</td></tr>"
                "</tbody></table>"
            )
        elif isinstance(value, CustomField):
            parts.append(
                "<table><tbody>"
                f"<tr><th>API name</th><td>{_esc(value.api_name)}</td></tr>"
                f"<tr><th>Type</th><td>{_esc(value.type)}</td></tr>"
                f"<tr><th>On object</th><td>{_esc(value.object_api_name)}</td></tr>"
                "</tbody></table>"
            )
        elif isinstance(value, (ApexClass, ApexTrigger)):
            parts.append(f"<pre><code>{_esc(value.body)}</code></pre>")
        elif isinstance(value, LightningComponent):
            parts.append(f"<h4>js</h4><pre><code>{_esc(value.js)}</code></pre>")
            parts.append(f"<h4>html</h4><pre><code>{_esc(value.html)}</code></pre>")
            if value.css:
                parts.append(f"<h4>css</h4><pre><code>{_esc(value.css)}</code></pre>")
            # Same rough non-executing preview the in-app "Preview" tab shows
            # (window.renderLwcPreview, from static/lwc-preview.js) - the
            # source html travels as base64 rather than inline markup so it
            # survives the round trip through a raw-text <script> or a data
            # attribute without any "</script"-in-source or quote-escaping
            # edge cases. render_standalone_report below is what actually
            # decodes and renders these; a caller embedding just this
            # fragment (mcp_server.py, into an Artifact) gets an inert
            # placeholder until it loads that script itself.
            encoded_html = base64.b64encode(value.html.encode("utf-8")).decode("ascii")
            parts.append(
                f'<h4>preview</h4><div class="lwc-preview" data-lwc-html="{encoded_html}">'
                "Preview renders once this report is opened in a browser."
                "</div>"
            )
            parts.append(
                f"<h4>How to test this component</h4>"
                f"<pre>{_esc(to_lwc_test_guide(value))}</pre>"
            )
        elif isinstance(value, PlatformEvent):
            parts.append(
                "<table><tbody>"
                f"<tr><th>API name</th><td>{_esc(value.api_name)}</td></tr>"
                f"<tr><th>Label</th><td>{_esc(value.label)}</td></tr>"
                f"<tr><th>Publish behavior</th><td>{_esc(value.publish_behavior)}</td></tr>"
                f"<tr><th>Fields</th><td>"
                f"{_esc(', '.join(f'{f.api_name} ({f.type})' for f in value.fields))}"
                "</td></tr>"
                "</tbody></table>"
            )
        elif isinstance(value, Flow):
            # A live diagram, not a screenshot - <pre class="mermaid"> is the
            # same convention Claude's own Artifact viewer renders natively,
            # and render_standalone_report below inlines the vendored
            # mermaid.js so it also renders when this fragment is wrapped
            # into a plain downloaded file, opened outside either of those.
            parts.append(f'<pre class="mermaid">{_esc(to_mermaid(value))}</pre>')
            # Per-element detail, the same table the in-app Documentation tab
            # already shows via flowtool.mermaid.element_index - what each
            # element actually does, not just its one-line diagram caption.
            parts.append(
                "<table><thead><tr><th>Element</th><th>Type</th>"
                "<th>What it does</th></tr></thead><tbody>"
            )
            for row in element_index(value):
                parts.append(
                    f"<tr><td>{_esc(row['label'])}</td><td>{_esc(row['type'])}</td>"
                    f"<td>{_esc(row['detail'])}</td></tr>"
                )
            parts.append("</tbody></table>")
            parts.append(
                f"<h4>How to test this flow</h4><pre>{_esc(to_test_guide(value))}</pre>"
            )

        parts.append("</article>")
    parts.append("</section>")
    return "".join(parts)


_PAGE_CSS = """
body { font-family: system-ui, sans-serif; max-width: 860px; margin: 2rem auto; padding: 0 1rem; color: #1a1a1a; }
h1 { font-size: 1.4rem; }
h3 { margin-bottom: 0.25rem; }
article { border: 1px solid #ddd; border-radius: 8px; padding: 1rem; margin: 1rem 0; }
table { border-collapse: collapse; }
th, td { text-align: left; padding: 0.2rem 0.75rem 0.2rem 0; }
th { color: #555; font-weight: 600; }
pre { background: #f5f5f5; padding: 0.75rem; border-radius: 6px; overflow-x: auto; white-space: pre-wrap; }
.meta { color: #555; font-size: 0.9rem; }
.reasoning { color: #555; font-size: 0.9rem; font-style: italic; }
section.plan-summary { margin: 1rem 0; }
section.plan-summary h2 { font-size: 1.05rem; margin-bottom: 0.25rem; }
"""


def _plan_summary_html(plan: Optional[Plan]) -> str:
    """The plan-level "why" and "how to test" - above the per-step detail,
    since both describe the bundle as a whole, not any one step."""
    if plan is None:
        return ""
    parts: List[str] = []
    if plan.reasoning:
        parts.append(
            f"<section class='plan-summary'><h2>Why this plan</h2>"
            f"<p>{_esc(plan.reasoning)}</p></section>"
        )
    if plan.how_to_test:
        parts.append(
            f"<section class='plan-summary'><h2>How to test</h2>"
            f"<pre>{_esc(plan.how_to_test)}</pre></section>"
        )
    return "".join(parts)


def render_standalone_report(
    steps: List[StepResult], title: str, meta: str = "", plan: Optional[Plan] = None,
) -> str:
    """
    A complete, self-contained HTML document - what the web UI's "Download
    report" button hands out. Meant to be opened in a browser and printed to
    PDF from there (or emailed/shared as-is) for someone or something outside
    this tool to review before approval.

    Any Flow step's diagram needs mermaid.js to actually render once this is
    a plain file on someone's disk, no longer inside this app's own page or
    Claude's Artifact viewer - so the vendored copy this project already
    ships (static/vendor/mermaid.min.js, chosen over a CDN script tag for
    the same reason the app itself avoids one) gets inlined here too, but
    only when a step actually needs it.
    """
    body = render_html_fragment(steps)
    meta_html = f'<p class="meta">{_esc(meta)}</p>' if meta else ""
    summary_html = _plan_summary_html(plan)
    lwc_preview_script = ""
    if any(isinstance(r.value, LightningComponent) for r in steps):
        lwc_preview_js = _LWC_PREVIEW_JS.read_text(encoding="utf-8")
        slds_css = _SLDS_SUBSET_CSS.read_text(encoding="utf-8")
        lwc_preview_script = (
            f"<style>{slds_css}</style>"
            f"<script>{lwc_preview_js}</script>"
            "<script>"
            "document.querySelectorAll('.lwc-preview[data-lwc-html]').forEach((div) => {"
            "const b64 = div.getAttribute('data-lwc-html');"
            "div.removeAttribute('data-lwc-html');"
            "try {"
            "const bytes = Uint8Array.from(atob(b64), (c) => c.charCodeAt(0));"
            "const source = new TextDecoder('utf-8').decode(bytes);"
            "window.renderLwcPreview(div, source);"
            "} catch (err) { div.textContent = 'Could not render preview: ' + err.message; }"
            "});"
            "</script>"
        )
    mermaid_script = ""
    if any(isinstance(r.value, Flow) for r in steps):
        mermaid_js = _VENDOR_MERMAID_JS.read_text(encoding="utf-8")
        # Manual render, not startOnLoad - the same approach app.js's own
        # renderPlanStepDiagram already uses, proven to actually work with
        # this vendored build rather than relying on a load-timing race.
        mermaid_script = (
            f"<script>{mermaid_js}</script>"
            "<script>"
            'mermaid.initialize({startOnLoad:false,securityLevel:"strict"});'
            "document.querySelectorAll('pre.mermaid').forEach(async (pre, i) => {"
            "const source = pre.textContent;"
            "try {"
            "const {svg} = await mermaid.render('report-diagram-' + i, source);"
            "pre.outerHTML = svg;"
            "} catch (err) { pre.textContent = source + '\\n\\n// could not render: ' + err.message; }"
            "});"
            "</script>"
        )
    # A document-level CSP, not just relying on whatever headers (or none)
    # the page happens to be served with: server.py's own CSP is 'self'-only
    # for scripts, which is right for the live app but would silently break
    # this file's inlined mermaid.js the moment it's downloaded and reopened
    # from disk with no server in front of it to send headers at all. This
    # meta tag is what governs in that real, intended use case; it never
    # loosens the live app's own stricter HTTP header, since a page under
    # two CSPs (header + meta) is bound by whichever is more restrictive.
    csp_meta = (
        '<meta http-equiv="Content-Security-Policy" '
        'content="default-src \'none\'; script-src \'unsafe-inline\'; '
        'style-src \'unsafe-inline\'; img-src data:">'
        if mermaid_script or lwc_preview_script else ""
    )
    return (
        "<!doctype html><html><head><meta charset=\"utf-8\">"
        f"{csp_meta}<title>{_esc(title)}</title><style>{_PAGE_CSS}</style></head><body>"
        f"<h1>{_esc(title)}</h1>{meta_html}{summary_html}{body}"
        f"{mermaid_script}{lwc_preview_script}</body></html>"
    )
