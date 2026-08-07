"""
Tests for the portfolio-agreement size ceiling.

The property that matters most is the one that is easy to lose in a later
refactor: this scalar may only ever *shrink*. A portfolio agreeing with the
incumbent must be worth nothing in size, because a cluster of correlated
strategies would otherwise bid its own position up exactly when its members
are most likely to be wrong together.
"""

from __future__ import annotations

import pytest

from src.engine.strategy_portfolio import (
    PortfolioInputs,
    StrategyPortfolioRunner,
)
from src.risk.conflict_resolver import ConflictResolution
from src.risk.portfolio_agreement import (
    apply_portfolio_agreement,
    portfolio_agreement_scalar,
)
from src.strategies.registry import Signal, StrategyRegistry


class _Eval:
    """Structural stand-in for a PortfolioEvaluation."""

    def __init__(self, direction: int, conflict: bool, voters: int, ratio: float = 0.8) -> None:
        self.direction = direction
        self.conflict = conflict
        self.voting_ids = tuple(f"s{i}" for i in range(voters))
        self.resolution = ConflictResolution(
            direction=direction, weight=0.5, conflict=conflict, agreement_ratio=ratio
        )


def test_no_evaluation_means_no_reduction() -> None:
    assert portfolio_agreement_scalar(None, 1) == 1.0


def test_flat_trade_has_no_ceiling_to_apply() -> None:
    assert portfolio_agreement_scalar(_Eval(-1, False, 3), 0) == 1.0


def test_agreement_is_worth_nothing() -> None:
    # The central asymmetry: agreement never grows a position.
    assert portfolio_agreement_scalar(_Eval(1, False, 4), 1) == 1.0
    assert portfolio_agreement_scalar(_Eval(-1, False, 4), -1) == 1.0


def test_opposition_shrinks_hardest() -> None:
    opposed = portfolio_agreement_scalar(_Eval(-1, False, 3), 1)
    conflicted = portfolio_agreement_scalar(_Eval(0, True, 3), 1)
    assert 0.0 < opposed < conflicted < 1.0


def test_conflicted_portfolio_shrinks() -> None:
    assert portfolio_agreement_scalar(_Eval(0, True, 3), 1) < 1.0


def test_a_single_voter_is_not_a_consensus() -> None:
    # One dissenting family is an opinion, not evidence enough to resize.
    assert portfolio_agreement_scalar(_Eval(-1, False, 1), 1) == 1.0
    assert portfolio_agreement_scalar(_Eval(0, True, 1), 1) == 1.0


def test_all_abstaining_never_reads_as_dissent() -> None:
    # Every unwired feed would otherwise be a silent permanent size cut.
    assert portfolio_agreement_scalar(_Eval(0, False, 0), 1) == 1.0


def test_opposition_never_vetoes_outright() -> None:
    # A veto belongs to the risk gates, which can block; a sizing scalar
    # must leave a tradeable position behind.
    assert portfolio_agreement_scalar(_Eval(-1, False, 5), 1) > 0.0


@pytest.mark.parametrize("direction", [1, -1])
@pytest.mark.parametrize("portfolio_dir", [-1, 0, 1])
@pytest.mark.parametrize("conflict", [True, False])
def test_scalar_is_always_shrink_only(direction: int, portfolio_dir: int, conflict: bool) -> None:
    scalar = portfolio_agreement_scalar(_Eval(portfolio_dir, conflict, 3), direction)
    assert 0.0 < scalar <= 1.0


def test_apply_is_multiplicative() -> None:
    assert apply_portfolio_agreement(0.8, 0.5) == pytest.approx(0.4)


def test_apply_can_only_reduce() -> None:
    assert apply_portfolio_agreement(0.8, 1.0) == pytest.approx(0.8)
    assert apply_portfolio_agreement(0.8, 0.4) < 0.8


@pytest.mark.parametrize(
    ("base", "agreement"),
    [(1.5, 0.5), (0.5, 1.5), (-0.1, 0.5), (0.5, -0.1)],
)
def test_apply_rejects_out_of_range_scalars(base: float, agreement: float) -> None:
    # Rejected rather than clamped: a scalar above 1.0 means a caller
    # computed something that grows a position, and clamping would hide
    # that behind correct-looking sizing.
    with pytest.raises(ValueError):
        apply_portfolio_agreement(base, agreement)


class _Stub:
    def __init__(self, sid: str, signal: Signal) -> None:
        self.strategy_id = sid
        self._signal = signal

    def generate_signal(self, bar: object) -> Signal:
        return self._signal

    def required_capital_fraction(self) -> float:
        return 0.3


def test_real_evaluation_satisfies_the_structural_view() -> None:
    # The Protocol exists to keep risk from importing the engine; this pins
    # that the concrete PortfolioEvaluation still fits through it.
    from src.engine.strategy_portfolio import build_signal_engine_context

    reg = StrategyRegistry()
    reg.register(_Stub("a", Signal(-1, 0.9, 0.9)))
    reg.register(_Stub("b", Signal(-1, 0.9, 0.9)))
    runner = StrategyPortfolioRunner(
        registry=reg,
        builders={"a": build_signal_engine_context, "b": build_signal_engine_context},
    )
    evaluation = runner.evaluate(PortfolioInputs(symbol="BTC/USDT", timeframe="15m"))
    assert evaluation.direction == -1
    # Two families short; a long trade is opposed and must be shrunk.
    assert portfolio_agreement_scalar(evaluation, 1) < 1.0
    assert portfolio_agreement_scalar(evaluation, -1) == 1.0
