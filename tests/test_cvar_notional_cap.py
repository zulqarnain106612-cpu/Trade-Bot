"""
Wiring tests for the CVaR notional ceiling.

risk_quantification.py was headed "NOT wired into live signal path, blocked
on API key provisioning" — but value_at_risk() needs only a returns array,
so that blocker never applied to it. Kelly sizes from win probability and
payoff ratio and is blind to tail shape; this is the ceiling that is not.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import numpy as np
import pandas as pd
import pytest

from src.intelligence.risk_quantification import RiskQuantifier


def _engine(limit_pct, *, lookback: int = 250, confidence: float = 0.95):
    from src.engine.signal_engine import SignalEngine

    engine = object.__new__(SignalEngine)
    cfg = MagicMock()
    cfg.risk.cvar_limit_pct = limit_pct
    cfg.risk.cvar_lookback_bars = lookback
    cfg.risk.cvar_confidence = confidence
    engine._cfg = cfg
    engine._risk_quantifier = RiskQuantifier()
    engine._log = MagicMock()
    return engine


def _bars(returns: np.ndarray) -> pd.DataFrame:
    closes = 100.0 * np.cumprod(1.0 + returns)
    return pd.DataFrame({"close": np.concatenate([[100.0], closes])})


def _normalish(n: int = 400, scale: float = 0.01, seed: int = 7) -> np.ndarray:
    return np.random.RandomState(seed).normal(0.0, scale, n)


def _fat_tailed(n: int = 400, seed: int = 7) -> np.ndarray:
    """Same body, much heavier tail — a t(3) rescaled to a similar sigma."""
    rng = np.random.RandomState(seed)
    return rng.standard_t(3, n) * 0.006


class TestDisabled:
    def test_no_limit_configured_publishes_no_ceiling(self) -> None:
        """The behaviour that existed before the setting did."""
        engine = _engine(None)
        assert engine._cvar_notional_cap(_bars(_normalish()), 10_000.0) is None


class TestCeiling:
    def test_a_ceiling_is_produced_from_real_history(self) -> None:
        engine = _engine(0.02)
        cap = engine._cvar_notional_cap(_bars(_normalish()), 10_000.0)
        assert cap is not None
        assert cap > 0.0

    def test_a_tighter_budget_gives_a_smaller_ceiling(self) -> None:
        bars = _bars(_normalish())
        loose = _engine(0.04)._cvar_notional_cap(bars, 10_000.0)
        tight = _engine(0.01)._cvar_notional_cap(bars, 10_000.0)
        assert loose is not None and tight is not None
        assert tight < loose
        # The budget is linear in the limit: 4x the budget, 4x the notional.
        assert loose == pytest.approx(tight * 4.0, rel=1e-6)

    def test_the_ceiling_scales_with_capital(self) -> None:
        bars = _bars(_normalish())
        small = _engine(0.02)._cvar_notional_cap(bars, 1_000.0)
        large = _engine(0.02)._cvar_notional_cap(bars, 10_000.0)
        assert small is not None and large is not None
        assert large == pytest.approx(small * 10.0, rel=1e-6)

    def test_a_fatter_tail_gives_a_smaller_ceiling(self) -> None:
        """
        The whole point: two series can look similar to Kelly and differ
        entirely in how bad the bad days get.
        """
        thin = _engine(0.02)._cvar_notional_cap(_bars(_normalish(scale=0.006)), 10_000.0)
        fat = _engine(0.02)._cvar_notional_cap(_bars(_fat_tailed()), 10_000.0)
        assert thin is not None and fat is not None
        assert fat < thin


class TestInsufficientOrUselessData:
    def test_too_little_history_publishes_no_ceiling(self) -> None:
        """
        Below ~100 observations the tail quantile is a handful of points and
        says more about the sample than the distribution.
        """
        engine = _engine(0.02)
        assert engine._cvar_notional_cap(_bars(_normalish(n=50)), 10_000.0) is None

    def test_a_lossless_series_publishes_no_ceiling(self) -> None:
        """
        A tail estimate that finds no loss carries no information. That is not
        a licence to size without limit, so no ceiling is published rather
        than an infinite one.
        """
        engine = _engine(0.02)
        assert engine._cvar_notional_cap(_bars(np.full(400, 0.001)), 10_000.0) is None

    def test_a_quantifier_fault_publishes_no_ceiling(self) -> None:
        """A ceiling that cannot be computed must not become a ceiling of zero."""
        engine = _engine(0.02)
        engine._risk_quantifier = MagicMock()
        engine._risk_quantifier.value_at_risk.side_effect = RuntimeError("boom")
        assert engine._cvar_notional_cap(_bars(_normalish()), 10_000.0) is None

    def test_a_non_finite_cvar_publishes_no_ceiling(self) -> None:
        engine = _engine(0.02)
        engine._risk_quantifier = MagicMock()
        engine._risk_quantifier.value_at_risk.return_value = {"cvar": float("nan")}
        assert engine._cvar_notional_cap(_bars(_normalish()), 10_000.0) is None

    def test_missing_close_column_publishes_no_ceiling(self) -> None:
        engine = _engine(0.02)
        assert engine._cvar_notional_cap(pd.DataFrame({"open": [1.0] * 400}), 10_000.0) is None


class TestLookbackWindow:
    def test_only_the_configured_window_is_used(self) -> None:
        """
        A calm recent window must not be diluted by an ancient crash, and a
        recent crash must not be diluted by a long calm history.
        """
        calm = _normalish(n=300, scale=0.002, seed=1)
        crash = np.concatenate([np.full(300, -0.05), calm])
        short_window = _engine(0.02, lookback=200)._cvar_notional_cap(_bars(crash), 10_000.0)
        long_window = _engine(0.02, lookback=5000)._cvar_notional_cap(_bars(crash), 10_000.0)
        assert short_window is not None and long_window is not None
        # The long window still contains the crash; the short one does not.
        assert long_window < short_window
