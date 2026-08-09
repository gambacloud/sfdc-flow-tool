"""
The half of verify.py that needs no org, run by the test suite.

verify.py exists because the org is the only authority on whether the XML is
right, and most of it can only run against one. But two parts of it are pure
local facts and would otherwise rot unnoticed between org runs:

  - every guard must still refuse what it says it refuses. A guard that stops
    guarding is worse than no guard, because the file still claims the check
    exists.
  - every shape must still round-trip. If one does not, the org run will say so
    - but only if somebody runs it, and the point of this file is that nobody
    reliably does.

What is deliberately not here: whether Salesforce accepts the XML. That needs
`python verify.py --org dev`, and nothing in this suite touches a network.
"""

import pytest

from flowtool.parse import parse_flow
from flowtool.xmlgen import generate
from verify import GUARDS, SHAPES, survives_round_trip


def ids(cases):
    return [f"{case.group}: {case.name}" for case in cases]


class TestGuards:
    @pytest.mark.parametrize("guard", GUARDS, ids=ids(GUARDS))
    def test_the_ir_still_refuses_it(self, guard):
        with pytest.raises(Exception):
            guard.build()

    @pytest.mark.parametrize("guard", GUARDS, ids=ids(GUARDS))
    def test_it_records_what_the_org_does(self, guard):
        """
        The org's behaviour is the reason each guard exists. Half of them read
        "the org deploys it happily", which is the whole argument for checking
        it here - and an argument nobody can re-check is a comment, not a
        record.
        """
        assert guard.org, f"{guard.name} does not say what the org does"


class TestShapes:
    @pytest.mark.parametrize("shape", SHAPES, ids=ids(SHAPES))
    def test_it_survives_a_round_trip(self, shape):
        assert survives_round_trip(shape.flow)

    @pytest.mark.parametrize("shape", SHAPES, ids=ids(SHAPES))
    def test_it_compiles_to_something_parseable(self, shape):
        assert parse_flow(generate(shape.flow), api_name=shape.flow.api_name)

    def test_no_two_shapes_share_an_api_name(self):
        """
        They are validated concurrently against one org. Two cases under one
        name would be two deploys of the same flow at the same time, and the
        result would depend on which landed second.
        """
        names = [shape.flow.api_name for shape in SHAPES]
        assert len(names) == len(set(names)), sorted(
            n for n in names if names.count(n) > 1
        )

    def test_every_case_is_namespaced(self):
        """
        checkOnly never writes, but the names still show up in deploy logs, and
        anything running against a real org should be obvious about whose it is.
        """
        for shape in SHAPES:
            assert shape.flow.api_name.startswith("Flow_Tool_Verify_")


class TestCoverage:
    """
    Not a coverage metric - a reminder. These are the features whose metadata
    shape was settled by asking the org rather than by reading anything, so each
    is a thing that would be guesswork again if its case disappeared.
    """

    @pytest.mark.parametrize("group", [
        "records", "assignments", "logic", "resources", "screens", "choices",
        "components", "paths", "pause", "elements",
    ])
    def test_the_group_still_has_cases(self, group):
        assert [s for s in SHAPES if s.group == group]

    def test_the_riskiest_checks_have_guards(self):
        """
        The four the org accepts and then gets wrong at runtime. Every one of
        them cost a debugging session to find.
        """
        guarded = {f"{g.group}: {g.name}" for g in GUARDS}
        for expected in [
            "logic: a condition number past the end",
            "components: an output assigned to a variable that does not exist",
            "paths: a path counting from a field it does not name",
            "pause: a Pause with no time to resume at",
        ]:
            assert expected in guarded
