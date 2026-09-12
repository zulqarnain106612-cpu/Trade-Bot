"""
INV-010, REL-001 — live trading is unlocked by evidence, not by a flag.

A bot reaches production on a Tuesday evening, on the strength of a backtest
that looked good and one line in a `.env` file. Nothing objects, because
nothing was ever asked to. These tests are the asking.

The cases that matter are not the happy path. They are:

  * a record that is excellent on every measure but too small to mean
    anything — the lucky fortnight, which is how most bots qualify;
  * a record that passes everything except the drawdown, which a weighted
    score would let an exceptional Sharpe buy its way past;
  * a record that passed under the old thresholds and is re-checked after
    somebody tightened them, because a standard that applies only to future
    evidence is not a standard;
  * every way of arriving at live trading without a record at all.
"""

from __future__ import annotations

import dataclasses
import json

import pytest

from src.risk.paper_qualification import (
    CRITERIA,
    SCHEMA_VERSION,
    Criterion,
    NotQualifiedError,
    PaperRecord,
    assert_qualified_for_live,
    criteria_table,
    evaluate,
    load_qualification,
    save_qualification,
)

PASSING = PaperRecord(
    started_at="2026-08-01T00:00:00+00:00",
    ended_at="2026-09-05T00:00:00+00:00",
    duration_days=35.0,
    closed_trades=140,
    sharpe=1.4,
    max_drawdown_pct=0.11,
    win_rate=0.47,
    profit_factor=1.6,
    max_slippage_error_bps=12.0,
    risk_limit_breaches=0,
)


def record(**changes) -> PaperRecord:
    return dataclasses.replace(PASSING, **changes)


class TestTheCriteriaThemselves:
    @pytest.mark.parametrize("criterion", list(CRITERIA), ids=[c.name for c in CRITERIA])
    def test_every_criterion_is_justified(self, criterion: Criterion):
        # A threshold with no reasoning is the one that gets lowered the first
        # time it fails.
        assert len(criterion.rationale) > 50

    def test_every_criterion_maps_to_a_measured_field(self):
        for criterion in CRITERIA:
            assert hasattr(PASSING, criterion.name), criterion.name

    def test_the_table_is_not_empty_and_has_no_duplicates(self):
        names = [c.name for c in criteria_table()]
        assert len(names) >= 6
        assert len(names) == len(set(names))

    def test_the_sample_size_criteria_exist(self):
        # The two that stop a lucky fortnight qualifying.
        names = {c.name for c in CRITERIA}
        assert {"duration_days", "closed_trades"} <= names

    def test_risk_breaches_must_be_zero(self):
        breaches = next(c for c in CRITERIA if c.name == "risk_limit_breaches")
        assert breaches.threshold == 0.0
        assert breaches.higher_is_better is False


class TestEvaluation:
    def test_a_strong_record_qualifies(self):
        assert evaluate(PASSING).qualified

    @pytest.mark.parametrize(
        "changes",
        [
            {"duration_days": 5.0},
            {"closed_trades": 12},
            {"sharpe": 0.4},
            {"max_drawdown_pct": 0.45},
            {"win_rate": 0.20},
            {"profit_factor": 0.9},
            {"max_slippage_error_bps": 80.0},
            {"risk_limit_breaches": 1},
        ],
        ids=lambda c: next(iter(c)),
    )
    def test_any_single_failure_disqualifies(self, changes):
        # Not a score. A weighted average would let an exceptional Sharpe buy
        # its way past a drawdown that would have ended the account.
        result = evaluate(record(**changes))
        assert not result.qualified
        assert {f.criterion.name for f in result.failures} == set(changes)

    def test_the_lucky_fortnight_does_not_qualify(self):
        # Superb on every quality measure, and meaningless: eight trades over
        # nine days says nothing about either number.
        result = evaluate(record(duration_days=9.0, closed_trades=8, sharpe=4.0, profit_factor=3.0))
        assert not result.qualified
        assert {f.criterion.name for f in result.failures} == {
            "duration_days",
            "closed_trades",
        }

    def test_one_risk_breach_is_enough(self):
        assert not evaluate(record(risk_limit_breaches=1)).qualified

    def test_every_failure_is_reported_not_just_the_first(self):
        # An operator fixing one problem needs to know about the other three
        # now, not a month later when the next attempt fails on the next one.
        result = evaluate(record(sharpe=0.1, win_rate=0.1, profit_factor=0.5))
        assert len(result.failures) == 3

    def test_the_report_names_the_measurement_and_the_threshold(self):
        result = evaluate(record(sharpe=0.4))
        rendered = result.report()
        assert "sharpe" in rendered
        assert "0.4" in rendered
        assert "at least" in rendered

    def test_a_boundary_value_passes(self):
        # Exactly at the threshold is a pass; a strictly-greater comparison
        # would make the published number a lie by one tick.
        assert evaluate(
            record(sharpe=1.0, max_drawdown_pct=0.20, closed_trades=100, duration_days=30.0)
        ).qualified


class TestPersistence:
    def test_a_passing_record_round_trips(self, tmp_path):
        path = tmp_path / "qualification.json"
        save_qualification(evaluate(PASSING), path)
        stored = load_qualification(path)
        assert stored["schema_version"] == SCHEMA_VERSION
        assert stored["record"]["closed_trades"] == 140

    def test_a_failing_record_is_refused(self, tmp_path):
        # The absence of a file is a clearer statement than a file saying no,
        # and a stored "not qualified" is a thing somebody can edit.
        path = tmp_path / "qualification.json"
        with pytest.raises(NotQualifiedError):
            save_qualification(evaluate(record(sharpe=0.2)), path)
        assert not path.exists()

    def test_the_thresholds_in_force_are_recorded(self, tmp_path):
        # So a reviewer can see what the record was judged against.
        path = tmp_path / "qualification.json"
        save_qualification(evaluate(PASSING), path)
        stored = load_qualification(path)
        assert set(stored["criteria"]) == {c.name for c in CRITERIA}


class TestTheLiveGate:
    def test_a_stored_passing_record_unlocks(self, tmp_path):
        path = tmp_path / "qualification.json"
        save_qualification(evaluate(PASSING), path)
        assert_qualified_for_live(path)  # must not raise

    def test_no_record_at_all_is_refused(self, tmp_path):
        with pytest.raises(NotQualifiedError, match="has not been earned"):
            assert_qualified_for_live(tmp_path / "absent.json")

    def test_a_malformed_record_is_refused(self, tmp_path):
        path = tmp_path / "qualification.json"
        path.write_text("{not json")
        with pytest.raises(NotQualifiedError):
            assert_qualified_for_live(path)

    def test_an_empty_object_is_refused(self, tmp_path):
        path = tmp_path / "qualification.json"
        path.write_text("{}")
        with pytest.raises(NotQualifiedError):
            assert_qualified_for_live(path)

    def test_a_record_with_no_measurements_is_refused(self, tmp_path):
        path = tmp_path / "qualification.json"
        path.write_text(json.dumps({"schema_version": SCHEMA_VERSION, "qualified_at": "now"}))
        with pytest.raises(NotQualifiedError, match="no measurements"):
            assert_qualified_for_live(path)

    def test_an_incomplete_record_is_refused(self, tmp_path):
        path = tmp_path / "qualification.json"
        path.write_text(json.dumps({"schema_version": SCHEMA_VERSION, "record": {"sharpe": 3.0}}))
        with pytest.raises(NotQualifiedError, match="incomplete"):
            assert_qualified_for_live(path)

    def test_a_forged_pass_is_re_evaluated_not_trusted(self, tmp_path):
        # The file says qualified; the measurements say otherwise. The gate
        # re-runs the criteria rather than believing the verdict, so editing
        # the file is not enough -- the numbers have to be edited too, which
        # is a different kind of lie and a visible one.
        path = tmp_path / "qualification.json"
        save_qualification(evaluate(PASSING), path)
        stored = json.loads(path.read_text())
        stored["record"]["closed_trades"] = 3
        path.write_text(json.dumps(stored))
        with pytest.raises(NotQualifiedError, match="no longer meets"):
            assert_qualified_for_live(path)

    def test_a_record_from_an_older_schema_is_refused(self, tmp_path):
        path = tmp_path / "qualification.json"
        save_qualification(evaluate(PASSING), path)
        stored = json.loads(path.read_text())
        stored["schema_version"] = SCHEMA_VERSION - 1
        path.write_text(json.dumps(stored))
        with pytest.raises(NotQualifiedError, match="schema"):
            assert_qualified_for_live(path)

    def test_tightening_a_threshold_invalidates_an_old_pass(self, tmp_path, monkeypatch):
        # The property that makes raising a standard meaningful: a record that
        # qualified under a 20% drawdown limit must not still unlock live
        # trading after the limit becomes 10%.
        path = tmp_path / "qualification.json"
        save_qualification(evaluate(record(max_drawdown_pct=0.18)), path)
        assert_qualified_for_live(path)

        from src.risk import paper_qualification

        tightened = tuple(
            dataclasses.replace(c, threshold=0.10) if c.name == "max_drawdown_pct" else c
            for c in paper_qualification.CRITERIA
        )
        monkeypatch.setattr(paper_qualification, "CRITERIA", tightened)
        with pytest.raises(NotQualifiedError, match="no longer meets"):
            paper_qualification.assert_qualified_for_live(path)


class TestNoConfigurationCanBypassIt:
    """INV-010 — no flag, variable or API call skips the gate."""

    def test_the_live_executor_asserts_qualification(self):
        import inspect

        from src.execution.live import LiveExecutor

        source = inspect.getsource(LiveExecutor.__init__)
        assert "assert_qualified_for_live()" in source

    def test_it_is_asserted_before_any_state_is_built(self):
        import inspect

        from src.execution.live import LiveExecutor

        source = inspect.getsource(LiveExecutor.__init__)
        assert source.index("assert_qualified_for_live()") < source.index("self._storage = storage")

    def test_the_trading_mode_check_alone_is_not_the_gate(self):
        # TRADING_MODE=live says what the operator wants. The qualification
        # says whether the evidence earns it. Both, in that order.
        import inspect

        from src.execution.live import LiveExecutor

        source = inspect.getsource(LiveExecutor.__init__)
        assert source.index("TradingMode.LIVE") < source.index("assert_qualified_for_live()")

    def test_the_gate_takes_no_override_parameter(self):
        # A `force=True` or `skip_qualification` argument is exactly the thing
        # that gets passed once "just for this deployment".
        import inspect

        from src.risk.paper_qualification import assert_qualified_for_live as gate

        params = inspect.signature(gate).parameters
        assert set(params) == {"path"}

    def test_no_environment_variable_short_circuits_it(self):
        import inspect

        from src.risk import paper_qualification

        source = inspect.getsource(paper_qualification)
        assert "os.environ" not in source
        assert "getenv" not in source
