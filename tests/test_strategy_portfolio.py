"""
Tests for the strategy-portfolio evaluation layer.

The behaviours worth pinning are the ones whose absence let the portfolio sit
unpolled: that every registered strategy produces a *verdict* (so silence is
visible), that regime_fit == 0 is a hard gate rather than a small weight, and
that a raising strategy cannot take the evaluation — and therefore the tick —
down with it.
"""

from __future__ import annotations

import pandas as pd
import pytest

from src.engine.strategy_portfolio import (
    NO_CONTEXT,
    PortfolioInputs,
    StrategyPortfolioRunner,
    VerdictStatus,
    build_basis_trade_context,
    build_breakout_context,
    build_cross_exchange_context,
    build_funding_context,
    build_signal_engine_context,
    build_xsec_momentum_context,
    default_context_builders,
    get_portfolio_runner,
    reset_portfolio_runner,
)
from src.strategies.registry import Signal, StrategyRegistry


class _StubStrategy:
    """Minimal StrategyProtocol conformer returning a canned Signal."""

    def __init__(self, strategy_id: str, signal: Signal, *, raises: bool = False) -> None:
        self.strategy_id = strategy_id
        self._signal = signal
        self._raises = raises
        self.calls = 0

    def generate_signal(self, bar: object) -> Signal:
        self.calls += 1
        if self._raises:
            raise RuntimeError("boom")
        return self._signal

    def required_capital_fraction(self) -> float:
        return 0.5


def _registry(*strategies: _StubStrategy) -> StrategyRegistry:
    reg = StrategyRegistry()
    for s in strategies:
        reg.register(s)
    return reg


def _runner(reg: StrategyRegistry, ids: list[str]) -> StrategyPortfolioRunner:
    return StrategyPortfolioRunner(
        registry=reg,
        builders={sid: build_signal_engine_context for sid in ids},
    )


_INPUTS = PortfolioInputs(symbol="BTC/USDT", timeframe="15m")


def _bars(n: int, *, breakout: bool = False) -> PortfolioInputs:
    closes = [100.0 + i * 0.01 for i in range(n)]
    highs = [c + 1.0 for c in closes]
    lows = [c - 1.0 for c in closes]
    volumes = [10.0] * n
    if breakout:
        closes[-1] = max(highs[:-1]) + 5.0
        highs[-1] = closes[-1] + 0.5
        volumes[-1] = 100.0
    return PortfolioInputs(
        symbol="BTC/USDT",
        timeframe="15m",
        highs=highs,
        lows=lows,
        closes=closes,
        volumes=volumes,
    )


# ---------------------------------------------------------------- builders


def test_signal_engine_builder_returns_sentinel_not_none() -> None:
    # NO_CONTEXT must not be None: None is the abstain answer, and returning
    # it would mute the incumbent strategy entirely.
    assert build_signal_engine_context(_INPUTS) is NO_CONTEXT
    assert NO_CONTEXT is not None


def test_breakout_builder_abstains_without_bars() -> None:
    assert build_breakout_context(_INPUTS) is None


def test_breakout_builder_abstains_on_short_history() -> None:
    assert build_breakout_context(_bars(10)) is None


def test_breakout_builder_abstains_on_ragged_series() -> None:
    inputs = _bars(100)
    ragged = PortfolioInputs(
        symbol=inputs.symbol,
        timeframe=inputs.timeframe,
        highs=inputs.highs,
        lows=list(inputs.lows or [])[:-1],
        closes=inputs.closes,
        volumes=inputs.volumes,
    )
    assert build_breakout_context(ragged) is None


def test_breakout_builder_produces_context() -> None:
    ctx = build_breakout_context(_bars(100))
    assert ctx is not None
    assert isinstance(ctx.close, pd.Series)  # type: ignore[attr-defined]
    assert len(ctx.close) == 100  # type: ignore[attr-defined]


def test_funding_builder_abstains_without_history() -> None:
    assert build_funding_context(_INPUTS) is None
    assert (
        build_funding_context(
            PortfolioInputs(
                symbol="BTC/USDT",
                timeframe="15m",
                funding_rate_pct=0.01,
                funding_history_pct=[0.01] * 3,
            )
        )
        is None
    )


def test_funding_builder_abstains_on_zero_variance() -> None:
    # A constant history makes the z-score undefined; emitting 0.0 would be
    # indistinguishable from a genuinely unremarkable rate.
    assert (
        build_funding_context(
            PortfolioInputs(
                symbol="BTC/USDT",
                timeframe="15m",
                funding_rate_pct=0.01,
                funding_history_pct=[0.01] * 40,
            )
        )
        is None
    )


def test_funding_builder_computes_zscore() -> None:
    history = [0.0] * 39 + [0.05]
    ctx = build_funding_context(
        PortfolioInputs(
            symbol="BTC/USDT",
            timeframe="15m",
            funding_rate_pct=0.05,
            funding_history_pct=history,
        )
    )
    assert ctx is not None
    assert ctx.funding_zscore > 1.0  # type: ignore[attr-defined]


def test_basis_builder_requires_both_legs_and_positive_prices() -> None:
    assert build_basis_trade_context(_INPUTS) is None
    assert (
        build_basis_trade_context(
            PortfolioInputs(symbol="B", timeframe="15m", spot_price=0.0, perp_price=10.0)
        )
        is None
    )
    ctx = build_basis_trade_context(
        PortfolioInputs(symbol="B", timeframe="15m", spot_price=100.0, perp_price=101.0)
    )
    assert ctx is not None
    assert ctx.perp_price == pytest.approx(101.0)  # type: ignore[attr-defined]


def test_default_builders_cover_the_families_with_feeds() -> None:
    builders = default_context_builders()
    assert "signal_engine_v1" in builders
    assert "breakout_volume_v1" in builders
    assert "funding_carry_v1" in builders
    assert "cross_exchange_arb_v1" in builders
    assert "xsec_momentum_v1" in builders


# ---------------------------------------------------------------- runner


def test_empty_registry_yields_no_direction() -> None:
    ev = StrategyPortfolioRunner(registry=StrategyRegistry()).evaluate(_INPUTS)
    assert ev.verdicts == ()
    assert ev.direction == 0
    assert ev.conviction == 0.0


def test_every_registered_strategy_gets_a_verdict() -> None:
    a = _StubStrategy("a", Signal(1, 0.9, 0.9))
    b = _StubStrategy("b", Signal(1, 0.8, 0.9))
    ev = _runner(_registry(a, b), ["a", "b"]).evaluate(_INPUTS)
    assert {v.strategy_id for v in ev.verdicts} == {"a", "b"}
    assert ev.direction == 1
    assert ev.conviction > 0.0
    assert ev.voting_ids == ("a", "b")


def test_strategy_without_builder_abstains_and_is_not_polled() -> None:
    a = _StubStrategy("a", Signal(1, 0.9, 0.9))
    ev = StrategyPortfolioRunner(registry=_registry(a), builders={}).evaluate(_INPUTS)
    (verdict,) = ev.verdicts
    assert verdict.status is VerdictStatus.ABSTAINED
    assert verdict.reason == "no_context_builder"
    assert a.calls == 0


def test_builder_returning_none_abstains() -> None:
    a = _StubStrategy("a", Signal(1, 0.9, 0.9))
    runner = StrategyPortfolioRunner(registry=_registry(a), builders={"a": lambda _i: None})
    (verdict,) = runner.evaluate(_INPUTS).verdicts
    assert verdict.status is VerdictStatus.ABSTAINED
    assert verdict.reason == "insufficient_data"
    assert a.calls == 0


def test_regime_fit_zero_is_a_hard_gate_not_a_small_weight() -> None:
    gated = _StubStrategy("gated", Signal(1, 1.0, 0.0))
    other = _StubStrategy("other", Signal(-1, 0.5, 0.9))
    ev = _runner(_registry(gated, other), ["gated", "other"]).evaluate(_INPUTS)
    statuses = {v.strategy_id: v.status for v in ev.verdicts}
    assert statuses["gated"] is VerdictStatus.REGIME_GATED
    # Full confidence on the gated leg must not survive into the vote.
    assert ev.direction == -1


def test_flat_signal_is_reported_but_does_not_vote() -> None:
    flat = _StubStrategy("flat", Signal(0, 0.0, 0.7))
    ev = _runner(_registry(flat), ["flat"]).evaluate(_INPUTS)
    (verdict,) = ev.verdicts
    assert verdict.status is VerdictStatus.FLAT
    assert ev.direction == 0
    # No voters at all is unanimity, not conflict.
    assert ev.conflict is False


def test_raising_strategy_becomes_an_error_verdict() -> None:
    bad = _StubStrategy("bad", Signal(1, 0.9, 0.9), raises=True)
    good = _StubStrategy("good", Signal(1, 0.9, 0.9))
    ev = _runner(_registry(bad, good), ["bad", "good"]).evaluate(_INPUTS)
    statuses = {v.strategy_id: v.status for v in ev.verdicts}
    assert statuses["bad"] is VerdictStatus.ERROR
    assert statuses["good"] is VerdictStatus.SIGNAL
    assert ev.direction == 1


def test_raising_builder_becomes_an_error_verdict() -> None:
    def _boom(_inputs: PortfolioInputs) -> object:
        raise ValueError("no data")

    a = _StubStrategy("a", Signal(1, 0.9, 0.9))
    runner = StrategyPortfolioRunner(registry=_registry(a), builders={"a": _boom})
    (verdict,) = runner.evaluate(_INPUTS).verdicts
    assert verdict.status is VerdictStatus.ERROR
    assert "no data" in verdict.reason
    assert a.calls == 0


def test_disabled_strategies_are_not_polled() -> None:
    on = _StubStrategy("on", Signal(1, 0.9, 0.9))
    off = _StubStrategy("off", Signal(-1, 1.0, 0.9))
    ev = _runner(_registry(on, off), ["on", "off"]).evaluate(_INPUTS, enabled_ids={"on"})
    statuses = {v.strategy_id: v.status for v in ev.verdicts}
    assert statuses["off"] is VerdictStatus.DISABLED
    assert off.calls == 0
    assert ev.direction == 1


def test_conflicted_portfolio_reports_no_direction_but_keeps_raw() -> None:
    a = _StubStrategy("a", Signal(1, 0.55, 0.9))
    b = _StubStrategy("b", Signal(-1, 0.45, 0.9))
    ev = _runner(_registry(a, b), ["a", "b"]).evaluate(_INPUTS)
    assert ev.conflict is True
    assert ev.direction == 0
    assert ev.as_dict()["raw_direction"] in (-1, 0, 1)


def test_allocation_weights_bias_the_vote() -> None:
    a = _StubStrategy("a", Signal(1, 0.6, 0.9))
    b = _StubStrategy("b", Signal(-1, 0.6, 0.9))
    reg = _registry(a, b)
    ev = _runner(reg, ["a", "b"]).evaluate(_INPUTS, weights={"a": 0.9, "b": 0.1})
    assert ev.direction == 1


def test_negative_weight_is_clamped_not_treated_as_a_short() -> None:
    a = _StubStrategy("a", Signal(1, 0.9, 0.9))
    b = _StubStrategy("b", Signal(1, 0.9, 0.9))
    ev = _runner(_registry(a, b), ["a", "b"]).evaluate(_INPUTS, weights={"a": -5.0, "b": 0.5})
    assert ev.direction == 1
    assert all(v.weight >= 0.0 for v in ev.verdicts)


def test_missing_weight_falls_back_to_equal_share() -> None:
    a = _StubStrategy("a", Signal(1, 0.9, 0.9))
    b = _StubStrategy("b", Signal(1, 0.9, 0.9))
    ev = _runner(_registry(a, b), ["a", "b"]).evaluate(_INPUTS, weights={"a": 0.5})
    weights = {v.strategy_id: v.weight for v in ev.verdicts}
    assert weights["b"] == pytest.approx(0.5)


def test_zero_confidence_strategy_cannot_vote_on_allocation_alone() -> None:
    quiet = _StubStrategy("quiet", Signal(1, 0.0, 0.9))
    ev = _runner(_registry(quiet), ["quiet"]).evaluate(_INPUTS, weights={"quiet": 1.0})
    (verdict,) = ev.verdicts
    assert verdict.status is VerdictStatus.FLAT
    assert ev.direction == 0


def test_as_dict_is_serialisable_and_counts_statuses() -> None:
    a = _StubStrategy("a", Signal(1, 0.9, 0.9))
    b = _StubStrategy("b", Signal(0, 0.0, 0.5))
    ev = _runner(_registry(a, b), ["a", "b"]).evaluate(_INPUTS)
    payload = ev.as_dict()
    assert payload["status_counts"]["signal"] == 1
    assert payload["status_counts"]["flat"] == 1
    assert len(payload["verdicts"]) == 2
    assert payload["verdicts"][0]["strategy_id"] == "a"


def test_by_status_filters_verdicts() -> None:
    a = _StubStrategy("a", Signal(1, 0.9, 0.9))
    b = _StubStrategy("b", Signal(0, 0.0, 0.5))
    ev = _runner(_registry(a, b), ["a", "b"]).evaluate(_INPUTS)
    assert len(ev.by_status(VerdictStatus.SIGNAL)) == 1
    assert len(ev.by_status(VerdictStatus.FLAT)) == 1


def test_register_context_builder_retires_an_abstention() -> None:
    a = _StubStrategy("a", Signal(1, 0.9, 0.9))
    runner = StrategyPortfolioRunner(registry=_registry(a), builders={})
    assert runner.evaluate(_INPUTS).verdicts[0].status is VerdictStatus.ABSTAINED
    runner.register_context_builder("a", build_signal_engine_context)
    assert runner.evaluate(_INPUTS).verdicts[0].status is VerdictStatus.SIGNAL
    assert "a" in runner.builders


def test_runner_resolves_the_registry_lazily_per_call() -> None:
    # A runner built before bootstrap must still see strategies registered
    # afterwards, or it would hold an empty registry for the process's life.
    reset_portfolio_runner()
    try:
        runner = get_portfolio_runner()
        assert get_portfolio_runner() is runner
        assert runner.evaluate(_INPUTS) is not None
    finally:
        reset_portfolio_runner()


def test_real_breakout_strategy_votes_through_the_runner() -> None:
    from src.strategies.breakout import BreakoutStrategy

    reg = StrategyRegistry()
    reg.register(BreakoutStrategy(0.15))
    runner = StrategyPortfolioRunner(registry=reg)
    ev = runner.evaluate(_bars(100, breakout=True))
    (verdict,) = ev.verdicts
    assert verdict.status is VerdictStatus.SIGNAL
    assert verdict.signal is not None
    assert verdict.signal.direction == 1
    assert ev.direction == 1


# ------------------------------------------------- cross-exchange builder


def _venues(
    prices: dict[str, float], stamps: dict[str, float] | None = None
) -> PortfolioInputs:
    return PortfolioInputs(
        symbol="BTC/USDT",
        timeframe="15m",
        venue_prices=prices,
        venue_price_ts=stamps or {},
    )


def test_cross_exchange_builder_needs_two_venues() -> None:
    assert build_cross_exchange_context(_venues({})) is None
    assert build_cross_exchange_context(_venues({"binance": 100.0})) is None


def test_cross_exchange_builder_ignores_non_positive_quotes() -> None:
    # A zero/negative quote is a failed fetch that leaked through, not a
    # price; counting it would pair a real venue against garbage.
    assert build_cross_exchange_context(_venues({"binance": 100.0, "okx": 0.0})) is None


def test_cross_exchange_builder_pairs_two_venues() -> None:
    ctx = build_cross_exchange_context(_venues({"binance": 100.0, "okx": 101.0}))
    assert ctx is not None
    assert ctx.venue_a == "binance"  # type: ignore[attr-defined]
    assert ctx.price_b == pytest.approx(101.0)  # type: ignore[attr-defined]


def test_cross_exchange_builder_rejects_skewed_quotes() -> None:
    # Two quotes taken 10s apart cannot be differenced: the market moved
    # between them, so any spread found is latency, not arbitrage.
    inputs = _venues(
        {"binance": 100.0, "okx": 101.0},
        {"binance": 1_000.0, "okx": 1_010.0},
    )
    assert build_cross_exchange_context(inputs) is None


def test_cross_exchange_builder_accepts_near_simultaneous_quotes() -> None:
    inputs = _venues(
        {"binance": 100.0, "okx": 101.0},
        {"binance": 1_000.0, "okx": 1_000.5},
    )
    assert build_cross_exchange_context(inputs) is not None


def test_cross_exchange_builder_without_timestamps_still_builds() -> None:
    # Absent stamps means the caller cannot vouch for skew; the strategy's
    # own entry threshold remains the gate rather than blocking outright.
    assert build_cross_exchange_context(_venues({"a": 100.0, "b": 101.0})) is not None


def test_cross_exchange_arb_votes_through_the_runner() -> None:
    from src.strategies.cross_exchange_arb import CrossExchangeArbStrategy

    reg = StrategyRegistry()
    reg.register(CrossExchangeArbStrategy(0.10))
    ev = StrategyPortfolioRunner(registry=reg).evaluate(
        _venues({"binance": 101.0, "okx": 100.0}, {"binance": 5.0, "okx": 5.1})
    )
    (verdict,) = ev.verdicts
    assert verdict.status is VerdictStatus.SIGNAL
    assert verdict.signal is not None
    # venue_a richer than venue_b -> short venue_a.
    assert verdict.signal.direction == -1


# ------------------------------------------------- cross-sectional builder


def _universe(returns: dict[str, float], symbol: str = "BTC/USDT") -> PortfolioInputs:
    return PortfolioInputs(symbol=symbol, timeframe="15m", universe_returns=returns)


def test_xsec_builder_abstains_without_a_universe() -> None:
    assert build_xsec_momentum_context(_INPUTS) is None
    assert build_xsec_momentum_context(_universe({})) is None


def test_xsec_builder_abstains_when_the_target_is_absent() -> None:
    # A missing target has no percentile rank; inserting it at a neutral
    # value would place it mid-universe, where a decile strategy will never
    # notice its own asset was never measured.
    assert build_xsec_momentum_context(_universe({"ETH/USDT": 0.1, "SOL/USDT": -0.2})) is None


def test_xsec_builder_builds_when_the_target_is_ranked() -> None:
    ctx = build_xsec_momentum_context(_universe({"BTC/USDT": 0.3, "ETH/USDT": 0.1}))
    assert ctx is not None
    assert ctx.target_symbol == "BTC/USDT"  # type: ignore[attr-defined]
    assert len(ctx.trailing_returns) == 2  # type: ignore[attr-defined]


def test_xsec_universe_size_guard_belongs_to_the_strategy() -> None:
    # A too-small universe must read as the strategy declining, not as
    # missing data — the builder deliberately does not second-guess it.
    from src.strategies.xsec_momentum import CrossSectionalMomentumStrategy

    reg = StrategyRegistry()
    reg.register(CrossSectionalMomentumStrategy(0.15))
    ev = StrategyPortfolioRunner(registry=reg).evaluate(
        _universe({"BTC/USDT": 0.3, "ETH/USDT": 0.1})
    )
    (verdict,) = ev.verdicts
    assert verdict.status is VerdictStatus.REGIME_GATED


def test_xsec_momentum_votes_on_a_full_universe() -> None:
    from src.strategies.xsec_momentum import CrossSectionalMomentumStrategy

    universe = {f"S{i}/USDT": i * 0.01 for i in range(12)}
    universe["BTC/USDT"] = 5.0  # decisively the top of the cross-section
    reg = StrategyRegistry()
    reg.register(CrossSectionalMomentumStrategy(0.15))
    ev = StrategyPortfolioRunner(registry=reg).evaluate(_universe(universe))
    (verdict,) = ev.verdicts
    assert verdict.status is VerdictStatus.SIGNAL
    assert verdict.signal is not None
    assert verdict.signal.direction == 1
