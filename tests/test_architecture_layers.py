"""
The direction dependencies are allowed to run in, and the six places they
already run backwards.

`check_import_cycles` refuses a *module*-level cycle, which is the easy case:
it fails at import time, so it reports itself. A *package*-level cycle never
fails. It hides behind submodule and deferred imports and surfaces instead as
two packages that cannot be changed, tested or reasoned about apart -- `api`
and `engine` import each other today, and so do `engine` and `strategies`.

config/architecture_layers.json declares the layer order and the upward edges
that already exist. These tests pin the contract itself, and the rule that
decides it, on graphs that are not this repository's -- the live repository is
checked by test_repository_satisfies_every_invariant.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONTRACT = PROJECT_ROOT / "config" / "architecture_layers.json"


@pytest.fixture(scope="module")
def contract() -> dict:
    return json.loads(CONTRACT.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def invariants():
    import importlib.util

    script = PROJECT_ROOT / "scripts" / "check_static_invariants.py"
    spec = importlib.util.spec_from_file_location("check_static_invariants", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _toy() -> dict:
    return {
        "layers": [
            {"name": "bottom", "packages": ["config"], "why": "x"},
            {"name": "middle", "packages": ["risk"], "why": "x"},
            {"name": "top", "packages": ["api"], "why": "x"},
        ],
        "accepted_upward_edges": [],
    }


class TestTheContractIsWellFormed:
    def test_every_package_is_placed_exactly_once(self, contract):
        """
        A package in two layers has no rank, and the rule would silently take
        whichever came last.
        """
        seen: list[str] = []
        for layer in contract["layers"]:
            seen.extend(layer["packages"])
        assert len(seen) == len(set(seen)), sorted(p for p in seen if seen.count(p) > 1)

    def test_every_layer_says_why_it_exists(self, contract):
        """
        A layer order with no argument behind it is a list, and a list gets
        edited to make a violation go away.
        """
        for layer in contract["layers"]:
            assert layer["why"].strip()
            assert layer["packages"]

    def test_every_accepted_inversion_carries_its_argument(self, contract):
        """
        The accepted list is debt. Debt with no note attached is debt nobody
        can pay off, because nobody knows what it was for.
        """
        for edge in contract["accepted_upward_edges"]:
            assert edge["why"].strip(), edge
            assert edge["from"] != edge["to"]

    def test_the_edge_endpoints_are_real_placed_packages(self, contract):
        placed = {p for layer in contract["layers"] for p in layer["packages"]}
        for edge in contract["accepted_upward_edges"]:
            assert edge["from"] in placed, edge
            assert edge["to"] in placed, edge

    def test_the_accepted_endpoints_exist_on_disk(self, contract):
        """A renamed package must not leave a permanent phantom exemption."""
        for edge in contract["accepted_upward_edges"]:
            for end in (edge["from"], edge["to"]):
                package = PROJECT_ROOT / "src" / end
                assert package.is_dir() or package.with_suffix(".py").is_file(), end

    def test_the_edge_layer_is_at_the_top(self, contract):
        """
        Nothing below may import `api`. If transport stops being the outermost
        thing, that is a decision, not a drift.
        """
        assert contract["layers"][-1]["packages"] == ["api"]


class TestTheRuleBites:
    def test_an_upward_edge_is_reported(self, invariants):
        problems = invariants._layering_problems(
            _toy(), {("risk", "api"): "src/risk/x.py"}, {"risk", "api"}
        )
        assert len(problems) == 1
        assert "src/risk imports src/api" in problems[0]
        assert "middle -> top" in problems[0]

    def test_a_downward_edge_is_fine(self, invariants):
        assert not invariants._layering_problems(
            _toy(), {("api", "config"): "src/api/x.py"}, {"api", "config"}
        )

    def test_a_same_layer_edge_is_fine(self, invariants):
        contract = _toy()
        contract["layers"][1]["packages"] = ["risk", "execution"]
        assert not invariants._layering_problems(
            contract, {("risk", "execution"): "src/risk/x.py"}, {"risk", "execution"}
        )

    def test_an_accepted_edge_is_not_reported(self, invariants):
        contract = _toy()
        contract["accepted_upward_edges"] = [{"from": "risk", "to": "api", "why": "x"}]
        assert not invariants._layering_problems(
            contract, {("risk", "api"): "src/risk/x.py"}, {"risk", "api"}
        )

    def test_a_fixed_inversion_must_leave_the_list(self, invariants):
        """
        The ratchet. An exemption kept after the import is gone is a slot
        already paid for, waiting for the next inversion to occupy it.
        """
        contract = _toy()
        contract["accepted_upward_edges"] = [{"from": "risk", "to": "api", "why": "x"}]
        problems = invariants._layering_problems(contract, {}, {"risk", "api"})
        assert len(problems) == 1
        assert "bank the fix" in problems[0]

    def test_a_package_in_no_layer_is_reported(self, invariants):
        """
        Adding a top-level package is an architectural decision. Defaulting it
        into some layer would make the decision silently.
        """
        problems = invariants._layering_problems(_toy(), {}, {"config", "newthing"})
        assert len(problems) == 1
        assert "src/newthing is in no layer" in problems[0]


class TestItIsWiredIn:
    def test_the_check_runs_with_the_others(self, invariants):
        """
        A checker nothing calls is the exact failure this script was written
        to catch, so it had better not be one.
        """
        assert "layering" in {label for label, _ in invariants.CHECKS}

    def test_the_live_repository_has_exactly_the_declared_inversions(self, invariants, contract):
        """
        Belt and braces with test_repository_satisfies_every_invariant: this
        one fails with the actual list when the two disagree, which is the
        message somebody fixing an inversion needs to see.
        """
        edges, _ = invariants._package_edges()
        rank = {pkg: i for i, layer in enumerate(contract["layers"]) for pkg in layer["packages"]}
        upward = {(a, b) for (a, b) in edges if a in rank and b in rank and rank[b] > rank[a]}
        declared = {(e["from"], e["to"]) for e in contract["accepted_upward_edges"]}
        assert upward == declared
