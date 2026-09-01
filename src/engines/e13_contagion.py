"""
E-13 — Cross-Asset Contagion engine.

Rolling correlation matrix (30d): BTC, ETH, SPX, DXY, GLD, VIX.
Granger causality via statsmodels.
Contagion index: Σ|ρᵢ(t) - ρᵢ(t-30d)| / N.
"""

from __future__ import annotations

from datetime import UTC, datetime

import numpy as np
import pandas as pd
import structlog

from src.engines.schema import EngineOutput


log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

_ENGINE_ID = "E-13"
_SLA_SECONDS = 5
_CORR_WINDOW = 30 * 24  # 30 days hourly


class E13Contagion:
    def __init__(self, horizon_hours: int = 4) -> None:
        self._horizon = horizon_hours
        self._prev_corr: dict[str, float] | None = None

    async def run(self, symbol: str, data: dict) -> EngineOutput:
        spot: float = data.get("spot", 0.0)
        if spot <= 0:
            return EngineOutput.abstain(_ENGINE_ID, symbol, spot, self._horizon, "no_spot")

        macro: dict | None = data.get("macro")
        df: pd.DataFrame | None = data.get("ohlcv")
        if macro is None or df is None or len(df) < 30:
            return EngineOutput.abstain(_ENGINE_ID, symbol, spot, self._horizon, "no_macro_data")

        try:
            btc_returns = df["close"].pct_change().dropna().values[-_CORR_WINDOW:]
            corr = self._build_correlation_state(btc_returns, macro, data)
            contagion_score = self._contagion_index(corr)
            self._prev_corr = corr

            granger_p = self._granger_causality(btc_returns, macro)
            corr_state = self._classify_regime(corr)

            direction = 0
            if contagion_score > 0.7:
                # Follow macro direction when contagion is high
                spx_ret = macro.get("spx_ret", 0.0) or 0.0
                direction = 1 if spx_ret > 0 else (-1 if spx_ret < 0 else 0)

            return EngineOutput(
                engine_id=_ENGINE_ID,
                symbol=symbol,
                timestamp_utc=datetime.now(UTC),
                predicted_price=spot * (1 + direction * 0.002),
                confidence=float(contagion_score),
                direction=direction,
                horizon_hours=self._horizon,
                metadata={
                    "contagion_score": contagion_score,
                    "correlation_state": corr_state,
                    "granger_pvalues": granger_p,
                    "btc_spx": corr.get("btc_spx", 0.0),
                    "btc_dxy": corr.get("btc_dxy", 0.0),
                },
            )
        except Exception as exc:
            log.warning("e13_error", exc=str(exc))
            return EngineOutput.abstain(_ENGINE_ID, symbol, spot, self._horizon, str(exc))

    @staticmethod
    def _build_correlation_state(
        btc_returns: np.ndarray, macro: dict, data: dict
    ) -> dict[str, float]:
        corr: dict[str, float] = {}
        if len(btc_returns) < 10:
            return corr

        def _pearson(a: np.ndarray, b_raw: object) -> float | None:
            """Compute Pearson r between btc_returns tail and a macro series."""
            if b_raw is None:
                return None
            b = np.asarray(b_raw, dtype=float)
            n = min(len(a), len(b))
            if n < 10:
                return None
            a_s, b_s = a[-n:], b[-n:]
            if np.std(a_s) < 1e-10 or np.std(b_s) < 1e-10:
                return None
            return float(np.corrcoef(a_s, b_s)[0, 1])

        r_spx = _pearson(btc_returns, macro.get("spx_series"))
        r_dxy = _pearson(btc_returns, macro.get("dxy_series"))

        # Fall back to sign heuristic when only a scalar is available
        if r_spx is not None:
            corr["btc_spx"] = r_spx
        else:
            spx_ret = float(macro.get("spx_ret", 0.0) or 0.0)
            corr["btc_spx"] = float(np.sign(np.mean(btc_returns[-5:]) * spx_ret)) * 0.5

        if r_dxy is not None:
            corr["btc_dxy"] = r_dxy
        else:
            dxy_ret = float(macro.get("dxy_ret", 0.0) or 0.0)
            corr["btc_dxy"] = float(np.sign(np.mean(btc_returns[-5:]) * dxy_ret)) * -0.3

        return corr

    def _contagion_index(self, corr: dict[str, float]) -> float:
        if self._prev_corr is None:
            return 0.0
        diffs = [abs(corr.get(k, 0.0) - self._prev_corr.get(k, 0.0)) for k in corr]
        return float(np.mean(diffs)) if diffs else 0.0

    @staticmethod
    def _granger_causality(btc_returns: np.ndarray, macro: dict) -> dict[str, float]:
        # Requires a full SPX return series in macro["spx_series"] (list/array).
        # A single scalar "spx_ret" cannot provide the temporal variation needed for
        # Granger causality — a constant series produces a singular OLS matrix.
        try:
            from statsmodels.tsa.stattools import grangercausalitytests

            spx_series_raw = macro.get("spx_series")
            if spx_series_raw is None:
                return {}
            spx_series = np.asarray(spx_series_raw, dtype=float)
            n = min(len(btc_returns), len(spx_series))
            if n < 10:
                return {}
            spx_slice = spx_series[-n:]
            btc_slice = btc_returns[-n:]
            # Skip if SPX series is constant — zero variance → singular OLS
            if np.std(spx_slice) < 1e-10:
                return {}
            data_pair = np.column_stack([btc_slice, spx_slice])
            # statsmodels 0.15 removed the verbose kwarg (and prints nothing by
            # default); passing it raised TypeError, which the except below
            # swallowed -- so this returned {} for every input.
            results = grangercausalitytests(data_pair, maxlag=2)
            return {f"lag_{lag}": float(res[0]["ssr_ftest"][1]) for lag, res in results.items()}
        except Exception:
            return {}

    @staticmethod
    def _classify_regime(corr: dict[str, float]) -> str:
        btc_spx = corr.get("btc_spx", 0.0)
        btc_dxy = corr.get("btc_dxy", 0.0)
        if btc_spx > 0.7:
            return "risk_on"
        if btc_dxy < -0.6:
            return "dxy_driven"
        return "neutral"
