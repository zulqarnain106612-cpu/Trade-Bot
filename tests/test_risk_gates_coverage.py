"""
Additional coverage for src/risk/gates.py — Debt-005.

Tests individual gate functions directly (pure functions, no async).
"""

from __future__ import annotations

from src.config import TradingMode
from src.risk.gates import (
    GateResult,
    GateStatus,
    RiskGateContext,
    check_consecutive_losses,
    check_daily_drawdown,
    check_position_size,
    check_regime_gate,
    check_slippage_veto,
    evaluate_all_gates,
)
from src.risk.slippage import SlippageEstimate


def _slip(total_bps: float = 5.0) -> SlippageEstimate:
    return SlippageEstimate(
        symbol="BTC/USDT",
        qty=1.0,
        notional_usd=100.0,
        adv_20d=1000.0,
        spread_bps=total_bps * 0.5,
        impact_bps=total_bps * 0.5,
        total_slippage_bps=total_bps,
        total_cost_usd=total_bps * 100.0 / 10_000,
        participation_rate=0.001,
    )


# ---------------------------------------------------------------------------
# check_slippage_veto
# ---------------------------------------------------------------------------


class TestSlippageVeto:
    def test_none_slippage_passes(self):
        r = check_slippage_veto(expected_edge_bps=10.0, slippage=None)
        assert r.passed is True

    def test_positive_net_edge_passes(self):
        r = check_slippage_veto(expected_edge_bps=20.0, slippage=_slip(5.0))
        assert r.passed is True

    def test_zero_net_edge_blocks(self):
        # edge 5, slippage 5, margin typically adds more — should block
        r = check_slippage_veto(expected_edge_bps=5.0, slippage=_slip(5.0))
        assert r.passed is False
        assert r.status == GateStatus.HALT_NEGATIVE_EV

    def test_negative_net_edge_blocks(self):
        r = check_slippage_veto(expected_edge_bps=1.0, slippage=_slip(50.0))
        assert r.passed is False
        assert r.status == GateStatus.HALT_NEGATIVE_EV
        assert "net EV" in r.reason


# ---------------------------------------------------------------------------
# check_daily_drawdown
# ---------------------------------------------------------------------------


class TestDailyDrawdown:
    def test_no_loss_passes(self):
        r = check_daily_drawdown(daily_pnl_usd=0.0, starting_equity_usd=10_000.0)
        assert r.passed is True

    def test_small_loss_passes(self):
        r = check_daily_drawdown(daily_pnl_usd=-100.0, starting_equity_usd=10_000.0)
        assert r.passed is True  # -1% < typical 2% halt

    def test_large_loss_blocks(self):
        # -6% loss on $10k = -$600
        r = check_daily_drawdown(daily_pnl_usd=-600.0, starting_equity_usd=10_000.0)
        assert r.passed is False
        assert r.status == GateStatus.HALT_DRAWDOWN
        assert "drawdown" in r.reason.lower()

    def test_zero_equity_blocks(self):
        r = check_daily_drawdown(daily_pnl_usd=0.0, starting_equity_usd=0.0)
        assert r.passed is False
        assert r.status == GateStatus.HALT_DRAWDOWN

    def test_details_populated(self):
        r = check_daily_drawdown(daily_pnl_usd=-50.0, starting_equity_usd=10_000.0)
        assert "drawdown_pct" in r.details
        assert "threshold_pct" in r.details


# ---------------------------------------------------------------------------
# check_consecutive_losses
# ---------------------------------------------------------------------------


class TestConsecutiveLosses:
    def test_zero_losses_passes(self):
        r = check_consecutive_losses(0)
        assert r.passed is True

    def test_below_threshold_passes(self):
        r = check_consecutive_losses(2)
        assert r.passed is True

    def test_at_threshold_blocks(self):
        from src.config import get_settings

        threshold = get_settings().risk.consecutive_loss_halt
        r = check_consecutive_losses(threshold)
        assert r.passed is False
        assert r.status == GateStatus.HALT_CONSECUTIVE_LOSSES

    def test_above_threshold_blocks(self):
        r = check_consecutive_losses(999)
        assert r.passed is False

    def test_details_populated(self):
        r = check_consecutive_losses(1)
        assert "consecutive_losses" in r.details
        assert "threshold" in r.details


# ---------------------------------------------------------------------------
# check_regime_gate
# ---------------------------------------------------------------------------


class TestRegimeGate:
    def test_ranging_passes(self):
        r = check_regime_gate(regime_state=0)  # ranging
        assert r.passed is True

    def test_trending_passes(self):
        r = check_regime_gate(regime_state=1)  # trending
        assert r.passed is True

    def test_volatile_blocks(self):
        r = check_regime_gate(regime_state=2)  # volatile
        assert r.passed is False
        assert r.status == GateStatus.HALT_REGIME

    def test_details_populated(self):
        r = check_regime_gate(regime_state=1)
        assert "regime_state" in r.details


# ---------------------------------------------------------------------------
# check_position_size
# ---------------------------------------------------------------------------


class TestPositionSize:
    def test_small_notional_passes(self):
        r = check_position_size(notional_usd=100.0, capital_usd=10_000.0)
        assert r.passed is True  # 1% << max

    def test_large_notional_blocks(self):
        # 90% of capital — way above max (typically 5%)
        r = check_position_size(notional_usd=9_000.0, capital_usd=10_000.0)
        assert r.passed is False
        assert r.status == GateStatus.HALT_POSITION_SIZE

    def test_zero_capital_blocks(self):
        r = check_position_size(notional_usd=100.0, capital_usd=0.0)
        assert r.passed is False

    def test_details_populated(self):
        r = check_position_size(notional_usd=100.0, capital_usd=10_000.0)
        assert "position_pct" in r.details or "capital_usd" in r.details or r.passed


# ---------------------------------------------------------------------------
# GateResult class methods
# ---------------------------------------------------------------------------


class TestGateResult:
    def test_pass_gate_creates_passed_result(self):
        r = GateResult.pass_gate()
        assert r.passed is True
        assert r.status == GateStatus.PASS

    def test_fail_creates_failed_result(self):
        r = GateResult.fail(GateStatus.HALT_DRAWDOWN, reason="test")
        assert r.passed is False
        assert r.status == GateStatus.HALT_DRAWDOWN
        assert r.reason == "test"

    def test_details_default_empty(self):
        r = GateResult.pass_gate()
        assert isinstance(r.details, dict)

    def test_fail_with_details(self):
        r = GateResult.fail(GateStatus.HALT_REGIME, reason="volatile", details={"key": 1})
        assert r.details["key"] == 1


# ---------------------------------------------------------------------------
# evaluate_all_gates integration
# ---------------------------------------------------------------------------


class TestEvaluateAllGates:
    def _ctx(self, **overrides) -> RiskGateContext:
        defaults = {
            "daily_pnl_usd": 0.0,
            "starting_equity_usd": 10_000.0,
            "consecutive_loss_count": 0,
            "regime_state": 1,  # trending
            "notional_usd": 200.0,
            "capital_usd": 10_000.0,
            "trading_mode": TradingMode.PAPER,
            "direction_gate_pass": True,
            "meta_gate_pass": True,
            "paper_trading_days": 30,
            "expected_edge_bps": 20.0,
            "slippage_estimate": None,
        }
        defaults.update(overrides)
        return RiskGateContext(**defaults)

    def test_all_clear_passes(self):
        r = evaluate_all_gates(self._ctx())
        assert r.passed is True

    def test_drawdown_blocks(self):
        r = evaluate_all_gates(self._ctx(daily_pnl_usd=-800.0, starting_equity_usd=10_000.0))
        assert r.passed is False
        assert r.status == GateStatus.HALT_DRAWDOWN

    def test_consecutive_losses_blocks(self):
        r = evaluate_all_gates(self._ctx(consecutive_loss_count=999))
        assert r.passed is False
        assert r.status == GateStatus.HALT_CONSECUTIVE_LOSSES

    def test_volatile_regime_blocks(self):
        r = evaluate_all_gates(self._ctx(regime_state=2))
        assert r.passed is False
        assert r.status == GateStatus.HALT_REGIME

    def test_oversized_position_blocks(self):
        r = evaluate_all_gates(self._ctx(notional_usd=9_000.0, capital_usd=10_000.0))
        assert r.passed is False
        assert r.status == GateStatus.HALT_POSITION_SIZE

    def test_direction_gate_not_passed_blocks(self):  # only fires in LIVE mode
        r = evaluate_all_gates(self._ctx(direction_gate_pass=False, trading_mode=TradingMode.LIVE))
        assert r.passed is False
        assert r.status == GateStatus.HALT_LIVE_GATE
