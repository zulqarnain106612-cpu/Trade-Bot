"""
RISK-005 — an optimizer never touches a live risk parameter directly.

A self-tuning system is an optimizer pointed at the parameters that decide how
much money is at stake. Left unguarded it converges on exactly the settings
that maximise the metric it was given, which for any backtest-derived metric
means maximum leverage, minimum stop distance, and a strategy that has learned
the shape of the last three months.

So the path from a proposal to a live risk parameter is deliberately long:
bounds, offline evaluation, an out-of-sample check, the risk tests, shadow
mode, paper, and an operator. These tests hold the two ends of it — the bound
is checked before anything else, and nothing reaches a live setting except
through the override layer that the gauntlet feeds.

The interesting assertion is not that the gate rejects a bad proposal. It is
that the gate rejects an *excellent* one that is out of bounds, before it
looks at how good it is: a proposal good enough to be tempting is the one a
score-based gate lets through.
"""

from __future__ import annotations

import inspect

import pytest

from src.tuning.gate import GateDecision, PromotionGate


class FakeComparison:
    def __init__(self, metric_name: str, significant_regression: bool = False) -> None:
        self.metric_name = metric_name
        self.significant_regression = significant_regression


class FakeEvaluation:
    """Minimal stand-in: the gate only reads these four things."""

    def __init__(
        self,
        challenger_value: float,
        improved: bool = True,
        regressions: tuple[str, ...] = (),
        known_metrics: tuple[str, ...] = ("sharpe",),
    ) -> None:
        self.challenger_value = challenger_value
        self._improved = improved
        self._known = known_metrics
        self.comparisons = [FakeComparison(m, m in regressions) for m in known_metrics]

    @property
    def any_significant_regression(self) -> bool:
        return any(c.significant_regression for c in self.comparisons)

    def improved(self, metric: str) -> bool:
        if metric not in self._known:
            raise KeyError(metric)
        return self._improved


class FakeParameter:
    def __init__(self, floor: float, ceiling: float) -> None:
        self.floor = floor
        self.ceiling = ceiling

    def in_bounds(self, value: float) -> bool:
        return self.floor <= value <= self.ceiling


@pytest.fixture
def gate() -> PromotionGate:
    return PromotionGate()


@pytest.fixture
def param() -> FakeParameter:
    return FakeParameter(floor=0.01, ceiling=0.05)


class TestBoundsComeFirst:
    def test_an_out_of_bounds_proposal_is_refused(self, gate, param):
        decision = gate.decide(param, FakeEvaluation(challenger_value=0.9), "sharpe")
        assert decision.accepted is False
        assert "outside bounds" in decision.reasons[0]

    def test_an_excellent_out_of_bounds_proposal_is_still_refused(self, gate, param):
        # The one that matters. An optimizer maximising a backtest metric will
        # propose maximum leverage and be *right* about the metric; a gate
        # that weighs the improvement against the bound lets it through.
        decision = gate.decide(
            param, FakeEvaluation(challenger_value=10.0, improved=True), "sharpe"
        )
        assert decision.accepted is False

    def test_the_bound_check_short_circuits(self, gate, param):
        # Nothing downstream even runs -- the reasons carry only the bound.
        decision = gate.decide(param, FakeEvaluation(challenger_value=0.9), "sharpe")
        assert len(decision.reasons) == 1

    @pytest.mark.parametrize("value", [0.01, 0.05])
    def test_the_bounds_are_inclusive(self, gate, param, value):
        decision = gate.decide(param, FakeEvaluation(challenger_value=value), "sharpe")
        assert decision.accepted is True


class TestImprovementIsNotEnough:
    def test_a_regression_anywhere_blocks_promotion(self, gate, param):
        decision = gate.decide(
            param,
            FakeEvaluation(
                challenger_value=0.03,
                improved=True,
                regressions=("max_drawdown",),
                known_metrics=("sharpe", "max_drawdown"),
            ),
            "sharpe",
        )
        assert decision.accepted is False
        assert any("regression" in r for r in decision.reasons)

    def test_no_improvement_blocks_promotion(self, gate, param):
        decision = gate.decide(
            param, FakeEvaluation(challenger_value=0.03, improved=False), "sharpe"
        )
        assert decision.accepted is False

    def test_a_missing_primary_metric_blocks_promotion(self, gate, param):
        # Not "assume it improved". An evaluation that never measured the
        # metric the decision is about has decided nothing.
        decision = gate.decide(
            param, FakeEvaluation(challenger_value=0.03, known_metrics=("winrate",)), "sharpe"
        )
        assert decision.accepted is False
        assert any("not present" in r for r in decision.reasons)

    def test_acceptance_is_explained(self, gate, param):
        decision = gate.decide(param, FakeEvaluation(challenger_value=0.03), "sharpe")
        assert decision.accepted is True
        assert decision.reasons  # an unexplained yes is as bad as an unexplained no


class TestTheDecisionIsImmutable:
    def test_it_cannot_be_edited_after_the_fact(self, gate, param):
        decision = gate.decide(param, FakeEvaluation(challenger_value=0.9), "sharpe")
        with pytest.raises((AttributeError, TypeError)):
            decision.accepted = True  # type: ignore[misc]

    def test_the_reasons_are_a_tuple(self):
        assert isinstance(GateDecision(accepted=True, reasons=("x",)).reasons, tuple)


class TestTheLiveOverrideLayerIsTheOnlyDoor:
    """
    Structural: the optimizer must not reach into settings objects directly.
    Tested by reading the source, because the violation is an import that
    looks entirely reasonable in review.
    """

    def test_the_override_layer_reads_rather_than_writes_settings(self):
        from src.tuning import live_overrides

        source = inspect.getsource(live_overrides)
        # It composes effective settings from a base plus overrides; it must
        # not mutate the process-wide Settings object, which would make the
        # change invisible and permanent.
        assert "get_settings().risk." not in source.replace(" ", "")

    def test_the_optimizer_does_not_import_the_executors(self):
        # A proposal reaching an executor directly would bypass every stage
        # between it and live money.
        from src.tuning import gate as tuning_gate

        source = inspect.getsource(tuning_gate)
        for forbidden in ("src.execution.live", "LiveExecutor", "place_order"):
            assert forbidden not in source

    def test_the_gate_has_no_force_parameter(self):
        params = inspect.signature(PromotionGate.decide).parameters
        assert set(params) == {"self", "param", "evaluation", "primary_metric"}

    def test_promotion_requires_a_parameter_with_declared_bounds(self):
        # An unbounded tunable is an optimizer with no ceiling. The signature
        # makes bounds mandatory rather than optional.
        source = inspect.getsource(PromotionGate.decide)
        assert "param.in_bounds" in source
