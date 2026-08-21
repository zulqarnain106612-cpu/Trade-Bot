"""Tests for src/risk/vol_target_sizer.py"""

from __future__ import annotations

import math
import random

import pytest

from src.risk.vol_target_sizer import (
    SizeResult,
    _annualised_vol,
    _drawdown_haircut,
    _kelly_scalar,
    vol_target_size,
    vol_target_size_from_returns,
)


CAPITAL = 100_000.0
HWM = CAPITAL


# ---------------------------------------------------------------------------
# _annualised_vol
# ---------------------------------------------------------------------------


def test_annualised_vol_empty_returns_zero():
    assert _annualised_vol([]) == 0.0


def test_annualised_vol_single_return_zero():
    assert _annualised_vol([0.01]) == 0.0


def test_annualised_vol_positive_for_varied_returns():
    rng = random.Random(42)
    returns = [rng.gauss(0, 0.01) for _ in range(50)]
    vol = _annualised_vol(returns)
    assert vol > 0.0


def test_annualised_vol_higher_for_more_volatile():
    rng = random.Random(7)
    low_vol = [rng.gauss(0, 0.001) for _ in range(50)]
    high_vol = [rng.gauss(0, 0.05) for _ in range(50)]
    assert _annualised_vol(high_vol) > _annualised_vol(low_vol)


def test_annualised_vol_finite():
    returns = [0.01, -0.02, 0.03, -0.01, 0.02]
    assert math.isfinite(_annualised_vol(returns))


# ---------------------------------------------------------------------------
# _kelly_scalar
# ---------------------------------------------------------------------------


def test_kelly_scalar_zero_win_rate():
    assert _kelly_scalar(0.0, 1.0, 1.0) == 0.0


def test_kelly_scalar_zero_avg_loss():
    assert _kelly_scalar(0.5, 1.0, 0.0) == 0.0


def test_kelly_scalar_symmetric_50pct():
    # win=0.5, b=1 → f* = (0.5 - 0.5) / 1 = 0 → half-Kelly = 0
    assert _kelly_scalar(0.5, 1.0, 1.0) == 0.0


def test_kelly_scalar_positive_edge():
    # win=0.6, b=1 → f* = (0.6 - 0.4) = 0.2 → half = 0.1
    ks = _kelly_scalar(0.6, 1.0, 1.0)
    assert ks == pytest.approx(0.1)


def test_kelly_scalar_capped_at_one():
    # win=1.0, b=999 → f*=1 → half=0.5
    ks = _kelly_scalar(1.0, 999.0, 1.0)
    assert ks <= 1.0


def test_kelly_scalar_nonnegative():
    for wr in [0.0, 0.3, 0.5, 0.7, 0.9]:
        ks = _kelly_scalar(wr, 2.0, 1.0)
        assert ks >= 0.0


# ---------------------------------------------------------------------------
# _drawdown_haircut
# ---------------------------------------------------------------------------


def test_dd_haircut_no_drawdown():
    assert _drawdown_haircut(100_000, 100_000, 0.10, 0.20) == pytest.approx(1.0)


def test_dd_haircut_equity_above_hwm():
    assert _drawdown_haircut(110_000, 100_000, 0.10, 0.20) == pytest.approx(1.0)


def test_dd_haircut_halt_at_dd_halt():
    # 20% drawdown → halt
    assert _drawdown_haircut(80_000, 100_000, 0.10, 0.20) == pytest.approx(0.0)


def test_dd_haircut_below_warn_unchanged():
    # 5% drawdown < 10% warn → no taper
    result = _drawdown_haircut(95_000, 100_000, 0.10, 0.20)
    assert result == pytest.approx(1.0)


def test_dd_haircut_linear_taper():
    # 15% drawdown midway between 10% and 20% → 0.5
    result = _drawdown_haircut(85_000, 100_000, 0.10, 0.20)
    assert result == pytest.approx(0.5)


def test_dd_haircut_zero_hwm():
    # Edge case: hwm = 0
    assert _drawdown_haircut(100_000, 0.0, 0.10, 0.20) == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# vol_target_size — basic contracts
# ---------------------------------------------------------------------------


def test_size_zero_capital_returns_zero():
    result = vol_target_size(0.0, 0.0, 0.0, realized_vol_pct=20.0)
    assert result.notional_usd == 0.0
    assert "invalid_capital" in result.reject_reason


def test_size_returns_positive_notional():
    result = vol_target_size(CAPITAL, CAPITAL, HWM, realized_vol_pct=20.0)
    assert result.notional_usd > 0.0
    assert result.reject_reason == ""


def test_size_higher_vol_gives_smaller_notional():
    r1 = vol_target_size(CAPITAL, CAPITAL, HWM, realized_vol_pct=10.0)
    r2 = vol_target_size(CAPITAL, CAPITAL, HWM, realized_vol_pct=50.0)
    assert r1.notional_usd > r2.notional_usd


def test_size_capped_at_max_notional():
    # Very low vol → huge raw notional; cap at 25%
    result = vol_target_size(CAPITAL, CAPITAL, HWM, realized_vol_pct=0.01, max_notional_pct=0.25)
    assert result.notional_usd <= CAPITAL * 0.25 + 1e-3


def test_size_dd_halt_gives_zero():
    equity = CAPITAL * 0.75  # 25% drawdown > 20% halt
    result = vol_target_size(CAPITAL, equity, HWM, realized_vol_pct=20.0)
    assert result.notional_usd == 0.0
    assert "dd_halt" in result.reject_reason


def test_size_dd_taper_reduces_notional():
    equity_ok = CAPITAL * 0.99  # no drawdown
    equity_dd = CAPITAL * 0.87  # ~13% drawdown → taper
    r1 = vol_target_size(CAPITAL, equity_ok, HWM, realized_vol_pct=20.0)
    r2 = vol_target_size(CAPITAL, equity_dd, HWM, realized_vol_pct=20.0)
    assert r1.notional_usd > r2.notional_usd


def test_size_result_frozen():
    result = vol_target_size(CAPITAL, CAPITAL, HWM, realized_vol_pct=20.0)
    with pytest.raises((AttributeError, TypeError)):
        result.notional_usd = 999.0  # type: ignore[misc]


def test_size_to_dict_keys():
    result = vol_target_size(CAPITAL, CAPITAL, HWM, realized_vol_pct=20.0)
    d = result.to_dict()
    for key in (
        "notional_usd",
        "vol_target_notional",
        "kelly_scalar",
        "dd_haircut",
        "realized_vol_pct",
        "reject_reason",
    ):
        assert key in d


# ---------------------------------------------------------------------------
# vol_target_size_from_returns
# ---------------------------------------------------------------------------


def test_size_from_returns_empty():
    result = vol_target_size_from_returns([], CAPITAL, CAPITAL, HWM)
    # With vol=0, eff_vol uses floor → notional should be capped at max
    assert isinstance(result, SizeResult)


def test_size_from_returns_uses_computed_vol():
    rng = random.Random(99)
    returns_low = [rng.gauss(0, 0.001) for _ in range(50)]
    returns_high = [rng.gauss(0, 0.05) for _ in range(50)]
    r1 = vol_target_size_from_returns(returns_low, CAPITAL, CAPITAL, HWM)
    r2 = vol_target_size_from_returns(returns_high, CAPITAL, CAPITAL, HWM)
    assert r1.notional_usd >= r2.notional_usd  # lower vol → bigger position


def test_size_from_returns_kwargs_forwarded():
    returns = [0.01, -0.02, 0.005, -0.015, 0.02]
    result = vol_target_size_from_returns(returns, CAPITAL, CAPITAL, HWM, max_notional_pct=0.10)
    assert result.notional_usd <= CAPITAL * 0.10 + 1e-3
