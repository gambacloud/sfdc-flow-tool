"""
How to actually exercise a Lightning Web Component once it is deployed -
derived from the component's own source and targets, the same idea as
mermaid.to_test_guide for a flow: concrete steps, not generic advice.

Plain-text/markdown-ish output, since both consumers (the report's <pre> and
the app's Test tab) show it verbatim.
"""

from __future__ import annotations

import re
from typing import List

from .ir_lwc import LightningComponent

_APEX_IMPORT_RE = re.compile(r"""@salesforce/apex/(\w+)\.(\w+)""")
_SCHEMA_IMPORT_RE = re.compile(r"""@salesforce/schema/(\w+)\.([\w.]+)""")
_BUTTON_LABEL_RE = re.compile(r"""<lightning-button[^>]*?\blabel\s*=\s*["']([^"']+)["']""", re.S)
_PLAIN_BUTTON_RE = re.compile(r"<button[^>]*>\s*([^<{]+?)\s*</button>", re.S)

_TARGET_STEPS = {
    "lightning__RecordPage": "Open a record page, click the gear icon > **Edit Page**",
    "lightning__AppPage": "Setup > **Lightning App Builder** > New > App Page (or edit an existing one)",
    "lightning__HomePage": "Setup > **Lightning App Builder** > New > Home Page (or edit an existing one)",
}


def _dedupe(items: List[str]) -> List[str]:
    return list(dict.fromkeys(items))


def to_lwc_test_guide(component: LightningComponent) -> str:
    js, html = component.js, component.html
    apex = _dedupe([f"{c}.{m}" for c, m in _APEX_IMPORT_RE.findall(js)])
    schema = _dedupe([f"{o}.{f}" for o, f in _SCHEMA_IMPORT_RE.findall(js)])
    objects = _dedupe([s.split(".")[0] for s in schema])
    buttons = _dedupe(_BUTTON_LABEL_RE.findall(html) + [b.strip() for b in _PLAIN_BUTTON_RE.findall(html)])
    tag = "c-" + re.sub(r"([A-Z])", lambda m: "-" + m.group(1).lower(), component.api_name)

    lines: List[str] = [f"# Testing {component.api_name}", ""]

    lines += ["## 1. Make sure its dependencies are in the org", ""]
    dependencies = False
    if apex:
        dependencies = True
        lines.append(
            "- Apex it calls: " + ", ".join(f"`{a}`" for a in apex) +
            " - deploy first, and make sure your user can run the class "
            "(Setup > Permission Sets > Apex Class Access, or your profile)."
        )
    custom = [s for s in schema if s.split(".", 1)[1].endswith("__c")]
    if custom:
        dependencies = True
        lines.append(
            "- Custom fields it reads: " + ", ".join(f"`{s}`" for s in custom) +
            " - deploy first. New custom fields are invisible to non-admin "
            "profiles until you grant field-level security, so an empty value "
            "in the component usually means FLS, not a bug."
        )
    if not dependencies:
        lines.append("- None found in its source - nothing else to deploy first.")
    lines.append("")

    lines += ["## 2. Put it on a page", ""]
    targets = [t for t in component.targets if t in _TARGET_STEPS]
    if not component.is_exposed or not component.targets:
        lines += [
            f"This component is not exposed to App Builder, so it cannot be dropped "
            f"onto a page by itself. Use it from another component with `<{tag}>`, "
            "or redeploy with `isExposed` and a target (e.g. "
            "`lightning__RecordPage`) if you meant it to be placeable.",
            "",
        ]
    else:
        for i, target in enumerate(component.targets, 1):
            where = _TARGET_STEPS.get(target, f"Add it where the `{target}` target applies")
            lines.append(f"{i}. {where}, then drag **{component.api_name}** from the "
                         "**Custom** section of the components list onto the page.")
        lines += [
            "",
            "Click **Save**, then **Activate** (assign it as the org default, or to "
            "your app/record type), and go back to the page.",
        ]
        if "lightning__RecordPage" in targets:
            obj = objects[0] if objects else "the object this component is meant for"
            lines += [
                "",
                f"Open a **{obj}** record - ideally one with enough data to make "
                "the component show something (an empty record proves little).",
            ]
        lines.append("")

    lines += ["## 3. What to check", ""]
    lines.append("- The component renders without a red error banner or blank space.")
    if apex or schema:
        lines.append("- The data it shows matches the record (compare against the record's own fields).")
    if buttons:
        lines.append(
            "- Click each action and confirm it does something visible: " +
            ", ".join(f"**{b}**" for b in buttons) + "."
        )
    if "ShowToastEvent" in js:
        lines.append("- After an action, a toast appears (success or error) - read its text.")
    if apex:
        lines.append(
            "- After an action that changes data, open the record's fields and "
            "confirm the values changed there too - not just in the component."
        )
    lines.append("")

    lines += [
        "## 4. If it does not work",
        "",
        "- Open the browser console (F12) - LWC errors and failed Apex calls are logged there.",
        "- Setup > **Debug Mode** > enable it for your user for readable stack traces.",
        "- Setup > **Debug Logs** to watch the Apex side of any button click.",
        "",
    ]
    return "\n".join(lines)
