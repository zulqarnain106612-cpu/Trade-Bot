"""
E-17 — Liquidity Stress engine.

Kyle lambda, Amihud ratio, orderbook depth score.
"""

from __future__ import annotations

from datetime import UTC, datetime

import numpy as np
import pandas as pd
import structlog

from src.engines.schema import EngineOutput


log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

_ENGINE_ID = "E-17"
_SLA_SECONDS = 5
_DEPTH_BPS = 50  # basis points for depth score


def kyle_lambda(price_changes: np.ndarray, signed_volumes: np.ndarray) -> float:
    """Kyle (1985) price impact coefficient."""
    n = min(len(price_changes), len(signed_volumes))
    if n < 5:
        return 0.0
    pc = price_changes[:n]
    sv = signed_volumes[:n]
    cov = np.cov(pc, sv)[0, 1]
    var = np.var(sv)
    return float(cov / max(var, 1e-12))


def amihud_ratio(returns: np.ndarray, volumes: np.ndarray, window: int = 20) -> float:
    """Amihud (2002) illiquidity ratio."""
    n = min(len(returns), len(volumes), window)
    if n < 5:
        return 0.0
    r = np.abs(returns[-n:])
    v = np.maximum(volumes[-n:], 1.0)
    return float(np.mean(r / v))


def depth_score(bids: list[dict], spot: float, n_bps: int = _DEPTH_BPS) -> float:
    """Total bid volume within n_bps of spot."""
    if not bids or spot <= 0:
        return 0.0
    threshold = spot * (1 - n_bps / 10_000)
    return float(
        sum(float(b.get("size", 0.0)) for b in bids if float(b.get("price", 0.0)) >= threshold)
    )


def cascade_price_level(bids: list[dict], spot: float, depth_pct10: float) -> float:
    """
    Find the price level where cumulative bid depth first drops below the
    10th-percentile of historical depth.  Bids are expected to be dicts with
    "price" and "size" keys, sorted descending by price (best bid first).

    Falls back to spot * 0.98 when insufficient data.
    """
    if not bids or spot <= 0 or depth_pct10 <= 0:
        return spot * 0.98

    cumulative = 0.0
    for bid in sorted(bids, key=lambda b: float(b.get("price", 0)), reverse=True):
        cumulative += float(bid.get("size", 0.0))
        if cumulative >= depth_pct10:
            price = float(bid.get("price", spot * 0.98))
            return max(price, spot * 0.90)  # floor at -10% to avoid nonsense

    return spot * 0.98  # bid wall exhausted before threshold


class E17Liquidity:
    def __init__(self, horizon_hours: int = 1) -> None:
        self._horizon = horizon_hours
        self._kyle_history: list[float] = []
        self._depth_history: list[float] = []

    async def run(self, symbol: str, data: dict) -> EngineOutput:
        spot: float = data.get("spot", 0.0)
        df: pd.DataFrame | None = data.get("ohlcv")
        if df is None or len(df) < 10 or spot <= 0:
            return EngineOutput.abstain(
                _ENGINE_ID, symbol, spot, self._horizon, "insufficient_data"
            )

        try:
            returns = df["close"].pct_change().dropna().values
            volumes = df["volume"].values[1:]  # align with returns

            # Price changes as % moves
            price_changes = returns[-50:] if len(returns) >= 50 else returns
            vols = volumes[-50:] if len(volumes) >= 50 else volumes
            signed_vols = vols * np.sign(price_changes[: len(vols)])

            kl = kyle_lambda(price_changes, signed_vols)
            ar = amihud_ratio(returns, volumes)

            # Orderbook depth
            bids_raw = data.get("bids", [])
            ds = depth_score(bids_raw, spot)

            # Track history
            self._kyle_history.append(abs(kl))
            self._depth_history.append(ds)
            if len(self._kyle_history) > 200:
                self._kyle_history.pop(0)
            if len(self._depth_history) > 200:
                self._depth_history.pop(0)

            # Stress flag: kyle > 2-sigma AND depth < 30th percentile
            kyle_std = np.std(self._kyle_history) if len(self._kyle_history) > 2 else 1.0
            kyle_mean = np.mean(self._kyle_history) if self._kyle_history else 0.0
            kyle_z = (abs(kl) - kyle_mean) / (kyle_std + 1e-9)
            depth_pct30 = (
                np.percentile(self._depth_history, 30) if len(self._depth_history) > 2 else 0.0
            )
            stress_flag = kyle_z > 2.0 and ds < depth_pct30

            # Cascade price: first price level where cumulative bid depth < 10th pct
            depth_pct10 = (
                np.percentile(self._depth_history, 10) if len(self._depth_history) > 2 else 0.0
            )
            cascade_level = cascade_price_level(bids_raw, spot, depth_pct10)
            liquidity_score = float(np.clip(1.0 - ar * 1e6, 0.0, 1.0))

            return EngineOutput(
                engine_id=_ENGINE_ID,
                symbol=symbol,
                timestamp_utc=datetime.now(UTC),
                predicted_price=spot,
                confidence=liquidity_score,
                direction=0,  # liquidity stress gives no directional signal
                horizon_hours=self._horizon,
                metadata={
                    "liquidity_score": liquidity_score,
                    "stress_flag": stress_flag,
                    "cascade_price_level": cascade_level,
                    "kyle_lambda": kl,
                    "amihud_ratio": ar,
                    "depth_score": ds,
                },
            )
        except Exception as exc:
            log.warning("e17_error", exc=str(exc))
            return EngineOutput.abstain(_ENGINE_ID, symbol, spot, self._horizon, str(exc))
