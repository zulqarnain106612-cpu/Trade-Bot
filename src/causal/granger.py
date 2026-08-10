"""
Rolling Granger causality — BTC → ALT causal edge detection.

Tests whether past values of BTC returns help predict ALT returns beyond
the ALT's own history. A significant F-test p-value (< 0.05) means BTC
Granger-causes the ALT, implying BTC price moves lead ALT moves.

Uses statsmodels `grangercausalitytests` on a rolling window.

Output:
  granger_btc_to_alt: dict mapping alt_symbol → is_caused_by_btc (bool)
  lead_lag_seconds:   dict mapping alt_symbol → estimated lead in bars
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd
import structlog


log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

_MAX_LAG = 5  # test lags 1..5 bars
_PVALUE_THRESHOLD = 0.05
_MIN_WINDOW = 60  # minimum observations for reliable test


@dataclass
class GrangerResult:
    treatment: str  # e.g. "BTC"
    outcome: str  # e.g. "ETH"
    is_causal: bool  # True if BTC Granger-causes ALT
    min_pvalue: float  # minimum p-value across all tested lags
    best_lag: int  # lag with lowest p-value
    f_stat: float  # F-statistic at best lag


class GrangerCausalityDetector:
    """
    Rolling Granger causality detector for BTC → ALT pairs.

    Maintains a rolling window of price returns and re-runs the test on each
    new bar close.  Results are cached and updated when the window shifts.
    """

    def __init__(self, window: int = 120, max_lag: int = _MAX_LAG) -> None:
        self._window = window
        self._max_lag = max_lag
        self._results: dict[str, GrangerResult] = {}

    def update(
        self,
        btc_returns: pd.Series,
        alt_prices: dict[str, pd.Series],
    ) -> dict[str, GrangerResult]:
        """
        Update Granger tests with new return data.

        btc_returns: pd.Series of BTC log-returns
        alt_prices:  dict symbol → pd.Series of prices

        Returns dict of GrangerResult per alt symbol.
        """
        if len(btc_returns) < _MIN_WINDOW:
            return self._results

        try:
            from statsmodels.tsa.stattools import grangercausalitytests
        except ImportError:
            log.warning("statsmodels_not_available_granger_disabled")
            return self._results

        btc_ret = btc_returns.dropna().iloc[-self._window :]

        for symbol, prices in alt_prices.items():
            try:
                alt_ret = np.log(prices / prices.shift(1)).dropna().iloc[-self._window :]
                n = min(len(btc_ret), len(alt_ret))
                if n < _MIN_WINDOW:
                    continue

                df = pd.DataFrame(
                    {"alt": alt_ret.values[-n:], "btc": btc_ret.values[-n:]},
                    dtype=float,
                )
                df = df.dropna()
                if len(df) < _MIN_WINDOW:
                    continue

                test_result = grangercausalitytests(
                    df[["alt", "btc"]], maxlag=self._max_lag, verbose=False
                )

                best_pval = 1.0
                best_lag = 1
                best_fstat = 0.0
                for lag in range(1, self._max_lag + 1):
                    pval = test_result[lag][0]["ssr_ftest"][1]  # F-test p-value
                    fstat = test_result[lag][0]["ssr_ftest"][0]
                    if pval < best_pval:
                        best_pval = float(pval)
                        best_lag = lag
                        best_fstat = float(fstat)

                self._results[symbol] = GrangerResult(
                    treatment="BTC",
                    outcome=symbol,
                    is_causal=best_pval < _PVALUE_THRESHOLD,
                    min_pvalue=best_pval,
                    best_lag=best_lag,
                    f_stat=best_fstat,
                )
            except Exception as exc:
                log.debug("granger_test_failed", symbol=symbol, exc=str(exc))

        return self._results

    @property
    def causal_symbols(self) -> list[str]:
        """List of ALT symbols that BTC Granger-causes."""
        return [s for s, r in self._results.items() if r.is_causal]

    def to_feature_vector(self) -> dict[str, float]:
        """
        Convert current results to a flat feature dict.

        E.g. {"granger_btc_to_ETH": 1.0, "granger_lag_ETH": 2.0, ...}
        """
        features: dict[str, float] = {}
        for symbol, result in self._results.items():
            features[f"granger_btc_to_{symbol}"] = float(result.is_causal)
            features[f"granger_lag_{symbol}"] = float(result.best_lag)
            features[f"granger_fstat_{symbol}"] = result.f_stat
        return features
