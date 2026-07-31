"""
Non-finite inputs must not pass a risk gate.

IEEE-754 makes every comparison against NaN False, so an unguarded NaN
measurement does not trip a threshold — it slips past it. `<= 0.0` guards
do not catch it either. src/risk/kelly.py already closed this defect class
one layer down (VF-024/026/027/028/029/030); these tests pin it at the gate
layer, where failing open is worse because nothing downstream re-checks.
"""

from __future__ import annotations

import math

import pytest

from src.config import REGIME_VOLATILE, RiskSettings, TradingMode
from src.risk.gates import (
    GateStatus,
    check_daily_drawdown,
    check_exchange_stress,
    check_position_exit,
    check_position_size,
    check_slippage_veto,
    check_whale_activity,
    evaluate_all_gates,
)
from src.risk.slippage import SlippageEstimate


NON_FINITE = [math.nan, math.inf, -math.inf]


@pytest.fixture
def cfg():
    return RiskSettings()


def _slippage(total_bps: float) -> SlippageEstimate:
    return SlippageEstimate(
        symbol="BTC/USDT",
        qty=1.0,
        notional_usd=50_000.0,
        adv_20d=100.0,
        spread_bps=1.0,
        impact_bps=1.0,
        total_slippage_bps=total_bps,
        total_cost_usd=10.0,
        participation_rate=0.01,
    )


# ---------------------------------------------------------------------------
# Position size — the clearest case
# ---------------------------------------------------------------------------


class TestPositionSizeGate:
    @pytest.mark.parametrize("bad", NON_FINITE)
    def test_non_finite_notional_is_blocked(self, cfg, bad):
        # nan / capital -> nan, and `nan > max_pct` is False, so without an
        # explicit guard this approves a position of unknown size.
        result = check_position_size(notional_usd=bad, capital_usd=10_000.0, cfg=cfg)
        assert result.passed is False
        assert result.status is GateStatus.HALT_POSITION_SIZE

    @pytest.mark.parametrize("bad", NON_FINITE)
    def test_non_finite_capital_is_blocked(self, cfg, bad):
        # `capital_usd <= 0.0` is also False for NaN, so the existing guard
        # does not catch this either.
        result = check_position_size(notional_usd=100.0, capital_usd=bad, cfg=cfg)
        assert result.passed is False
        assert result.status is GateStatus.HALT_POSITION_SIZE

    def test_finite_within_limit_still_passes(self, cfg):
        assert check_position_size(notional_usd=100.0, capital_usd=10_000.0, cfg=cfg).passed


# ---------------------------------------------------------------------------
# Daily drawdown
# ---------------------------------------------------------------------------


class TestDailyDrawdownGate:
    @pytest.mark.parametrize("bad", NON_FINITE)
    def test_non_finite_pnl_halts(self, cfg, bad):
        result = check_daily_drawdown(daily_pnl_usd=bad, starting_equity_usd=10_000.0, cfg=cfg)
        assert result.passed is False
        assert result.status is GateStatus.HALT_DRAWDOWN

    @pytest.mark.parametrize("bad", NON_FINITE)
    def test_non_finite_starting_equity_halts(self, cfg, bad):
        result = check_daily_drawdown(daily_pnl_usd=-10.0, starting_equity_usd=bad, cfg=cfg)
        assert result.passed is False
        assert result.status is GateStatus.HALT_DRAWDOWN

    def test_small_loss_still_passes(self, cfg):
        assert check_daily_drawdown(
            daily_pnl_usd=-1.0, starting_equity_usd=10_000.0, cfg=cfg
        ).passed


# ---------------------------------------------------------------------------
# Slippage / negative-EV veto
# ---------------------------------------------------------------------------


class TestSlippageVeto:
    @pytest.mark.parametrize("bad", NON_FINITE)
    def test_non_finite_edge_blocks(self, cfg, bad):
        result = check_slippage_veto(bad, _slippage(2.0), cfg=cfg)
        assert result.passed is False
        assert result.status is GateStatus.HALT_NEGATIVE_EV

    @pytest.mark.parametrize("bad", NON_FINITE)
    def test_non_finite_slippage_blocks(self, cfg, bad):
        result = check_slippage_veto(100.0, _slippage(bad), cfg=cfg)
        assert result.passed is False
        assert result.status is GateStatus.HALT_NEGATIVE_EV

    def test_none_estimate_still_fails_open(self, cfg):
        # "No estimate was produced" is a deliberate pass; only a produced-
        # but-corrupt estimate is blocked.
        assert check_slippage_veto(100.0, None, cfg=cfg).passed

    def test_healthy_edge_still_passes(self, cfg):
        assert check_slippage_veto(1000.0, _slippage(2.0), cfg=cfg).passed


# ---------------------------------------------------------------------------
# Intelligence gates — None and NaN must not mean the same thing
# ---------------------------------------------------------------------------


class TestIntelligenceGates:
    @pytest.mark.parametrize("bad", NON_FINITE)
    def test_non_finite_exchange_stress_halts(self, bad):
        result = check_exchange_stress(bad)
        assert result.passed is False
        assert result.status is GateStatus.HALT_EXCHANGE_STRESS

    def test_none_exchange_stress_still_fails_open(self):
        assert check_exchange_stress(None).passed

    def test_calm_exchange_still_passes(self):
        assert check_exchange_stress(0.1).passed

    @pytest.mark.parametrize("bad", NON_FINITE)
    def test_non_finite_whale_ratio_reduces_size(self, bad):
        # This gate is advisory, so the conservative outcome is REDUCE, not
        # a halt — but it must not read as neutral.
        result = check_whale_activity(bad)
        assert result.passed is False
        assert result.status is GateStatus.REDUCE_WHALE_ACTIVITY

    def test_none_whale_ratio_still_fails_open(self):
        assert check_whale_activity(None).passed

    def test_buying_pressure_still_passes(self):
        assert check_whale_activity(1.2).passed


# ---------------------------------------------------------------------------
# Exit checks — a NaN mark disabled three exits at once
# ---------------------------------------------------------------------------


class TestPositionExit:
    _EXIT = {
        "entry_ts_ms": 1_000_000,
        "now_ts_ms": 1_000_500,
        "stop_loss_enabled": True,
        "stop_loss_pct": 2.0,
        "take_profit_enabled": True,
        "take_profit_pct": 4.0,
        "max_holding_period_s": 86_400.0,
    }

    @pytest.mark.parametrize("bad", NON_FINITE)
    def test_non_finite_mark_closes_the_position(self, bad):
        # stop-loss, trailing stop and take-profit are all comparisons
        # against unrealized_pnl_pct and are all False for NaN, so the
        # position would otherwise sit unprotected until the time exit.
        assert check_position_exit(unrealized_pnl_pct=bad, **self._EXIT) == "invalid_mark"

    def test_invalid_mark_takes_priority_over_a_reachable_stop_loss(self):
        assert (
            check_position_exit(
                unrealized_pnl_pct=math.nan,
                **{**self._EXIT, "trailing_stop_enabled": True, "peak_unrealized_pct": 10.0},
            )
            == "invalid_mark"
        )

    def test_real_stop_loss_is_unaffected(self):
        assert check_position_exit(unrealized_pnl_pct=-5.0, **self._EXIT) == "stop_loss"

    def test_real_take_profit_is_unaffected(self):
        assert check_position_exit(unrealized_pnl_pct=5.0, **self._EXIT) == "profit_target"

    def test_healthy_position_stays_open(self):
        assert check_position_exit(unrealized_pnl_pct=0.5, **self._EXIT) is None


# ---------------------------------------------------------------------------
# The full stack
# ---------------------------------------------------------------------------


class TestFullStackShortCircuits:
    def _ctx(self, **overrides):
        from src.risk.gates import RiskGateContext

        base = {
            "daily_pnl_usd": 0.0,
            "starting_equity_usd": 10_000.0,
            "consecutive_loss_count": 0,
            "regime_state": 0,
            "notional_usd": 100.0,
            "capital_usd": 10_000.0,
            "trading_mode": TradingMode.PAPER,
            "direction_gate_pass": True,
            "meta_gate_pass": True,
            "paper_trading_days": 30,
        }
        base.update(overrides)
        return RiskGateContext(**base)

    def test_baseline_context_passes(self, cfg):
        assert evaluate_all_gates(self._ctx(), cfg=cfg).passed

    def test_nan_notional_blocks_the_whole_stack(self, cfg):
        result = evaluate_all_gates(self._ctx(notional_usd=math.nan), cfg=cfg)
        assert result.passed is False
        assert result.status is GateStatus.HALT_POSITION_SIZE

    def test_nan_equity_blocks_the_whole_stack(self, cfg):
        result = evaluate_all_gates(self._ctx(starting_equity_usd=math.nan), cfg=cfg)
        assert result.passed is False
        assert result.status is GateStatus.HALT_DRAWDOWN

    def test_volatile_regime_still_blocks(self, cfg):
        result = evaluate_all_gates(self._ctx(regime_state=REGIME_VOLATILE), cfg=cfg)
        assert result.passed is False
        assert result.status is GateStatus.HALT_REGIME
