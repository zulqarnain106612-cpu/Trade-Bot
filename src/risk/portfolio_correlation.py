"""
Portfolio Correlation Layer — Gap-005.

Tracks rolling pairwise return correlations between symbols and provides
correlation-adjusted Kelly sizing inputs.

The core problem (Gap-005): Kelly per-symbol ignores cross-asset
correlation.  If BTC/USDT and ETH/USDT both have Kelly fractions of 5%,
and their 30-day return correlation is 0.85, the *portfolio* Kelly fraction
should be much less than 10% — but the existing signal engine adds them
as though they were independent.

This module provides:
  1. PortfolioCorrelationTracker — rolling correlation matrix (EWM-based).
  2. get_avg_correlation_with_book() — average correlation of a new
     signal's symbol with all currently open positions.
  3. correlation_scalar() — multiplicative position-size scalar that
     reduce sizing when the new position is highly correlated with the book.

The scalar is then passed to SignalEngine → compute_position_size() →
correlation_adjusted_notional() (already implemented in position_sizing.py).

Reference: López de Prado (2018) AFML Ch.16 — portfolio construction via
HRP and correlation-adjusted sizing.  Carver (2019) Systematic Trading
Ch.11 — correlation-adjusted position sizing.
"""

from __future__ import annotations

import math
from typing import Final

import structlog


log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

# EWM half-life in bars (30-day at 15-min bars ≈ 2880 bars; use shorter
# window for responsiveness while keeping enough for stable estimates).
_EWM_HALFLIFE: Final[int] = 500  # ~5 days of 15-min bars
_MIN_OBSERVATIONS: Final[int] = 30  # bars needed before reporting correlation
_CORRELATION_REDUCTION_THRESHOLD: Final[float] = 0.60  # match position_sizing.py

# Shrinkage toward 0 (independence) for correlation estimates near the
# _MIN_OBSERVATIONS floor: shrunk_r = raw_r * n / (n + _CORRELATION_SHRINKAGE_K).
# A correlation estimated from 31 bars is noisy and shouldn't drive as large a
# size reduction as one estimated from thousands of bars; shrinkage vanishes
# as n grows (e.g. n=500 -> factor ~0.96, n=30 -> factor ~0.6).
_CORRELATION_SHRINKAGE_K: Final[float] = 20.0


class _EWMSeries:
    """Minimal exponentially weighted mean + variance tracker (no pandas/numpy needed)."""

    def __init__(self, halflife: int) -> None:
        self._alpha = 1.0 - math.exp(-math.log(2) / halflife)
        self._mean: float | None = None
        self._var: float | None = None
        self._cov_partner: dict[str, _EWMCov] = {}
        self._n = 0

    def update(self, value: float) -> None:
        if not math.isfinite(value):
            return
        self._n += 1
        if self._mean is None:
            self._mean = value
            self._var = 0.0
        else:
            delta = value - self._mean
            self._mean += self._alpha * delta
            self._var = (1 - self._alpha) * (self._var + self._alpha * delta**2)

    @property
    def std(self) -> float:
        if self._var is None or self._var < 0:
            return 0.0
        return math.sqrt(self._var)

    @property
    def mean(self) -> float:
        return self._mean if self._mean is not None else 0.0

    @property
    def n(self) -> int:
        return self._n


class _EWMCov:
    """Pairwise EWM covariance tracker."""

    def __init__(self, halflife: int) -> None:
        self._alpha = 1.0 - math.exp(-math.log(2) / halflife)
        self._cov: float | None = None
        self._mean_x: float | None = None
        self._mean_y: float | None = None

    def update(self, x: float, y: float) -> None:
        if not (math.isfinite(x) and math.isfinite(y)):
            return
        if self._mean_x is None:
            self._mean_x = x
            self._mean_y = y
            self._cov = 0.0
        else:
            dx = x - self._mean_x
            dy = y - self._mean_y
            self._cov = (1 - self._alpha) * (self._cov + self._alpha * dx * dy)
            self._mean_x += self._alpha * dx
            self._mean_y += self._alpha * dy

    @property
    def cov(self) -> float:
        return self._cov if self._cov is not None else 0.0


class PortfolioCorrelationTracker:
    """
    Tracks EWM rolling correlations between all pairs of tracked symbols.

    Usage:
        tracker = PortfolioCorrelationTracker()
        # Each bar, call push_return() for each symbol:
        tracker.push_return("BTC/USDT", 0.0012)
        tracker.push_return("ETH/USDT", 0.0009)
        # Before opening a new BTC position:
        avg_corr = tracker.avg_correlation_with_open_positions(
            new_symbol="BTC/USDT",
            open_symbols=["ETH/USDT"],
        )
    """

    def __init__(self, halflife: int = _EWM_HALFLIFE) -> None:
        self._halflife = halflife
        self._series: dict[str, _EWMSeries] = {}
        self._covs: dict[tuple[str, str], _EWMCov] = {}

    def _cov_key(self, a: str, b: str) -> tuple[str, str]:
        """Canonical key — always lexicographically ordered."""
        return (a, b) if a <= b else (b, a)

    def push_return(self, symbol: str, ret: float) -> None:
        """
        Record one bar return for a symbol.

        ret should be a simple arithmetic return: (close_t - close_{t-1}) / close_{t-1}.
        Must be called for all tracked symbols each bar to keep the series in sync.
        """
        if symbol not in self._series:
            self._series[symbol] = _EWMSeries(self._halflife)
        self._series[symbol].update(ret)

        # Update pairwise covariances with all other symbols
        for other in self._series:
            if other == symbol:
                continue
            key = self._cov_key(symbol, other)
            if key not in self._covs:
                self._covs[key] = _EWMCov(self._halflife)
            # NOTE: push_return() only sees one symbol's return at a time, so
            # it cannot pair true same-bar returns like push_bar_returns()
            # does. As an approximation, pair this bar's return against the
            # other symbol's current EWM mean (its expected return) rather
            # than leaving the covariance tracker permanently un-updated —
            # the previous code looked up self._covs[key] and discarded the
            # result, so push_return() never actually recorded any
            # covariance and correlation() always saw cov=0.
            self._covs[key].update(ret, self._series[other].mean)

    def push_bar_returns(self, returns: dict[str, float]) -> None:
        """
        Preferred API: push all symbol returns for one bar atomically.

        Args:
            returns: {symbol: arithmetic_return} for all tracked symbols.
        """
        # First pass: update all univariate series
        for symbol, ret in returns.items():
            if symbol not in self._series:
                self._series[symbol] = _EWMSeries(self._halflife)
            self._series[symbol].update(ret)

        # Second pass: update all pairwise covariances
        symbols = list(returns.keys())
        for i, sym_a in enumerate(symbols):
            for sym_b in symbols[i + 1 :]:
                key = self._cov_key(sym_a, sym_b)
                if key not in self._covs:
                    self._covs[key] = _EWMCov(self._halflife)
                self._covs[key].update(returns[sym_a], returns[sym_b])

    def correlation(self, sym_a: str, sym_b: str) -> float | None:
        """
        EWM Pearson correlation between sym_a and sym_b.

        Returns None if either series has fewer than MIN_OBSERVATIONS bars.
        Returns a float in [-1, 1].
        """
        if sym_a == sym_b:
            return 1.0

        s_a = self._series.get(sym_a)
        s_b = self._series.get(sym_b)
        if s_a is None or s_b is None:
            return None
        if s_a.n < _MIN_OBSERVATIONS or s_b.n < _MIN_OBSERVATIONS:
            return None

        key = self._cov_key(sym_a, sym_b)
        cov_tracker = self._covs.get(key)
        if cov_tracker is None:
            return None

        std_a = s_a.std
        std_b = s_b.std
        denom = std_a * std_b
        if denom < 1e-12:
            return 0.0

        r = cov_tracker.cov / denom
        # Clamp to [-1, 1] to guard against floating-point imprecision
        r = max(-1.0, min(1.0, r))

        # Shrink toward 0 based on the smaller series' sample size — see
        # _CORRELATION_SHRINKAGE_K.
        n_eff = min(s_a.n, s_b.n)
        shrink_factor = n_eff / (n_eff + _CORRELATION_SHRINKAGE_K)
        return r * shrink_factor

    def avg_correlation_with_open_positions(
        self,
        new_symbol: str,
        open_symbols: list[str],
    ) -> float:
        """
        Average EWM correlation of new_symbol with all open position symbols.

        If no correlation data is available (< MIN_OBSERVATIONS bars), returns
        0.0 (assumes independent — conservative; allows sizing to proceed at
        full Kelly).

        Args:
            new_symbol:   Symbol about to be opened.
            open_symbols: Symbols of currently open positions.

        Returns:
            float in [0, 1].  Negative correlations are clamped to 0 (a hedge
            does not *increase* Kelly sizing in this model; it's a separate
            decision).
        """
        if not open_symbols:
            return 0.0

        correlations: list[float] = []
        for sym in open_symbols:
            r = self.correlation(new_symbol, sym)
            if r is not None:
                correlations.append(max(0.0, r))  # clamp negatives to 0

        if not correlations:
            log.debug(
                "portfolio_correlation.insufficient_data",
                new_symbol=new_symbol,
                open_symbols=open_symbols,
                note="returning 0.0 (assumes independent)",
            )
            return 0.0

        avg = sum(correlations) / len(correlations)
        log.debug(
            "portfolio_correlation.avg",
            new_symbol=new_symbol,
            open_symbols=open_symbols,
            avg_correlation=round(avg, 4),
        )
        return avg

    def correlation_scalar(
        self,
        new_symbol: str,
        open_symbols: list[str],
        threshold: float = _CORRELATION_REDUCTION_THRESHOLD,
    ) -> float:
        """
        Position-size scalar [0, 1] for new_symbol given open positions.

        Returns 1.0 when avg_correlation <= threshold (no reduction needed).
        Linearly reduces to 0.0 as avg_correlation approaches 1.0.

        This scalar feeds directly into position_sizing.correlation_adjusted_notional().

        Reference: Carver (2019) Systematic Trading Ch.11 — position sizing
        under correlation.
        """
        avg_corr = self.avg_correlation_with_open_positions(new_symbol, open_symbols)
        if avg_corr <= threshold:
            return 1.0
        # Linear reduction: 1.0 at threshold, 0.0 at correlation=1.0
        scalar = (1.0 - avg_corr) / (1.0 - threshold)
        return max(0.0, round(scalar, 4))

    def correlation_matrix(self) -> dict[tuple[str, str], float | None]:
        """Full pairwise correlation matrix for all tracked symbols."""
        symbols = sorted(self._series.keys())
        result: dict[tuple[str, str], float | None] = {}
        for i, sym_a in enumerate(symbols):
            for sym_b in symbols[i + 1 :]:
                result[(sym_a, sym_b)] = self.correlation(sym_a, sym_b)
        return result

    @property
    def tracked_symbols(self) -> list[str]:
        return list(self._series.keys())


# ---------------------------------------------------------------------------
# Module-level singleton (used by orchestrator + signal engine)
# ---------------------------------------------------------------------------

_portfolio_correlation: PortfolioCorrelationTracker = PortfolioCorrelationTracker()


def get_portfolio_correlation() -> PortfolioCorrelationTracker:
    """Module-level singleton for the portfolio correlation tracker."""
    return _portfolio_correlation
