"""
RISK-002 — Boundary values at every risk limit are exercised.

Every gate in the stack is a comparison, and every comparison has a side the
limit itself falls on. `<` versus `<=` is one character, it never shows up in
a coverage report, and it decides whether the trade the limit exists to stop
is permitted exactly at the limit.

So each limit gets three cases: just inside, exactly on, and just outside.
Where the limit is inclusive that is documented in the test name, because the
name is what a future reader compares against when they are about to change
the operator.

Summary of what is pinned here:

| Limit                        | Comparison                    | On the limit |
|------------------------------|-------------------------------|--------------|
| max_position_size_pct        | `position_pct > max_pct`      | allowed      |
| daily_drawdown_halt_pct      | `drawdown_pct <= -threshold`  | halts        |
| consecutive_loss_halt        | `count >= threshold`          | halts        |
| capital_preservation floor   | `drawdown >= max_drawdown`    | halts        |
| slippage veto margin         | `net_edge_bps <= 0`           | halts        |
| exchange stress halt         | `score > halt_threshold`      | allowed      |
| exchange stress reduce       | `score > reduce_threshold`    | no warning   |
| whale sell threshold         | `ratio < sell_threshold`      | allowed      |
| paper_trading_days_minimum   | `days < min_days`             | allowed      |
"""

from __future__ import annotations

import pytest

from src.config import TradingMode
from src.risk.capital_preservation_floor import CapitalPreservationFloor
from src.risk.gates import (
    GateStatus,
    check_consecutive_losses,
    check_daily_drawdown,
    check_exchange_stress,
    check_paper_minimum_days,
    check_position_size,
    check_regime_gate,
    check_slippage_veto,
    check_whale_activity,
)
from src.risk.slippage import SlippageEstimate

#: A hair, in the units of whatever is being nudged. Small enough to be
#: unambiguously "the same value plus epsilon", large enough to survive
#: float64 rounding at the magnitudes used here.
EPS = 1e-6


def _slippage(total_bps: float) -> SlippageEstimate:
    return SlippageEstimate(
        symbol="BTC/USDT",
        qty=0.02,
        notional_usd=1_000.0,
        adv_20d=2.0,
        spread_bps=1.0,
        impact_bps=total_bps - 1.0,
        total_slippage_bps=total_bps,
        total_cost_usd=total_bps / 10_000.0 * 1_000.0,
        participation_rate=0.01,
    )


class TestMaxPositionSize:
    """`position_pct > max_pct` — the limit itself is allowed."""

    def test_just_inside_passes(self, risk_cfg):
        capital = 100_000.0
        notional = capital * (risk_cfg.max_position_size_pct - 0.001) / 100.0
        assert check_position_size(notional, capital, risk_cfg).passed

    def test_exactly_on_the_limit_is_allowed(self, risk_cfg):
        capital = 100_000.0
        notional = capital * risk_cfg.max_position_size_pct / 100.0
        assert check_position_size(notional, capital, risk_cfg).passed

    def test_just_outside_halts(self, risk_cfg):
        capital = 100_000.0
        notional = capital * (risk_cfg.max_position_size_pct + 0.001) / 100.0
        result = check_position_size(notional, capital, risk_cfg)
        assert not result.passed
        assert result.status is GateStatus.HALT_POSITION_SIZE


class TestDailyDrawdown:
    """`drawdown_pct <= -threshold` — the limit itself halts."""

    def test_just_inside_passes(self, risk_cfg):
        equity = 100_000.0
        pnl = -equity * (risk_cfg.daily_drawdown_halt_pct - 0.001) / 100.0
        assert check_daily_drawdown(pnl, equity, risk_cfg).passed

    def test_exactly_on_the_limit_halts(self, risk_cfg):
        equity = 100_000.0
        pnl = -equity * risk_cfg.daily_drawdown_halt_pct / 100.0
        result = check_daily_drawdown(pnl, equity, risk_cfg)
        assert not result.passed
        assert result.status is GateStatus.HALT_DRAWDOWN

    def test_just_outside_halts(self, risk_cfg):
        equity = 100_000.0
        pnl = -equity * (risk_cfg.daily_drawdown_halt_pct + 0.001) / 100.0
        assert not check_daily_drawdown(pnl, equity, risk_cfg).passed

    def test_a_profitable_day_passes(self, risk_cfg):
        assert check_daily_drawdown(5_000.0, 100_000.0, risk_cfg).passed

    def test_zero_equity_halts_rather_than_dividing(self, risk_cfg):
        result = check_daily_drawdown(-1.0, 0.0, risk_cfg)
        assert not result.passed
        assert result.status is GateStatus.HALT_DRAWDOWN


class TestConsecutiveLosses:
    """`count >= threshold` — the limit itself halts."""

    def test_one_below_passes(self, risk_cfg):
        assert check_consecutive_losses(risk_cfg.consecutive_loss_halt - 1, risk_cfg).passed

    def test_exactly_on_the_limit_halts(self, risk_cfg):
        result = check_consecutive_losses(risk_cfg.consecutive_loss_halt, risk_cfg)
        assert not result.passed
        assert result.status is GateStatus.HALT_CONSECUTIVE_LOSSES

    def test_above_the_limit_halts(self, risk_cfg):
        assert not check_consecutive_losses(risk_cfg.consecutive_loss_halt + 5, risk_cfg).passed

    def test_no_losses_passes(self, risk_cfg):
        assert check_consecutive_losses(0, risk_cfg).passed


class TestCapitalPreservationFloor:
    """`drawdown >= max_drawdown_pct` — the limit itself halts."""

    @staticmethod
    def _floor_at(peak: float, equity: float, limit: float) -> CapitalPreservationFloor:
        floor = CapitalPreservationFloor(max_drawdown_pct=limit)
        floor.update_equity(peak)
        floor.update_equity(equity)
        return floor

    def test_just_inside_does_not_halt(self, risk_cfg):
        limit = risk_cfg.capital_preservation_max_drawdown_pct
        peak = 100_000.0
        equity = peak * (1.0 - limit) + 1.0
        assert not self._floor_at(peak, equity, limit).is_halted

    def test_exactly_on_the_limit_halts(self, risk_cfg):
        limit = risk_cfg.capital_preservation_max_drawdown_pct
        peak = 100_000.0
        equity = peak * (1.0 - limit)
        assert self._floor_at(peak, equity, limit).is_halted

    def test_just_outside_halts(self, risk_cfg):
        limit = risk_cfg.capital_preservation_max_drawdown_pct
        peak = 100_000.0
        equity = peak * (1.0 - limit) - 1.0
        assert self._floor_at(peak, equity, limit).is_halted


class TestSlippageVeto:
    """`net_edge_bps <= 0` — a net edge of exactly zero halts."""

    def test_a_hair_of_net_edge_passes(self, risk_cfg):
        margin = risk_cfg.slippage_veto_margin_bps
        edge = 10.0
        assert check_slippage_veto(edge, _slippage(edge - margin - EPS), risk_cfg).passed

    def test_exactly_break_even_halts(self, risk_cfg):
        margin = risk_cfg.slippage_veto_margin_bps
        edge = 10.0
        result = check_slippage_veto(edge, _slippage(edge - margin), risk_cfg)
        assert not result.passed
        assert result.status is GateStatus.HALT_NEGATIVE_EV

    def test_a_hair_of_net_loss_halts(self, risk_cfg):
        margin = risk_cfg.slippage_veto_margin_bps
        edge = 10.0
        assert not check_slippage_veto(edge, _slippage(edge - margin + EPS), risk_cfg).passed

    def test_no_estimate_fails_open(self, risk_cfg):
        assert check_slippage_veto(-1_000.0, None, risk_cfg).passed


class TestExchangeStress:
    """`score > halt_threshold` — the threshold itself is allowed."""

    def test_exactly_on_the_halt_threshold_is_allowed(self):
        assert check_exchange_stress(0.75, stress_halt_threshold=0.75).passed

    def test_just_above_the_halt_threshold_halts(self):
        result = check_exchange_stress(0.75 + EPS, stress_halt_threshold=0.75)
        assert not result.passed
        assert result.status is GateStatus.HALT_EXCHANGE_STRESS

    def test_exactly_on_the_reduce_threshold_emits_no_warning(self):
        result = check_exchange_stress(0.50, stress_reduce_threshold=0.50)
        assert result.passed
        assert result.details["stress_action"] == "none"

    def test_just_above_the_reduce_threshold_suggests_reducing(self):
        result = check_exchange_stress(0.50 + EPS, stress_reduce_threshold=0.50)
        assert result.passed
        assert result.details["stress_action"] == "reduce_suggested"

    def test_no_data_fails_open(self):
        assert check_exchange_stress(None).passed


class TestWhaleActivity:
    """`ratio < sell_threshold` — the threshold itself is allowed."""

    def test_exactly_on_the_threshold_is_allowed(self):
        result = check_whale_activity(0.85, sell_threshold=0.85)
        assert result.passed
        assert result.size_scalar == 1.0

    def test_just_below_the_threshold_triggers(self):
        result = check_whale_activity(0.85 - EPS, sell_threshold=0.85, advisory=True)
        assert result.status is GateStatus.REDUCE_WHALE_ACTIVITY
        assert result.size_scalar < 1.0

    def test_the_advisory_scalar_is_applied_not_merely_reported(self):
        result = check_whale_activity(0.1, advisory=True, advisory_scalar=0.25)
        assert result.size_scalar == 0.25
        assert result.details["size_scalar"] == 0.25

    def test_with_advisory_off_the_same_input_vetoes(self):
        # The posture difference is a trading-policy decision, so it is pinned
        # rather than left to whichever default happens to be in force.
        assert not check_whale_activity(0.1, advisory=False).passed

    def test_no_data_fails_open(self):
        assert check_whale_activity(None).passed


class TestPaperMinimumDays:
    """`days < min_days` — the minimum itself is enough."""

    def test_one_day_short_halts(self):
        result = check_paper_minimum_days(29, settings_override=30)
        assert not result.passed
        assert result.status is GateStatus.HALT_PAPER_ONLY
        assert result.details["days_remaining"] == 1

    def test_exactly_the_minimum_passes(self):
        assert check_paper_minimum_days(30, settings_override=30).passed

    def test_more_than_the_minimum_passes(self):
        assert check_paper_minimum_days(31, settings_override=30).passed


class TestRegimeGate:
    """Not a numeric limit, but the same question: which value is the edge?"""

    @pytest.mark.parametrize("state", [0, 1])
    def test_non_volatile_regimes_pass(self, state):
        assert check_regime_gate(state).passed

    def test_the_volatile_state_halts(self):
        result = check_regime_gate(2)
        assert not result.passed
        assert result.status is GateStatus.HALT_REGIME

    def test_an_unknown_state_passes_and_says_so(self):
        # Fails open, and the detail field is how an operator finds out. If
        # this is ever changed to fail closed, this test is the record of the
        # previous decision.
        result = check_regime_gate(99)
        assert result.passed
        assert result.details["regime_name"] == "unknown"


class TestLiveGateIsSkippedInPaper:
    def test_paper_skips_the_model_quality_gate(self):
        from src.risk.gates import check_live_gate

        result = check_live_gate(TradingMode.PAPER, False, False)
        assert result.passed
        assert result.details["gate_check"] == "skipped_paper"

    @pytest.mark.parametrize(("direction", "meta"), [(False, False), (False, True), (True, False)])
    def test_live_requires_both_models(self, direction, meta):
        from src.risk.gates import check_live_gate

        result = check_live_gate(TradingMode.LIVE, direction, meta)
        assert not result.passed
        assert result.status is GateStatus.HALT_LIVE_GATE

    def test_live_passes_when_both_models_pass(self):
        from src.risk.gates import check_live_gate

        assert check_live_gate(TradingMode.LIVE, True, True).passed
