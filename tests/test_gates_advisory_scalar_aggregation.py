"""Coverage for the advisory-scalar aggregation in evaluate_all_gates.

Advisory gates PASS (so they do not short-circuit the sequence) but carry
a size_scalar < 1.0 that must multiply into the ceiling on the final
result. That accumulation loop and the reduced-PASS return were
uncovered.

check_whale_activity is patched rather than driven through real whale
inputs: the behaviour under test is the aggregation across gates, not
whale-ratio thresholds, and patching keeps the two concerns separable.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

import src.risk.gates as gates_mod
from src.risk.gates import (
    DrawdownTracker,
    GateResult,
    GateStatus,
    RiskGateContext,
    evaluate_all_gates,
)
from src.config import TradingMode


def _ctx(**overrides) -> RiskGateContext:
    kwargs = dict(
        daily_pnl_usd=0.0,
        starting_equity_usd=10_000.0,
        consecutive_loss_count=0,
        regime_state=1,
        notional_usd=100.0,
        capital_usd=10_000.0,
        trading_mode=TradingMode.PAPER,
        direction_gate_pass=True,
        meta_gate_pass=True,
    )
    kwargs.update(overrides)
    return RiskGateContext(**kwargs)


def test_all_gates_pass_with_no_advisory_reduction():
    result = evaluate_all_gates(_ctx())
    assert result.passed is True
    assert result.size_scalar == 1.0


def test_single_advisory_scalar_reduces_the_final_result():
    reduced = GateResult.reduce(GateStatus.PASS, 0.5, reason="whale advisory")
    with patch.object(gates_mod, "check_whale_activity", return_value=reduced):
        result = evaluate_all_gates(_ctx())
    assert result.passed is True
    assert result.size_scalar == pytest.approx(0.5)
    assert "advisory" in result.reason


def test_multiple_advisory_scalars_multiply():
    half = GateResult.reduce(GateStatus.PASS, 0.5, reason="whale advisory")
    quarter = GateResult.reduce(GateStatus.PASS, 0.5, reason="stress advisory")
    with (
        patch.object(gates_mod, "check_whale_activity", return_value=half),
        patch.object(gates_mod, "check_exchange_stress", return_value=quarter),
    ):
        result = evaluate_all_gates(_ctx())
    # Scalars compound rather than taking the min -- two independent 50%
    # reductions leave 25% of the original size.
    assert result.size_scalar == pytest.approx(0.25)


def test_a_hard_veto_short_circuits_before_advisory_aggregation():
    veto = GateResult.fail(GateStatus.HALT_REGIME, reason="regime blocked")
    advisory = GateResult.reduce(GateStatus.PASS, 0.5, reason="whale advisory")
    with (
        patch.object(gates_mod, "check_regime_gate", return_value=veto),
        patch.object(gates_mod, "check_whale_activity", return_value=advisory),
    ):
        result = evaluate_all_gates(_ctx())
    assert result.passed is False
    assert result.reason == "regime blocked"


def test_drawdown_tracker_rejects_non_positive_starting_equity():
    with pytest.raises(ValueError, match="starting_equity must be > 0"):
        DrawdownTracker(0.0)


def test_drawdown_tracker_tracks_peak_and_drawdown():
    tracker = DrawdownTracker(10_000.0)
    tracker.update(12_000.0)
    assert tracker.drawdown_from_peak_pct == pytest.approx(0.0)
    tracker.update(9_000.0)
    # Peak-relative, not start-relative: 3000 off a 12000 peak is -25%.
    assert tracker.drawdown_from_peak_pct == pytest.approx(-25.0)


def test_drawdown_tracker_daily_pnl_and_reset():
    tracker = DrawdownTracker(10_000.0)
    tracker.update(10_500.0)
    assert tracker.daily_pnl_usd == pytest.approx(500.0)
    assert tracker.daily_pnl_pct == pytest.approx(5.0)
    tracker.reset_daily(10_500.0)
    assert tracker.daily_pnl_usd == pytest.approx(0.0)
