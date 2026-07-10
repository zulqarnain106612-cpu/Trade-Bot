"""Tests for src/risk/gates.py — all risk gate functions and the full stack."""

import pytest

from src.config import (
    REGIME_RANGING,
    REGIME_TRENDING,
    REGIME_VOLATILE,
    TradingMode,
    invalidate_settings_cache,
)
from src.risk.gates import (
    DrawdownTracker,
    GateStatus,
    RiskGateContext,
    check_consecutive_losses,
    check_daily_drawdown,
    check_live_gate,
    check_paper_minimum_days,
    check_position_size,
    check_regime_gate,
    check_slippage_veto,
    evaluate_all_gates,
)
from src.risk.slippage import SlippageModel


@pytest.fixture(autouse=True)
def reset_settings():
    invalidate_settings_cache()
    yield
    invalidate_settings_cache()


# ─── check_daily_drawdown ─────────────────────────────────────────────────────


class TestDailyDrawdown:
    def test_pass_small_loss(self):
        assert check_daily_drawdown(-10.0, 1000.0).passed  # -1%

    def test_pass_profit(self):
        assert check_daily_drawdown(50.0, 1000.0).passed

    def test_fail_at_threshold(self):
        result = check_daily_drawdown(-20.0, 1000.0)  # exactly -2%
        assert not result.passed
        assert result.status == GateStatus.HALT_DRAWDOWN

    def test_fail_below_threshold(self):
        assert not check_daily_drawdown(-30.0, 1000.0).passed

    def test_fail_zero_equity(self):
        result = check_daily_drawdown(0.0, 0.0)
        assert not result.passed
        assert result.status == GateStatus.HALT_DRAWDOWN

    def test_pass_just_above_threshold(self):
        assert check_daily_drawdown(-19.9, 1000.0).passed

    def test_details_populated(self):
        result = check_daily_drawdown(-30.0, 1000.0)
        assert "drawdown_pct" in result.details
        assert result.details["daily_pnl_usd"] == -30.0


# ─── check_consecutive_losses ────────────────────────────────────────────────


class TestConsecutiveLosses:
    def test_pass_zero(self):
        assert check_consecutive_losses(0).passed

    def test_pass_two(self):
        assert check_consecutive_losses(2).passed

    def test_fail_at_three(self):
        result = check_consecutive_losses(3)
        assert not result.passed
        assert result.status == GateStatus.HALT_CONSECUTIVE_LOSSES

    def test_fail_above_three(self):
        assert not check_consecutive_losses(10).passed

    def test_details_populated(self):
        result = check_consecutive_losses(3)
        assert result.details["consecutive_losses"] == 3
        assert result.details["threshold"] == 3


# ─── check_regime_gate ───────────────────────────────────────────────────────


class TestRegimeGate:
    def test_pass_ranging(self):
        assert check_regime_gate(REGIME_RANGING).passed

    def test_pass_trending(self):
        assert check_regime_gate(REGIME_TRENDING).passed

    def test_fail_volatile(self):
        result = check_regime_gate(REGIME_VOLATILE)
        assert not result.passed
        assert result.status == GateStatus.HALT_REGIME

    def test_details_contain_state(self):
        result = check_regime_gate(REGIME_VOLATILE)
        assert result.details["regime_state"] == REGIME_VOLATILE


# ─── check_position_size ─────────────────────────────────────────────────────


class TestPositionSize:
    def test_pass_below_max(self):
        assert check_position_size(40.0, 1000.0).passed  # 4% < 5%

    def test_pass_at_max(self):
        assert check_position_size(50.0, 1000.0).passed  # exactly 5%

    def test_fail_above_max(self):
        result = check_position_size(50.1, 1000.0)
        assert not result.passed
        assert result.status == GateStatus.HALT_POSITION_SIZE

    def test_fail_zero_capital(self):
        result = check_position_size(10.0, 0.0)
        assert not result.passed

    def test_fail_large_notional(self):
        assert not check_position_size(600.0, 1000.0).passed

    def test_details_include_pct(self):
        result = check_position_size(60.0, 1000.0)
        assert "position_pct" in result.details
        assert result.details["max_pct"] == 5.0


# ─── check_live_gate ─────────────────────────────────────────────────────────


class TestLiveGate:
    def test_paper_always_passes(self):
        assert check_live_gate(TradingMode.PAPER, False, False).passed

    def test_live_fails_without_direction(self):
        result = check_live_gate(TradingMode.LIVE, False, True)
        assert not result.passed
        assert result.status == GateStatus.HALT_LIVE_GATE

    def test_live_fails_without_meta(self):
        assert not check_live_gate(TradingMode.LIVE, True, False).passed

    def test_live_passes_both(self):
        assert check_live_gate(TradingMode.LIVE, True, True).passed

    def test_details_include_mode(self):
        result = check_live_gate(TradingMode.LIVE, False, False)
        assert result.details["trading_mode"] == "live"


# ─── check_paper_minimum_days ────────────────────────────────────────────────


class TestPaperMinimumDays:
    def test_pass_at_minimum(self):
        assert check_paper_minimum_days(30, settings_override=30).passed

    def test_pass_above_minimum(self):
        assert check_paper_minimum_days(60, settings_override=30).passed

    def test_fail_below_minimum(self):
        result = check_paper_minimum_days(29, settings_override=30)
        assert not result.passed
        assert result.status == GateStatus.HALT_PAPER_ONLY

    def test_days_remaining_in_details(self):
        result = check_paper_minimum_days(10, settings_override=30)
        assert result.details["days_remaining"] == 20


# ─── evaluate_all_gates ──────────────────────────────────────────────────────


def _make_ctx(**overrides) -> RiskGateContext:
    defaults = {
        "daily_pnl_usd": -5.0,
        "starting_equity_usd": 1000.0,
        "consecutive_loss_count": 1,
        "regime_state": REGIME_TRENDING,
        "notional_usd": 40.0,
        "capital_usd": 1000.0,
        "trading_mode": TradingMode.PAPER,
        "direction_gate_pass": True,
        "meta_gate_pass": True,
        "paper_trading_days": 30,
    }
    defaults.update(overrides)
    return RiskGateContext(**defaults)


class TestEvaluateAllGates:
    def test_full_pass(self):
        assert evaluate_all_gates(_make_ctx()).passed

    def test_drawdown_blocks_first(self):
        ctx = _make_ctx(daily_pnl_usd=-30.0, consecutive_loss_count=0)
        result = evaluate_all_gates(ctx)
        assert result.status == GateStatus.HALT_DRAWDOWN

    def test_consecutive_blocks(self):
        ctx = _make_ctx(consecutive_loss_count=3)
        assert evaluate_all_gates(ctx).status == GateStatus.HALT_CONSECUTIVE_LOSSES

    def test_regime_blocks(self):
        ctx = _make_ctx(regime_state=REGIME_VOLATILE)
        assert evaluate_all_gates(ctx).status == GateStatus.HALT_REGIME

    def test_position_size_blocks(self):
        ctx = _make_ctx(notional_usd=200.0)
        assert evaluate_all_gates(ctx).status == GateStatus.HALT_POSITION_SIZE

    def test_live_gate_blocks(self):
        ctx = _make_ctx(trading_mode=TradingMode.LIVE, direction_gate_pass=False)
        assert evaluate_all_gates(ctx).status == GateStatus.HALT_LIVE_GATE

    def test_gate_result_is_frozen(self):
        result = evaluate_all_gates(_make_ctx())
        with pytest.raises((AttributeError, TypeError)):
            result.passed = False  # type: ignore[misc]


# ─── DrawdownTracker ─────────────────────────────────────────────────────────


class TestDrawdownTracker:
    def test_initial_state(self):
        dt = DrawdownTracker(1000.0)
        assert dt.starting_equity == 1000.0
        assert dt.peak_equity == 1000.0
        assert dt.current_equity == 1000.0
        assert dt.daily_pnl_usd == 0.0

    def test_update_up(self):
        dt = DrawdownTracker(1000.0)
        dt.update(1100.0)
        assert dt.peak_equity == 1100.0
        assert dt.current_equity == 1100.0
        assert dt.drawdown_from_peak_pct == 0.0

    def test_drawdown_after_decline(self):
        dt = DrawdownTracker(1000.0)
        dt.update(1100.0)
        dt.update(990.0)
        assert dt.peak_equity == 1100.0
        expected_dd = ((990.0 - 1100.0) / 1100.0) * 100.0
        assert abs(dt.drawdown_from_peak_pct - expected_dd) < 0.001

    def test_daily_pnl(self):
        dt = DrawdownTracker(1000.0)
        dt.update(1050.0)
        assert abs(dt.daily_pnl_usd - 50.0) < 1e-9
        assert abs(dt.daily_pnl_pct - 5.0) < 0.001

    def test_reset_daily(self):
        dt = DrawdownTracker(1000.0)
        dt.update(1050.0)
        dt.reset_daily(1050.0)
        assert dt.daily_start_equity == 1050.0
        assert dt.daily_pnl_usd == 0.0

    def test_invalid_starting_equity(self):
        with pytest.raises(ValueError):
            DrawdownTracker(0.0)
        with pytest.raises(ValueError):
            DrawdownTracker(-100.0)


# ─── check_slippage_veto (Gate 0, GAP-001) ───────────────────────────────────


class TestSlippageVetoGate:
    def test_none_estimate_passes_open(self):
        result = check_slippage_veto(expected_edge_bps=0.0, slippage=None)
        assert result.passed
        assert result.details["slippage_gate"] == "skipped_no_estimate"

    def test_sufficient_edge_passes(self):
        est = SlippageModel().estimate("BTC/USDT", qty=0.1, price=100.0, adv_20d=10_000.0)
        result = check_slippage_veto(expected_edge_bps=50.0, slippage=est)
        assert result.passed

    def test_insufficient_edge_blocks(self):
        est = SlippageModel().estimate("BTC/USDT", qty=0.1, price=100.0, adv_20d=10_000.0)
        result = check_slippage_veto(expected_edge_bps=0.5, slippage=est)
        assert not result.passed
        assert result.status == GateStatus.HALT_NEGATIVE_EV

    def test_details_populated_on_block(self):
        est = SlippageModel().estimate("BTC/USDT", qty=0.1, price=100.0, adv_20d=10_000.0)
        result = check_slippage_veto(expected_edge_bps=0.5, slippage=est)
        assert "net_edge_bps" in result.details
        assert result.details["symbol"] == "BTC/USDT"


# ─── evaluate_all_gates — gate 0 wiring ──────────────────────────────────────


class TestEvaluateAllGatesSlippageWiring:
    def test_backward_compat_no_estimate_still_passes(self):
        # Existing callers that have not wired a SlippageModel yet must not
        # be blocked by the new gate 0.
        ctx = _make_ctx()
        assert evaluate_all_gates(ctx).passed

    def test_negative_ev_blocks_before_drawdown_check(self):
        est = SlippageModel().estimate("BTC/USDT", qty=0.1, price=100.0, adv_20d=10_000.0)
        # daily_pnl_usd well within limits so only gate 0 could possibly fail
        ctx = _make_ctx(
            daily_pnl_usd=-5.0,
            expected_edge_bps=0.5,
            slippage_estimate=est,
        )
        result = evaluate_all_gates(ctx)
        assert not result.passed
        assert result.status == GateStatus.HALT_NEGATIVE_EV

    def test_positive_ev_with_estimate_passes_full_stack(self):
        est = SlippageModel().estimate("BTC/USDT", qty=0.1, price=100.0, adv_20d=10_000.0)
        ctx = _make_ctx(expected_edge_bps=50.0, slippage_estimate=est)
        assert evaluate_all_gates(ctx).passed
