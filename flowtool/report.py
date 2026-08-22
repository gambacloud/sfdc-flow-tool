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

import html
from typing import Any, List

from .ir import Flow
from .ir_apex import ApexClass
from .ir_object import CustomField, CustomObject
from .planner import StepResult


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
        elif isinstance(value, ApexClass):
            parts.append(f"<pre><code>{_esc(value.body)}</code></pre>")
        elif isinstance(value, Flow):
            parts.append(f"<p>{len(value.elements)} element(s)</p>")
            parts.append("<ol>")
            for element in value.elements:
                label = getattr(element, "label", None) or getattr(
                    element, "name", type(element).__name__
                )
                parts.append(f"<li>{_esc(label)}</li>")
            parts.append("</ol>")

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
pre { background: #f5f5f5; padding: 0.75rem; border-radius: 6px; overflow-x: auto; }
.meta { color: #555; font-size: 0.9rem; }
"""


def render_standalone_report(steps: List[StepResult], title: str, meta: str = "") -> str:
    """
    A complete, self-contained HTML document - what the web UI's "Download
    report" button hands out. Meant to be opened in a browser and printed to
    PDF from there (or emailed/shared as-is) for someone or something outside
    this tool to review before approval.
    """
    body = render_html_fragment(steps)
    meta_html = f'<p class="meta">{_esc(meta)}</p>' if meta else ""
    return (
        "<!doctype html><html><head><meta charset=\"utf-8\">"
        f"<title>{_esc(title)}</title><style>{_PAGE_CSS}</style></head><body>"
        f"<h1>{_esc(title)}</h1>{meta_html}{body}</body></html>"
    )
