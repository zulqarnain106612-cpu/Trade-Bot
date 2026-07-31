"""Tests for src/risk/capital_preservation_floor.py"""

from __future__ import annotations

import pytest

from src.risk.capital_preservation_floor import (
    CapitalPreservationFloor,
)


INITIAL = 10_000.0


def _floor(trigger=0.10, lock_in=0.05, max_loss=0.20, initial=INITIAL) -> CapitalPreservationFloor:
    return CapitalPreservationFloor(
        initial_capital=initial,
        trigger_pct=trigger,
        lock_in_pct=lock_in,
        max_loss_pct=max_loss,
    )


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


def test_init_defaults():
    f = CapitalPreservationFloor(initial_capital=10_000.0)
    assert f.hwm == 10_000.0
    assert f.floor == 0.0
    assert not f.ratchet_active


def test_init_zero_capital_raises():
    with pytest.raises(ValueError, match="initial_capital"):
        CapitalPreservationFloor(initial_capital=0.0)


def test_init_negative_capital_raises():
    with pytest.raises(ValueError, match="initial_capital"):
        CapitalPreservationFloor(initial_capital=-100.0)


def test_init_lock_in_gte_trigger_raises():
    with pytest.raises(ValueError, match="lock_in_pct"):
        CapitalPreservationFloor(initial_capital=10_000.0, trigger_pct=0.10, lock_in_pct=0.10)


def test_init_bad_max_loss_raises():
    with pytest.raises(ValueError, match="max_loss_pct"):
        CapitalPreservationFloor(initial_capital=10_000.0, max_loss_pct=0.0)
    with pytest.raises(ValueError, match="max_loss_pct"):
        CapitalPreservationFloor(initial_capital=10_000.0, max_loss_pct=1.0)


# ---------------------------------------------------------------------------
# check — baseline (no gates fired)
# ---------------------------------------------------------------------------


def test_check_at_initial_equity_allowed():
    f = _floor()
    result = f.check(INITIAL)
    assert result.allowed is True
    assert result.reason == ""


def test_check_small_gain_allowed():
    f = _floor()
    result = f.check(INITIAL * 1.05)  # +5%, below trigger
    assert result.allowed is True
    assert not f.ratchet_active


def test_check_updates_hwm():
    f = _floor()
    f.check(INITIAL * 1.15)
    assert f.hwm == pytest.approx(INITIAL * 1.15)


def test_hwm_does_not_decrease():
    f = _floor()
    f.check(INITIAL * 1.20)
    f.check(INITIAL * 0.95)  # equity drops
    assert f.hwm == pytest.approx(INITIAL * 1.20)


# ---------------------------------------------------------------------------
# check — ratchet activation
# ---------------------------------------------------------------------------


def test_ratchet_activates_at_trigger():
    f = _floor(trigger=0.10, lock_in=0.05)
    result = f.check(INITIAL * 1.10)  # exactly at trigger
    assert f.ratchet_active is True
    assert f.floor == pytest.approx(INITIAL * 1.05)
    assert result.allowed is True  # equity is above floor


def test_ratchet_does_not_activate_below_trigger():
    f = _floor(trigger=0.10, lock_in=0.05)
    f.check(INITIAL * 1.09)
    assert f.ratchet_active is False
    assert f.floor == 0.0


def test_ratchet_floor_blocks_entry_below_floor():
    f = _floor(trigger=0.10, lock_in=0.05)
    f.check(INITIAL * 1.10)  # activate ratchet, floor = 1.05x
    result = f.check(INITIAL * 1.02)  # below floor (1.05x)
    assert result.allowed is False
    assert "ratchet_floor" in result.reason


def test_ratchet_floor_allows_entry_above_floor():
    f = _floor(trigger=0.10, lock_in=0.05)
    f.check(INITIAL * 1.20)  # activate ratchet
    result = f.check(INITIAL * 1.06)  # above floor (1.05x)
    assert result.allowed is True


# ---------------------------------------------------------------------------
# check — absolute max-loss floor
# ---------------------------------------------------------------------------


def test_absolute_floor_blocks_below_max_loss():
    f = _floor(max_loss=0.20)
    result = f.check(INITIAL * 0.79)  # 21% loss → below 80% floor
    assert result.allowed is False
    assert "absolute_floor" in result.reason


def test_absolute_floor_allows_at_max_loss_boundary():
    f = _floor(max_loss=0.20)
    result = f.check(INITIAL * 0.80)  # exactly at floor: equity >= floor
    assert result.allowed is True


def test_absolute_floor_fires_without_ratchet():
    f = _floor(trigger=0.50, lock_in=0.30, max_loss=0.20)
    # ratchet hasn't fired (trigger=50%), but max_loss still active
    result = f.check(INITIAL * 0.75)
    assert result.allowed is False
    assert not f.ratchet_active


# ---------------------------------------------------------------------------
# FloorCheckResult fields
# ---------------------------------------------------------------------------


def test_result_gain_pct_positive():
    f = _floor()
    result = f.check(INITIAL * 1.15)
    assert result.gain_since_start_pct == pytest.approx(0.15)


def test_result_gain_pct_negative():
    f = _floor()
    result = f.check(INITIAL * 0.90)
    assert result.gain_since_start_pct == pytest.approx(-0.10)


def test_result_drawdown_from_hwm():
    f = _floor()
    f.check(INITIAL * 1.20)
    result = f.check(INITIAL * 1.10)
    # dd = (1.20 - 1.10) / 1.20
    expected_dd = (INITIAL * 1.20 - INITIAL * 1.10) / (INITIAL * 1.20)
    assert result.drawdown_from_hwm_pct == pytest.approx(expected_dd)


def test_result_to_dict_keys():
    f = _floor()
    result = f.check(INITIAL)
    d = result.to_dict()
    assert "allowed" in d
    assert "reason" in d
    assert "hwm" in d
    assert "floor" in d
    assert "ratchet_active" in d
    assert "gain_since_start_pct" in d
    assert "drawdown_from_hwm_pct" in d


def test_result_frozen():
    f = _floor()
    result = f.check(INITIAL)
    with pytest.raises((AttributeError, TypeError)):
        result.allowed = False  # type: ignore[misc]


# ---------------------------------------------------------------------------
# reset
# ---------------------------------------------------------------------------


def test_reset_clears_ratchet():
    f = _floor()
    f.check(INITIAL * 1.15)
    assert f.ratchet_active
    f.reset()
    assert not f.ratchet_active
    assert f.floor == 0.0
    assert f.hwm == INITIAL


def test_reset_with_new_capital():
    f = _floor()
    f.check(INITIAL * 1.20)
    f.reset(new_initial_capital=20_000.0)
    assert f.hwm == 20_000.0
    assert f.floor == 0.0


def test_reset_then_check_starts_fresh():
    f = _floor(trigger=0.10, lock_in=0.05)
    f.check(INITIAL * 1.15)  # activate ratchet
    f.reset()
    result = f.check(INITIAL * 0.91)  # only 9% below — no max_loss trigger
    assert result.allowed is True  # ratchet inactive after reset


# ---------------------------------------------------------------------------
# state_dict
# ---------------------------------------------------------------------------


def test_state_dict_structure():
    f = _floor()
    d = f.state_dict()
    assert "initial_capital" in d
    assert "hwm" in d
    assert "floor" in d
    assert "ratchet_active" in d
