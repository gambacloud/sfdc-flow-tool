"""
LwcGenerator reuses llm.py's generic repair loop, but its real gate is
`_extra_error` - the heuristic check - since there is no Pydantic structure to
lean on for js/html. These pin down that a heuristic failure goes round the
loop exactly like a validation error does for Flow, same as ApexClassGenerator.
"""

from flowtool.ir_lwc import LightningComponent
from flowtool.llm import LwcGenerator
from tests.test_llm import ScriptedProvider

VALID = {
    "api_name": "contactCard",
    "js": (
        "import { LightningElement } from 'lwc';\n\n"
        "export default class ContactCard extends LightningElement {}\n"
    ),
    "html": "<template>\n    <div>Hello</div>\n</template>\n",
}

UNBALANCED = {
    "api_name": "contactCard",
    "js": "export default class ContactCard extends LightningElement {\n",
    "html": VALID["html"],
}

NAME_MISMATCH = {
    "api_name": "contactCard",
    "js": "export default class WrongName extends LightningElement {}",
    "html": VALID["html"],
}


class TestLwcGenerator:
    def test_valid_first_try_costs_no_repairs(self):
        provider = ScriptedProvider(VALID)
        result = LwcGenerator(provider).generate("a card that shows the contact's name")
        assert result.repairs == 0
        assert isinstance(result.value, LightningComponent)
        assert result.value.api_name == "contactCard"

    def test_unbalanced_braces_are_repaired(self):
        provider = ScriptedProvider(UNBALANCED, VALID)
        result = LwcGenerator(provider).generate("...")
        assert result.repairs == 1

        complaint = provider.calls[1][-1].content
        assert "unbalanced brackets" in complaint

    def test_name_mismatch_is_repaired(self):
        provider = ScriptedProvider(NAME_MISMATCH, VALID)
        result = LwcGenerator(provider).generate("...")
        assert result.repairs == 1

        complaint = provider.calls[1][-1].content
        assert "WrongName" in complaint
        assert "ContactCard" in complaint

    def test_heuristic_check_runs_before_pydantic_validation(self):
        provider = ScriptedProvider(UNBALANCED, VALID)
        LwcGenerator(provider).generate("...")
        assert len(provider.calls) == 2, "should have needed a second attempt"
