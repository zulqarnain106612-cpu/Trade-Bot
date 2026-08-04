"""
E-12 — Options Market Signal engine.

Inputs: Deribit options chain for BTC/ETH.
Gap G-05: LTC/XMR → returns None; consensus redistributes weight to E-01.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pandas as pd
import structlog

from src.engines.schema import EngineOutput


log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

_ENGINE_ID = "E-12"
_SLA_SECONDS = 5
_SUPPORTED_COINS = {"BTC", "ETH"}


def compute_gex(options_df: pd.DataFrame, spot: float) -> float:
    """Gamma exposure: dealers stabilize (>0) or amplify (<0) moves."""
    contract_size = 1.0  # BTC: 1 BTC/contract; ETH: 1 ETH
    gex = 0.0
    for _, row in options_df.iterrows():
        gamma = float(row.get("gamma", 0.0))
        oi = float(row.get("oi", 0.0))
        sign = 1 if row.get("option_type") == "call" else -1
        gex += sign * gamma * oi * contract_size * spot**2 / 100
    return gex


def put_call_ratio(options_df: pd.DataFrame) -> float:
    put_oi = options_df[options_df["option_type"] == "put"]["oi"].sum()
    call_oi = options_df[options_df["option_type"] == "call"]["oi"].sum()
    return float(put_oi / max(call_oi, 1.0))


def iv_skew(options_df: pd.DataFrame) -> float:
    """OTM put IV - OTM call IV at same delta (~0.25)."""
    puts = options_df[
        (options_df["option_type"] == "put") & (options_df["delta"].abs().between(0.2, 0.3))
    ]
    calls = options_df[
        (options_df["option_type"] == "call") & (options_df["delta"].abs().between(0.2, 0.3))
    ]
    if puts.empty or calls.empty:
        return 0.0
    return float(puts["iv"].mean() - calls["iv"].mean())


def max_pain(options_df: pd.DataFrame) -> float:
    """Strike where aggregate option value destruction is maximum."""
    strikes = options_df["strike"].unique()
    if len(strikes) == 0:
        return 0.0
    pain: dict[float, float] = {}
    for s in strikes:
        total = 0.0
        for _, row in options_df.iterrows():
            k = float(row["strike"])
            oi = float(row.get("oi", 0.0))
            if row["option_type"] == "call":
                total += max(0.0, s - k) * oi
            else:
                total += max(0.0, k - s) * oi
        pain[s] = total
    return float(min(pain, key=lambda x: pain[x]))


class E12Options:
    def __init__(self, horizon_hours: int = 4) -> None:
        self._horizon = horizon_hours
        self._max_hist_skew: float = 0.1

    async def run(self, symbol: str, data: dict) -> EngineOutput:
        spot: float = data.get("spot", 0.0)
        coin = symbol.split("/")[0].upper()

        # Gap G-05: unsupported coins abstain
        if coin not in _SUPPORTED_COINS:
            return EngineOutput.abstain(
                _ENGINE_ID, symbol, spot, self._horizon, "no_options_market"
            )

        options_df: pd.DataFrame | None = data.get("options")
        if options_df is None or options_df.empty or spot <= 0:
            return EngineOutput.abstain(_ENGINE_ID, symbol, spot, self._horizon, "no_options_data")

        try:
            pc = put_call_ratio(options_df)
            skew = iv_skew(options_df)
            gex = compute_gex(options_df, spot)
            mp = max_pain(options_df)

            # Update historical max skew for normalization
            self._max_hist_skew = max(self._max_hist_skew, abs(skew))
            confidence = float(abs(skew) / (self._max_hist_skew + 1e-9))

            direction = 1 if pc < 0.8 else (-1 if pc > 1.2 else 0)

            return EngineOutput(
                engine_id=_ENGINE_ID,
                symbol=symbol,
                timestamp_utc=datetime.now(UTC),
                predicted_price=spot,
                confidence=min(confidence, 1.0),
                direction=direction,
                horizon_hours=self._horizon,
                metadata={
                    "pc_ratio": pc,
                    "iv_skew": skew,
                    "gex": gex,
                    "max_pain_level": mp,
                },
            )
        except Exception as exc:
            log.warning("e12_error", exc=str(exc))
            return EngineOutput.abstain(_ENGINE_ID, symbol, spot, self._horizon, str(exc))
