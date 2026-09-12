"""
RES-006 — performance budgets that can fail, and a check that measures.

A latency regression is silent. Nothing errors, no alert fires, and the only
symptom is that a decision which used to land inside the bar sometimes does
not. By the time that is visible as missed fills it has been there for several
releases.

Two halves here. The first asserts the declared budgets are coherent — every
operation in the trading path has one, percentiles are ordered, the tolerance
is a real fraction. The second drives `check()` with synthetic measurements
and requires it to catch the regressions it claims to, including the ones that
are easy to get wrong: a tail that moves while the median does not, an error
rate that creeps up, an operation nobody declared a budget for.

Deliberately not here: wall-clock measurement of the real pipeline. That
belongs in the nightly workflow, where the machine is not also running a test
suite. A timing assertion inside the unit suite measures the runner.
"""

from __future__ import annotations

import json

import pytest

from src.diagnostics.performance_baseline import (
    PERCENTILES,
    Baseline,
    BaselineError,
    Measurement,
    check,
    check_all,
    load_baselines,
    tolerance,
)

# The operations that sit in the path between a bar arriving and an order
# being placed. Named here rather than derived from the file, so deleting a
# budget fails rather than shrinking the check.
TRADING_PATH = (
    "feature_generation",
    "signal_generation",
    "risk_check",
    "order_placement",
)


@pytest.fixture(scope="module")
def baselines() -> dict[str, Baseline]:
    return load_baselines()


class TestTheDeclaredBudgets:
    def test_the_file_loads(self, baselines):
        assert baselines

    @pytest.mark.parametrize("operation", TRADING_PATH)
    def test_every_trading_path_operation_has_a_budget(self, operation, baselines):
        assert operation in baselines

    @pytest.mark.parametrize("operation", TRADING_PATH)
    def test_percentiles_are_ordered(self, operation, baselines):
        b = baselines[operation]
        assert b.p50_ms <= b.p95_ms <= b.p99_ms

    def test_every_budget_is_justified(self, baselines):
        # A number with no reasoning gets raised the first time it fails.
        for b in baselines.values():
            assert len(b.note) > 30, f"{b.operation} has no rationale"

    def test_the_trading_path_fits_inside_a_bar(self, baselines):
        # The budgets are chosen from what the loop needs: the fastest
        # timeframe is 1m, and the p99 sum has to leave room for everything
        # else. If this ever fails, the budgets stopped being derived from
        # the requirement and became a record of what the code happens to do.
        total_p99 = sum(baselines[op].p99_ms for op in TRADING_PATH)
        assert total_p99 < 60_000 * 0.1, f"p99 path sums to {total_p99}ms"

    def test_the_risk_check_is_the_cheapest_gate(self, baselines):
        # It sits between every signal and every order. Expensive risk checks
        # are how somebody ends up "temporarily" bypassing them.
        assert baselines["risk_check"].p99_ms < baselines["signal_generation"].p99_ms

    def test_only_order_placement_tolerates_errors(self, baselines):
        # Venue flakiness is real; a feature pipeline that errors is a bug.
        for name, b in baselines.items():
            if name == "order_placement":
                assert 0 < b.max_error_rate <= 0.05
            else:
                assert b.max_error_rate == 0.0

    def test_the_tolerance_is_a_sensible_fraction(self):
        value = tolerance()
        # Wide enough to survive a noisy shared runner, narrow enough that an
        # accidental O(n^2) cannot hide inside it.
        assert 0.05 <= value <= 0.5


class TestTheCheckCatchesRegressions:
    def _measurement(self, operation="risk_check", ms=1.0, n=100, **kwargs) -> Measurement:
        return Measurement(operation=operation, durations_ms=[ms] * n, **kwargs)

    def test_a_fast_run_passes(self, baselines):
        assert check(self._measurement(ms=1.0), baselines) == []

    def test_a_slow_median_is_caught(self, baselines):
        assert check(self._measurement(ms=50.0), baselines)

    def test_a_slow_tail_alone_is_caught(self, baselines):
        # The regression a mean would hide entirely: 99 fast samples and one
        # that takes a second.
        durations = [1.0] * 99 + [1000.0]
        found = check(Measurement("risk_check", durations), baselines)
        assert any(r.metric == "p99_ms" for r in found)

    def test_the_median_is_not_dragged_by_one_outlier(self, baselines):
        durations = [1.0] * 99 + [1000.0]
        found = check(Measurement("risk_check", durations), baselines)
        assert not any(r.metric == "p50_ms" for r in found)

    def test_a_regression_inside_the_tolerance_passes(self, baselines):
        budget = baselines["risk_check"].p50_ms
        found = check(self._measurement(ms=budget * (1 + tolerance() * 0.5)), baselines)
        assert not any(r.metric == "p50_ms" for r in found)

    def test_a_regression_beyond_the_tolerance_fails(self, baselines):
        budget = baselines["risk_check"].p99_ms
        assert check(self._measurement(ms=budget * (1 + tolerance() + 0.1)), baselines)

    def test_an_error_rate_above_budget_is_caught(self, baselines):
        found = check(self._measurement(ms=1.0, n=90, errors=10), baselines)
        assert any(r.metric == "error_rate" for r in found)

    def test_memory_growth_is_caught(self, baselines):
        found = check(self._measurement(ms=1.0, peak_memory_mb=10_000.0), baselines)
        assert any(r.metric == "peak_memory_mb" for r in found)

    def test_a_regression_names_the_operation_and_the_numbers(self, baselines):
        found = check(self._measurement(ms=500.0), baselines)
        rendered = str(found[0])
        assert "risk_check" in rendered
        assert "exceeds budget" in rendered


class TestSilenceIsNotAPass:
    def test_an_undeclared_operation_raises(self, baselines):
        # Measuring something with no budget is not a pass; it means somebody
        # added a hot path and nobody decided what it may cost.
        with pytest.raises(BaselineError, match="no declared baseline"):
            check(Measurement("brand_new_hot_path", [1.0]), baselines)

    def test_an_empty_measurement_raises(self, baselines):
        with pytest.raises(BaselineError, match="no samples"):
            check(Measurement("risk_check", []), baselines)

    def test_a_malformed_baseline_file_raises(self, tmp_path):
        path = tmp_path / "baselines.json"
        path.write_text("{not json")
        with pytest.raises(BaselineError, match="malformed"):
            load_baselines(path)

    def test_a_baseline_file_with_no_operations_raises(self, tmp_path):
        path = tmp_path / "baselines.json"
        path.write_text(json.dumps({"version": 1, "operations": {}}))
        with pytest.raises(BaselineError, match="no operations"):
            load_baselines(path)

    def test_an_incomplete_baseline_raises(self, tmp_path):
        path = tmp_path / "baselines.json"
        path.write_text(json.dumps({"operations": {"x": {"p50_ms": 1.0}}}))
        with pytest.raises(BaselineError):
            load_baselines(path)

    def test_out_of_order_percentiles_raise(self, tmp_path):
        # A typo that would silently make the p99 gate looser than the p50 one.
        path = tmp_path / "baselines.json"
        path.write_text(
            json.dumps(
                {
                    "operations": {
                        "x": {
                            "p50_ms": 100.0,
                            "p95_ms": 10.0,
                            "p99_ms": 1.0,
                            "max_error_rate": 0.0,
                            "max_memory_mb": 1.0,
                        }
                    }
                }
            )
        )
        with pytest.raises(BaselineError, match="not ordered"):
            load_baselines(path)


class TestTheBatchForm:
    def test_it_aggregates_across_operations(self, baselines):
        found = check_all(
            [
                Measurement("risk_check", [1.0] * 10),
                Measurement("feature_generation", [5_000.0] * 10),
            ]
        )
        assert {r.operation for r in found} == {"feature_generation"}

    def test_every_declared_percentile_is_checked(self, baselines):
        # Not an implementation detail: dropping p99 from the loop would make
        # the tail invisible again, which is the entire failure mode.
        assert PERCENTILES == (50, 95, 99)
