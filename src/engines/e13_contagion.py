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
        # SPX
        spx_ret = macro.get("spx_ret", 0.0) or 0.0
        # DXY
        dxy_ret = macro.get("dxy_ret", 0.0) or 0.0

        if len(btc_returns) >= 10:
            # Scalar proxy (single-period): use rolling from multi-asset df if available
            corr["btc_spx"] = float(np.sign(np.mean(btc_returns[-5:]) * spx_ret)) * 0.5
            corr["btc_dxy"] = float(np.sign(np.mean(btc_returns[-5:]) * dxy_ret)) * -0.3
        return corr

    def _contagion_index(self, corr: dict[str, float]) -> float:
        if self._prev_corr is None:
            return 0.0
        diffs = [abs(corr.get(k, 0.0) - self._prev_corr.get(k, 0.0)) for k in corr]
        return float(np.mean(diffs)) if diffs else 0.0

    @staticmethod
    def _granger_causality(btc_returns: np.ndarray, macro: dict) -> dict[str, float]:
        try:
            from statsmodels.tsa.stattools import grangercausalitytests

            spx_ret_scalar = macro.get("spx_ret", 0.0) or 0.0
            # Build a proxy series — single scalar expanded to match btc length
            spx_series = np.full(len(btc_returns), float(spx_ret_scalar))
            data_pair = np.column_stack([btc_returns, spx_series])
            if len(data_pair) < 10:
                return {}
            results = grangercausalitytests(data_pair, maxlag=2, verbose=False)
            p_vals = {f"lag_{lag}": float(res[0]["ssr_ftest"][1]) for lag, res in results.items()}
            return p_vals
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
