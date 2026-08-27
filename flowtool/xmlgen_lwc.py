"""
LightningComponent IR -> deployable metadata.

An LWC has no XML body for its js/html/css - those are the source files
themselves, verbatim, same as ApexClass.body. What's generated here is the
`.js-meta.xml` sidecar (from the IR's structured is_exposed/targets/
api_version fields) plus the zip-path layout for every member file, since a
component is a bundle of files under `lwc/<name>/` rather than one flat file
the way a class or trigger is.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from typing import Dict

from .ir_lwc import LightningComponent
from .xmlgen import METADATA_NS, _bool, _sub


def generate_lwc(component: LightningComponent) -> Dict[str, str]:
    """
    Render a validated LightningComponent IR as a dict of zip-path -> content
    covering every member file of the bundle: the .js, .html, optional .css,
    and the synthesized .js-meta.xml sidecar. Callers (server.py's
    _deploy_files, xmlgen tests) can hand this straight to
    build_deploy_package's `files` argument.
    """
    base = f"lwc/{component.api_name}/{component.api_name}"
    files = {
        f"{base}.js": component.js,
        f"{base}.html": component.html,
        f"{base}.js-meta.xml": _meta_xml(component),
    }
    if component.css:
        files[f"{base}.css"] = component.css
    return files


def _meta_xml(component: LightningComponent) -> str:
    ET.register_namespace("", METADATA_NS)
    root = ET.Element(f"{{{METADATA_NS}}}LightningComponentBundle")
    _sub(root, "apiVersion", component.api_version)
    _sub(root, "isExposed", _bool(component.is_exposed))
    for target in component.targets:
        targets_el = root.find("targets")
        if targets_el is None:
            targets_el = _sub(root, "targets")
        _sub(targets_el, "target", target)

    ET.indent(root, space="    ")
    body = ET.tostring(root, encoding="unicode")
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + body + "\n"
