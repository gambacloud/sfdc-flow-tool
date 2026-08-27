"""
The LWC IR's job is thin, same shape as Apex's: reject an empty js/html and a
bad api_name. heuristic_errors is the real gate on the free-form js/html -
these pin down what it catches and what it deliberately doesn't (see
ir_lwc.py's module docstring).
"""

import pytest
from pydantic import ValidationError

from flowtool.ir_lwc import LightningComponent, heuristic_errors

VALID_JS = (
    "import { LightningElement } from 'lwc';\n\n"
    "export default class ContactCard extends LightningElement {}\n"
)
VALID_HTML = "<template>\n    <div>Hello</div>\n</template>\n"


class TestLightningComponentModel:
    def test_valid_component_is_accepted(self):
        component = LightningComponent(api_name="contactCard", js=VALID_JS, html=VALID_HTML)
        assert component.is_exposed is False
        assert component.targets == []
        assert component.api_version == "62.0"

    def test_empty_js_is_rejected(self):
        with pytest.raises(ValidationError, match="js must not be empty"):
            LightningComponent(api_name="contactCard", js="   ", html=VALID_HTML)

    def test_empty_html_is_rejected(self):
        with pytest.raises(ValidationError, match="html must not be empty"):
            LightningComponent(api_name="contactCard", js=VALID_JS, html="  ")

    @pytest.mark.parametrize("name", ["ContactCard", "contact_card", "1contactCard", "contact card"])
    def test_rejects_non_camel_case_name(self, name):
        with pytest.raises(ValidationError, match="must be camelCase"):
            LightningComponent(api_name=name, js=VALID_JS, html=VALID_HTML)

    def test_name_over_40_chars_is_rejected(self):
        too_long = "a" + "b" * 40
        with pytest.raises(ValidationError, match="over Salesforce's 40-character limit"):
            LightningComponent(api_name=too_long, js=VALID_JS, html=VALID_HTML)


class TestHeuristicErrors:
    def test_valid_component_has_no_problems(self):
        assert heuristic_errors("contactCard", VALID_JS, VALID_HTML) == []

    def test_unclosed_brace_is_caught(self):
        js = "export default class ContactCard extends LightningElement {\n"
        problems = heuristic_errors("contactCard", js, VALID_HTML)
        assert any("unbalanced brackets" in p for p in problems)

    def test_missing_class_declaration_is_caught(self):
        problems = heuristic_errors("contactCard", "const x = 1;", VALID_HTML)
        assert any("export default class" in p for p in problems)

    def test_class_name_mismatch_is_caught(self):
        js = "export default class WrongName extends LightningElement {}"
        problems = heuristic_errors("contactCard", js, VALID_HTML)
        assert any("WrongName" in p for p in problems)
        assert any("ContactCard" in p for p in problems)

    def test_missing_template_root_is_caught(self):
        problems = heuristic_errors("contactCard", VALID_JS, "<div>no template</div>")
        assert any("<template>" in p for p in problems)

    def test_unbalanced_template_tags_is_caught(self):
        problems = heuristic_errors("contactCard", VALID_JS, "<template><div></div>")
        assert any("unbalanced <template>" in p for p in problems)
