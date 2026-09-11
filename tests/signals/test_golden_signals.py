"""
SIG-001 — Golden signal fixtures pin end-to-end behaviour.

Each fixture in `tests/fixtures/signals/` records what the deterministic part
of the signal path produced for a fixed market input: the data-quality
verdict, the feature vector at the decision bar, the risk-gate outcome and the
final action. A refactor that changes what the bot trades then shows up as a
diff in a reviewed file rather than as a change in the PnL.

**How the protection actually works.** Regenerating a fixture makes its test
pass by construction, so the mechanism is not the assertion -- it is that the
regenerated file lands in a diff that a human reads. A regeneration whose diff
nobody can explain is the finding. That is why the generator prints "then READ
the diff" and why this module asserts on the *content* of the fixtures as well
as on their reproducibility: a fixture set that has quietly become trivial
(every case blocked, every feature null) would still round-trip perfectly.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.signals.golden import CASES, GoldenCase, build_record

PROJECT_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_DIR = PROJECT_ROOT / "tests" / "fixtures" / "signals"


def load(case_id: str) -> dict:
    return json.loads((FIXTURE_DIR / f"{case_id}.json").read_text(encoding="utf-8"))


#: Replaying a case runs the whole feature pipeline, which is a few seconds.
#: Each case is asserted from four angles, so without this the module pays
#: that cost four times over for no extra evidence -- the replay is
#: deterministic, which is the first thing the suite establishes.
_REPLAYED: dict[str, dict] = {}


def replay(case: GoldenCase) -> dict:
    if case.case_id not in _REPLAYED:
        _REPLAYED[case.case_id] = build_record(case)
    return _REPLAYED[case.case_id]


CASE_IDS = [case.case_id for case in CASES]


class TestTheFixturesExist:
    def test_there_is_a_fixture_per_case(self):
        on_disk = {p.stem for p in FIXTURE_DIR.glob("*.json")}
        assert on_disk == set(CASE_IDS)

    def test_no_orphan_fixtures(self):
        # A fixture whose case was deleted is a file nothing regenerates, so
        # it silently stops tracking anything.
        for path in FIXTURE_DIR.glob("*.json"):
            assert path.stem in CASE_IDS, f"{path.name} has no case in tests/signals/golden.py"


@pytest.mark.parametrize("case", CASES, ids=CASE_IDS)
class TestEachCaseReproducesItsFixture:
    def test_the_whole_record_matches(self, case: GoldenCase):
        assert replay(case) == load(case.case_id)

    def test_the_data_quality_verdict_matches(self, case: GoldenCase):
        recorded = load(case.case_id)["data_quality"]
        fresh = replay(case)["data_quality"]
        assert fresh["passed"] == recorded["passed"]
        assert fresh["reason"] == recorded["reason"]

    def test_the_final_action_matches(self, case: GoldenCase):
        assert replay(case)["expected_action"] == load(case.case_id)["expected_action"]

    def test_the_market_input_is_reproducible(self, case: GoldenCase):
        # If the synthetic path itself drifts, every other assertion in this
        # module is comparing two different markets and would still pass.
        assert replay(case)["market"] == load(case.case_id)["market"]

    def test_a_second_replay_is_identical(self, case: GoldenCase):
        # Determinism of the replay itself, asserted against a fresh build
        # rather than the memo -- otherwise the memo would be what is tested.
        assert build_record(case) == replay(case)


class TestTheFixtureSetIsNotTrivial:
    """
    A golden suite that has quietly become vacuous round-trips perfectly.

    These assertions are about the *shape* of the case set rather than any one
    case: they fail if the fixtures stop covering the situations they were
    written to cover.
    """

    @pytest.fixture
    def records(self) -> list[dict]:
        return [load(case_id) for case_id in CASE_IDS]

    def test_at_least_one_case_reaches_an_order(self, records):
        assert [r for r in records if r["expected_action"].startswith("enter_")]

    def test_both_directions_are_covered(self, records):
        actions = {r["expected_action"] for r in records}
        assert "enter_long" in actions
        assert "enter_short" in actions

    def test_a_risk_gate_halt_is_covered(self, records):
        halted = [r for r in records if r["risk"] and not r["risk"]["passed"]]
        assert halted, "no case exercises a risk-gate halt"

    def test_a_data_quality_rejection_is_covered(self, records):
        rejected = [r for r in records if not r["data_quality"]["passed"]]
        assert len(rejected) >= 2, "corrupt and stale should both be covered"

    def test_a_flat_signal_is_covered(self, records):
        assert [r for r in records if r["expected_action"] == "no_trade_flat_signal"]

    def test_passing_cases_carry_a_real_feature_vector(self, records):
        for record in records:
            if not record["data_quality"]["passed"]:
                assert record["features"] is None
                continue
            features = record["features"]
            assert features, f"{record['case_id']} has no features"
            assert len(features) >= 5
            populated = [v for v in features.values() if v is not None]
            assert populated, f"{record['case_id']} feature vector is entirely null"

    def test_rejected_cases_never_carry_a_risk_decision(self, records):
        # A frame that never reached the feature pipeline must not appear to
        # have reached the gates either.
        for record in records:
            if not record["data_quality"]["passed"]:
                assert record["risk"] is None
                assert record["expected_action"] == "no_trade_data_quality"

    def test_every_case_documents_itself(self, records):
        for record in records:
            assert len(record["description"]) > 40
            assert record["tags"]


class TestTheGeneratorAndTheTestAgree:
    def test_the_check_mode_passes(self):
        # The same command CI runs. It exists so a stale fixture is a build
        # failure with a clear instruction rather than an opaque assertion
        # diff halfway down a parametrised run.
        import subprocess
        import sys

        result = subprocess.run(
            [sys.executable, "scripts/generate_signal_fixtures.py", "--check"],
            capture_output=True,
            text=True,
            cwd=PROJECT_ROOT,
            timeout=900,
            check=False,
        )
        assert result.returncode == 0, result.stdout + result.stderr
