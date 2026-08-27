"""
Golden checks on the LWC compiler: js/html/css pass through verbatim under
the right zip paths, and the synthesized .js-meta.xml sidecar carries the
fields Salesforce actually reads from it.
"""

import xml.etree.ElementTree as ET

from flowtool.ir_lwc import LightningComponent
from flowtool.xmlgen_lwc import METADATA_NS, generate_lwc

NS = {"m": METADATA_NS}


class TestGenerateLwc:
    def test_js_and_html_pass_through_verbatim(self):
        js = "export default class Foo extends LightningElement {}"
        html = "<template><div>{greeting}</div></template>"
        component = LightningComponent(api_name="foo", js=js, html=html)
        files = generate_lwc(component)
        assert files["lwc/foo/foo.js"] == js
        assert files["lwc/foo/foo.html"] == html

    def test_no_css_file_when_css_is_absent(self):
        component = LightningComponent(
            api_name="foo",
            js="export default class Foo extends LightningElement {}",
            html="<template></template>",
        )
        files = generate_lwc(component)
        assert "lwc/foo/foo.css" not in files

    def test_css_file_present_when_provided(self):
        component = LightningComponent(
            api_name="foo",
            js="export default class Foo extends LightningElement {}",
            html="<template></template>",
            css="div { color: red; }",
        )
        files = generate_lwc(component)
        assert files["lwc/foo/foo.css"] == "div { color: red; }"

    def test_sidecar_carries_version_and_exposure(self):
        component = LightningComponent(
            api_name="foo",
            js="export default class Foo extends LightningElement {}",
            html="<template></template>",
            api_version="60.0",
            is_exposed=True,
            targets=["lightning__RecordPage", "lightning__AppPage"],
        )
        files = generate_lwc(component)
        root = ET.fromstring(files["lwc/foo/foo.js-meta.xml"])
        assert root.tag == f"{{{METADATA_NS}}}LightningComponentBundle"
        assert root.find("m:apiVersion", NS).text == "60.0"
        assert root.find("m:isExposed", NS).text == "true"
        targets = [t.text for t in root.findall("m:targets/m:target", NS)]
        assert targets == ["lightning__RecordPage", "lightning__AppPage"]

    def test_sidecar_default_not_exposed_with_no_targets(self):
        component = LightningComponent(
            api_name="foo",
            js="export default class Foo extends LightningElement {}",
            html="<template></template>",
        )
        files = generate_lwc(component)
        root = ET.fromstring(files["lwc/foo/foo.js-meta.xml"])
        assert root.find("m:isExposed", NS).text == "false"
        assert root.find("m:targets", NS) is None
