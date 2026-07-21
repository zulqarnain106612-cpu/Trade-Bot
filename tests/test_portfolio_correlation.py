"""
Tests for src/risk/portfolio_correlation.py

Covers: _EWMSeries, _EWMCov, PortfolioCorrelationTracker, get_portfolio_correlation
Target: lift coverage from 19% → ≥ 70% on this module
"""

from __future__ import annotations

from src.risk.portfolio_correlation import (
    _CORRELATION_REDUCTION_THRESHOLD,
    _CORRELATION_SHRINKAGE_K,
    _EWM_HALFLIFE,
    _MIN_OBSERVATIONS,
    PortfolioCorrelationTracker,
    _EWMCov,
    _EWMSeries,
    get_portfolio_correlation,
)


# ---------------------------------------------------------------------------
# _EWMSeries
# ---------------------------------------------------------------------------


class TestEWMSeries:
    def test_initial_state(self):
        s = _EWMSeries(halflife=10)
        assert s.n == 0
        assert s.std == 0.0

    def test_update_once(self):
        s = _EWMSeries(halflife=10)
        s.update(1.0)
        assert s.n == 1
        assert s.std == 0.0  # variance initialised to 0 on first observation

    def test_update_multiple_increments_n(self):
        s = _EWMSeries(halflife=10)
        for v in [0.01, -0.02, 0.015, -0.005, 0.03]:
            s.update(v)
        assert s.n == 5

    def test_std_positive_after_variance_builds(self):
        s = _EWMSeries(halflife=10)
        for i in range(50):
            s.update(0.01 if i % 2 == 0 else -0.01)
        assert s.std > 0.0

    def test_ignores_non_finite(self):
        s = _EWMSeries(halflife=10)
        s.update(float("nan"))
        s.update(float("inf"))
        assert s.n == 0

    def test_finite_after_non_finite_ignored(self):
        s = _EWMSeries(halflife=10)
        s.update(float("nan"))
        s.update(0.01)
        assert s.n == 1


# ---------------------------------------------------------------------------
# _EWMCov
# ---------------------------------------------------------------------------


class TestEWMCov:
    def test_initial_cov_zero(self):
        c = _EWMCov(halflife=10)
        assert c.cov == 0.0

    def test_update_once_sets_zero_cov(self):
        c = _EWMCov(halflife=10)
        c.update(0.01, 0.02)
        assert c.cov == 0.0  # initialised to 0 on first observation

    def test_update_multiple_positive_cov_for_correlated(self):
        c = _EWMCov(halflife=20)
        # Positively correlated pairs
        for _ in range(60):
            c.update(0.01, 0.01)
        assert c.cov >= 0.0

    def test_ignores_non_finite(self):
        c = _EWMCov(halflife=10)
        c.update(float("nan"), 0.01)
        assert c.cov == 0.0
        c.update(0.01, float("inf"))
        assert c.cov == 0.0


# ---------------------------------------------------------------------------
# PortfolioCorrelationTracker — push_bar_returns
# ---------------------------------------------------------------------------


class TestPushBarReturns:
    def _tracker_with_history(self, n: int = 60, corr: float = 1.0) -> PortfolioCorrelationTracker:
        """Return a tracker with n bars of BTC/ETH with given target correlation."""
        t = PortfolioCorrelationTracker(halflife=_EWM_HALFLIFE)
        import random

        rng = random.Random(42)
        for _ in range(n):
            btc = rng.gauss(0, 0.01)
            eth = btc * corr + rng.gauss(0, 0.001) * (1 - abs(corr))
            t.push_bar_returns({"BTC/USDT": btc, "ETH/USDT": eth})
        return t

    def test_tracked_symbols_after_push(self):
        t = PortfolioCorrelationTracker()
        t.push_bar_returns({"BTC/USDT": 0.01, "ETH/USDT": 0.009})
        assert "BTC/USDT" in t.tracked_symbols
        assert "ETH/USDT" in t.tracked_symbols

    def test_single_symbol_no_crash(self):
        t = PortfolioCorrelationTracker()
        for _ in range(40):
            t.push_bar_returns({"BTC/USDT": 0.01})
        # No crash; no covariance partner
        assert t.correlation("BTC/USDT", "BTC/USDT") == 1.0

    def test_insufficient_data_returns_none(self):
        t = PortfolioCorrelationTracker()
        for _ in range(_MIN_OBSERVATIONS - 1):
            t.push_bar_returns({"BTC/USDT": 0.01, "ETH/USDT": 0.01})
        assert t.correlation("BTC/USDT", "ETH/USDT") is None

    def test_sufficient_data_returns_float(self):
        t = self._tracker_with_history(n=60)
        r = t.correlation("BTC/USDT", "ETH/USDT")
        assert r is not None
        assert -1.0 <= r <= 1.0

    def test_high_positive_correlation_detected(self):
        t = self._tracker_with_history(n=200, corr=1.0)
        r = t.correlation("BTC/USDT", "ETH/USDT")
        assert r is not None
        assert r > 0.7  # highly correlated pairs should register > 0.7

    def test_push_bar_returns_empty_dict(self):
        t = PortfolioCorrelationTracker()
        t.push_bar_returns({})  # should not crash


# ---------------------------------------------------------------------------
# push_return (sequential API)
# ---------------------------------------------------------------------------


class TestPushReturn:
    def test_push_return_no_crash(self):
        t = PortfolioCorrelationTracker()
        t.push_return("BTC/USDT", 0.01)
        t.push_return("ETH/USDT", 0.009)
        assert "BTC/USDT" in t.tracked_symbols

    def test_push_return_multiple_symbols(self):
        t = PortfolioCorrelationTracker()
        for _ in range(5):
            t.push_return("BTC/USDT", 0.01)
            t.push_return("ETH/USDT", 0.01)
        assert len(t.tracked_symbols) == 2


# ---------------------------------------------------------------------------
# correlation()
# ---------------------------------------------------------------------------


class TestCorrelation:
    def test_self_correlation(self):
        t = PortfolioCorrelationTracker()
        assert t.correlation("BTC/USDT", "BTC/USDT") == 1.0

    def test_unknown_symbols_returns_none(self):
        t = PortfolioCorrelationTracker()
        assert t.correlation("BTC/USDT", "ETH/USDT") is None

    def test_one_known_one_unknown_returns_none(self):
        t = PortfolioCorrelationTracker()
        for _ in range(40):
            t.push_bar_returns({"BTC/USDT": 0.01})
        assert t.correlation("BTC/USDT", "ETH/USDT") is None

    def test_result_clamped_to_minus_one_one(self):
        """Floating-point edge: correlation must be in [-1, 1]."""
        t = PortfolioCorrelationTracker(halflife=10)
        for _ in range(60):
            t.push_bar_returns({"A": 0.01, "B": 0.01})
        r = t.correlation("A", "B")
        if r is not None:
            assert -1.0 <= r <= 1.0

    def test_zero_variance_returns_zero_correlation(self):
        """When std of one series is ~0, correlation should return 0.0 not crash."""
        t = PortfolioCorrelationTracker(halflife=10)
        for _ in range(60):
            t.push_bar_returns({"STABLE": 0.0, "VOLATILE": 0.01 if _ % 2 == 0 else -0.01})
        r = t.correlation("STABLE", "VOLATILE")
        # Either None (insufficient variance) or 0.0
        assert r is None or r == 0.0

    def test_correlation_none_when_symbols_never_pushed_together(self):
        """Each symbol individually has enough observations, but they were
        never in the same push_bar_returns() call together -- no covariance
        tracker key exists for the pair, so correlation() must return None
        rather than KeyError."""
        t = PortfolioCorrelationTracker(halflife=10)
        for i in range(_MIN_OBSERVATIONS + 5):
            t.push_bar_returns({"ALONE_A": 0.01 * (i % 3 - 1)})
        for i in range(_MIN_OBSERVATIONS + 5):
            t.push_bar_returns({"ALONE_B": 0.02 * (i % 3 - 1)})
        assert t.correlation("ALONE_A", "ALONE_B") is None

    def test_correlation_shrunk_near_min_observations(self):
        """
        Just past _MIN_OBSERVATIONS, a near-perfectly-correlated pair should
        report a correlation well below the raw ~1.0 due to sample-size
        shrinkage (n / (n + _CORRELATION_SHRINKAGE_K)).
        """
        t = PortfolioCorrelationTracker(halflife=_EWM_HALFLIFE)
        import random

        rng = random.Random(3)
        for _ in range(_MIN_OBSERVATIONS + 1):
            btc = rng.gauss(0, 0.01)
            t.push_bar_returns({"BTC/USDT": btc, "ETH/USDT": btc})

        r = t.correlation("BTC/USDT", "ETH/USDT")
        assert r is not None
        n = _MIN_OBSERVATIONS + 1
        expected_shrink_factor = n / (n + _CORRELATION_SHRINKAGE_K)
        assert r <= expected_shrink_factor + 1e-9

    def test_correlation_shrinkage_vanishes_at_large_sample(self):
        """With thousands of bars, shrinkage should have negligible effect."""
        t = PortfolioCorrelationTracker(halflife=_EWM_HALFLIFE)
        import random

        rng = random.Random(3)
        for _ in range(3000):
            btc = rng.gauss(0, 0.01)
            t.push_bar_returns({"BTC/USDT": btc, "ETH/USDT": btc})

        r = t.correlation("BTC/USDT", "ETH/USDT")
        assert r is not None
        assert r > 0.99

    def test_self_correlation_not_shrunk(self):
        """Self-correlation is always exactly 1.0, regardless of sample size."""
        t = PortfolioCorrelationTracker()
        for _ in range(_MIN_OBSERVATIONS + 5):
            t.push_bar_returns({"BTC/USDT": 0.01})
        assert t.correlation("BTC/USDT", "BTC/USDT") == 1.0


# ---------------------------------------------------------------------------
# avg_correlation_with_open_positions()
# ---------------------------------------------------------------------------


class TestAvgCorrelation:
    def _correlated_tracker(self, n: int = 200) -> PortfolioCorrelationTracker:
        t = PortfolioCorrelationTracker(halflife=_EWM_HALFLIFE)
        import random

        rng = random.Random(0)
        for _ in range(n):
            btc = rng.gauss(0, 0.01)
            t.push_bar_returns(
                {
                    "BTC/USDT": btc,
                    "ETH/USDT": btc + rng.gauss(0, 0.0005),
                    "SOL/USDT": rng.gauss(0, 0.01),  # independent
                }
            )
        return t

    def test_empty_open_positions_returns_zero(self):
        t = PortfolioCorrelationTracker()
        assert t.avg_correlation_with_open_positions("BTC/USDT", []) == 0.0

    def test_no_data_returns_zero(self):
        t = PortfolioCorrelationTracker()
        result = t.avg_correlation_with_open_positions("BTC/USDT", ["ETH/USDT"])
        assert result == 0.0

    def test_avg_correlation_is_nonneg(self):
        t = self._correlated_tracker()
        avg = t.avg_correlation_with_open_positions("BTC/USDT", ["ETH/USDT"])
        assert avg >= 0.0

    def test_avg_correlation_high_for_correlated_pair(self):
        t = self._correlated_tracker(n=300)
        avg = t.avg_correlation_with_open_positions("BTC/USDT", ["ETH/USDT"])
        assert avg > 0.5

    def test_avg_correlation_multiple_open(self):
        t = self._correlated_tracker(n=300)
        avg = t.avg_correlation_with_open_positions("BTC/USDT", ["ETH/USDT", "SOL/USDT"])
        assert 0.0 <= avg <= 1.0

    def test_negative_correlations_clamped_to_zero(self):
        """Negative correlations (hedges) should not reduce avg below 0."""
        t = PortfolioCorrelationTracker(halflife=_EWM_HALFLIFE)
        import random

        rng = random.Random(7)
        for _ in range(200):
            btc = rng.gauss(0, 0.01)
            t.push_bar_returns({"BTC/USDT": btc, "INVERSE": -btc})
        avg = t.avg_correlation_with_open_positions("BTC/USDT", ["INVERSE"])
        assert avg >= 0.0


# ---------------------------------------------------------------------------
# correlation_scalar()
# ---------------------------------------------------------------------------


class TestCorrelationScalar:
    def _correlated_tracker(self, n: int = 300) -> PortfolioCorrelationTracker:
        t = PortfolioCorrelationTracker(halflife=_EWM_HALFLIFE)
        import random

        rng = random.Random(1)
        for _ in range(n):
            btc = rng.gauss(0, 0.01)
            t.push_bar_returns(
                {
                    "BTC/USDT": btc,
                    "ETH/USDT": btc + rng.gauss(0, 0.0002),
                }
            )
        return t

    def test_no_open_positions_scalar_is_one(self):
        t = PortfolioCorrelationTracker()
        assert t.correlation_scalar("BTC/USDT", []) == 1.0

    def test_no_data_scalar_is_one(self):
        t = PortfolioCorrelationTracker()
        assert t.correlation_scalar("BTC/USDT", ["ETH/USDT"]) == 1.0

    def test_scalar_in_zero_one_range(self):
        t = self._correlated_tracker()
        s = t.correlation_scalar("BTC/USDT", ["ETH/USDT"])
        assert 0.0 <= s <= 1.0

    def test_highly_correlated_reduces_scalar_below_one(self):
        t = self._correlated_tracker(n=500)
        s = t.correlation_scalar("BTC/USDT", ["ETH/USDT"])
        # With near-perfect correlation, scalar should be reduced
        avg = t.avg_correlation_with_open_positions("BTC/USDT", ["ETH/USDT"])
        if avg > _CORRELATION_REDUCTION_THRESHOLD:
            assert s < 1.0

    def test_low_correlation_scalar_is_one(self):
        t = PortfolioCorrelationTracker(halflife=_EWM_HALFLIFE)
        import random

        rng = random.Random(99)
        for _ in range(300):
            t.push_bar_returns(
                {
                    "BTC/USDT": rng.gauss(0, 0.01),
                    "GOLD": rng.gauss(0, 0.005),
                }
            )
        s = t.correlation_scalar("BTC/USDT", ["GOLD"])
        avg = t.avg_correlation_with_open_positions("BTC/USDT", ["GOLD"])
        if avg <= _CORRELATION_REDUCTION_THRESHOLD:
            assert s == 1.0

    def test_scalar_zero_floor(self):
        """Scalar should never go below 0."""
        t = self._correlated_tracker(n=500)
        s = t.correlation_scalar("BTC/USDT", ["ETH/USDT"], threshold=0.0)
        assert s >= 0.0


# ---------------------------------------------------------------------------
# correlation_matrix()
# ---------------------------------------------------------------------------


class TestCorrelationMatrix:
    def test_empty_tracker_empty_matrix(self):
        t = PortfolioCorrelationTracker()
        assert t.correlation_matrix() == {}

    def test_single_symbol_empty_matrix(self):
        t = PortfolioCorrelationTracker()
        for _ in range(40):
            t.push_bar_returns({"BTC/USDT": 0.01})
        assert t.correlation_matrix() == {}

    def test_two_symbols_one_pair(self):
        t = PortfolioCorrelationTracker()
        for _ in range(40):
            t.push_bar_returns({"BTC/USDT": 0.01, "ETH/USDT": 0.009})
        matrix = t.correlation_matrix()
        assert len(matrix) == 1

    def test_three_symbols_three_pairs(self):
        t = PortfolioCorrelationTracker()
        for _ in range(40):
            t.push_bar_returns({"A": 0.01, "B": 0.009, "C": 0.008})
        matrix = t.correlation_matrix()
        assert len(matrix) == 3

    def test_matrix_keys_are_sorted_pairs(self):
        t = PortfolioCorrelationTracker()
        for _ in range(40):
            t.push_bar_returns({"BTC/USDT": 0.01, "ETH/USDT": 0.009})
        matrix = t.correlation_matrix()
        for a, b in matrix:
            assert a <= b  # lexicographic canonical order


# ---------------------------------------------------------------------------
# get_portfolio_correlation singleton
# ---------------------------------------------------------------------------


class TestSingleton:
    def test_returns_same_instance(self):
        t1 = get_portfolio_correlation()
        t2 = get_portfolio_correlation()
        assert t1 is t2

    def test_is_portfolio_correlation_tracker(self):
        assert isinstance(get_portfolio_correlation(), PortfolioCorrelationTracker)
