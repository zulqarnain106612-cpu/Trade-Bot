"""
Coinglass provider — OCI-006.

Supplements exchange providers with open-interest aggregates, liquidation
heatmap data, and long/short ratios that span all exchanges. Disabled when
key is empty (fail-open: all fields neutral, confidence=0.0).

Populated fields:
  futures_oi_change_pct           (supplement — blended with Binance/OKX)
  liquidation_pressure_24h_zscore (supplement — blended with Binance/OKX)
  liquidation_cascade_risk_usd    (additive — heatmap max cluster)
  binance_funding_rate_pct        (fallback — averaged across exchanges)
  whale_buy_sell_ratio            (supplement — global L/S ratio proxy)

Auth: CG-API-KEY header
Rate: 30 req/min (free tier) → RateLimiter(30, window_s=60.0)
Cache: 60s (1-min granularity on most endpoints)

Authority: https://open-api-v3.coinglass.com/api/docs
"""

from __future__ import annotations

import logging
import math
import time
from typing import Any

from src.intelligence.onchain.base import OnChainProvider


logger = logging.getLogger(__name__)

_BASE = "https://open-api-v3.coinglass.com/api"
_EPS = 1e-9
_OI_WINDOW = 48  # 1h candles for 48h OI window
_LIQ_WINDOW = 24  # liquidation candles (1h) for z-score
_CONFIDENCE_PENALTY = 0.05

_NEUTRAL: dict[str, float] = {
    "futures_oi_change_pct": 0.0,
    "liquidation_pressure_24h_zscore": 0.0,
    "liquidation_cascade_risk_usd": 0.0,
    "binance_funding_rate_pct": 0.0,
    "whale_buy_sell_ratio": 1.0,
}


class CoinglassProvider(OnChainProvider):
    """
    Coinglass on-chain/derivatives provider.

    Disabled (fail-open) when coinglass_api_key is empty.
    """

    _BASE_URL = _BASE
    _CACHE_TTL_S = 60
    _RATE = 30.0 / 60.0  # 30 req/min expressed as req/s for window_s=1.0

    def __init__(self, api_key: str, cache_ttl_s: int = 60) -> None:
        super().__init__()
        self._api_key = api_key
        self._CACHE_TTL_S = cache_ttl_s
        self._disabled = not bool(api_key)
        if self._disabled:
            logger.warning("CoinglassProvider: no API key — all fields neutral (fail-open)")

    @property
    def exchange_id(self) -> str:
        return "coinglass"

    def _auth(self) -> dict[str, str]:
        return {"CG-API-KEY": self._api_key, "Accept": "application/json"}

    async def initialize(self) -> None:
        if self._disabled:
            return
        # Warm cache with OI (most-used field)
        await self._get(
            f"{_BASE}/futures/openInterest/ohlc-history",
            headers=self._auth(),
            params={"symbol": "BTC", "interval": "1h", "limit": str(_OI_WINDOW)},
        )

    async def close(self) -> None:
        await super().close()

    # ------------------------------------------------------------------
    # fetch_metrics
    # ------------------------------------------------------------------

    async def fetch_metrics(self) -> dict[str, float]:
        result: dict[str, float] = dict(_NEUTRAL)
        result["timestamp"] = int(time.time())

        if self._disabled:
            result["confidence"] = 0.0
            return result

        confidence = 1.0
        h = self._auth()

        # 1. futures_oi_change_pct — % change over last 24h
        oi_data = await self._get(
            f"{_BASE}/futures/openInterest/ohlc-history",
            headers=h,
            params={"symbol": "BTC", "interval": "1h", "limit": str(_OI_WINDOW)},
        )
        if oi_data is not None:
            result["futures_oi_change_pct"] = _oi_change_pct(oi_data)
        else:
            confidence -= _CONFIDENCE_PENALTY

        # 2. liquidation_pressure_24h_zscore
        liq_data = await self._get(
            f"{_BASE}/futures/liquidation/chart",
            headers=h,
            params={"symbol": "BTC", "interval": "1h", "limit": str(_LIQ_WINDOW)},
        )
        if liq_data is not None:
            result["liquidation_pressure_24h_zscore"] = _liq_zscore(liq_data)
        else:
            confidence -= _CONFIDENCE_PENALTY

        # 3. liquidation_cascade_risk_usd — max cluster in heatmap
        heatmap = await self._get(
            f"{_BASE}/futures/liquidation/heatmap",
            headers=h,
            params={"symbol": "BTCUSDT", "timeType": "3"},
        )
        if heatmap is not None:
            result["liquidation_cascade_risk_usd"] = _heatmap_max(heatmap)
        else:
            confidence -= _CONFIDENCE_PENALTY

        # 4. binance_funding_rate_pct — aggregated funding across exchanges
        fr_data = await self._get(
            f"{_BASE}/futures/funding-rate/chart",
            headers=h,
            params={"symbol": "BTC", "interval": "8h", "limit": "3"},
        )
        if fr_data is not None:
            fr = _extract_funding(fr_data)
            if fr is not None:
                result["binance_funding_rate_pct"] = fr
        else:
            confidence -= _CONFIDENCE_PENALTY

        # 5. whale_buy_sell_ratio — global long/short ratio proxy
        ls_data = await self._get(
            f"{_BASE}/futures/longShortAccount/chart",
            headers=h,
            params={"symbol": "BTC", "interval": "1h", "limit": "1"},
        )
        if ls_data is not None:
            result["whale_buy_sell_ratio"] = _ls_ratio(ls_data)
        else:
            confidence -= _CONFIDENCE_PENALTY

        result["confidence"] = max(0.0, confidence)
        return result


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _extract_list(data: dict[str, Any]) -> list[Any]:
    """Coinglass v3 wraps rows under data.data or data."""
    if isinstance(data, dict):
        inner = data.get("data", data)
        if isinstance(inner, list):
            return inner
        # Sometimes nested: {data: {list: [...]}}
        if isinstance(inner, dict):
            for v in inner.values():
                if isinstance(v, list):
                    return v
    return []


def _oi_change_pct(data: dict[str, Any]) -> float:
    """% change of OI from 24h-ago candle to latest."""
    rows = _extract_list(data)
    if len(rows) < 2:
        return 0.0

    # Each candle: {o, h, l, c} or {openInterest, ...}
    def _close(row: Any) -> float:
        if isinstance(row, list | tuple) and len(row) >= 4:
            return float(row[3])
        if isinstance(row, dict):
            for k in ("c", "close", "openInterest", "oi"):
                if k in row:
                    return float(row[k])
        return 0.0

    latest = _close(rows[-1])
    past_24h = _close(rows[max(0, len(rows) - 24)])
    if abs(past_24h) < _EPS:
        return 0.0
    return (latest - past_24h) / (abs(past_24h) + _EPS) * 100.0


def _liq_zscore(data: dict[str, Any]) -> float:
    """Z-score of latest 1h liquidation vs window."""
    rows = _extract_list(data)
    if len(rows) < 2:
        return 0.0

    def _total(row: Any) -> float:
        if isinstance(row, dict):
            buy = float(row.get("buyLiquidationUsd", 0) or 0)
            sell = float(row.get("sellLiquidationUsd", 0) or 0)
            return buy + sell
        if isinstance(row, list | tuple) and len(row) >= 2:
            return float(row[0]) + float(row[1])
        return 0.0

    values = [_total(r) for r in rows]
    mean = sum(values) / len(values)
    variance = sum((x - mean) ** 2 for x in values) / len(values)
    std = math.sqrt(variance) if variance > 0 else _EPS
    return (values[-1] - mean) / std


def _heatmap_max(data: dict[str, Any]) -> float:
    """Maximum liquidation cluster USD value from heatmap."""
    rows = _extract_list(data)
    if not rows:
        return 0.0
    max_val = 0.0
    for row in rows:
        if isinstance(row, list | tuple) and len(row) >= 3:
            max_val = max(max_val, float(row[2]))
        elif isinstance(row, dict):
            v = float(row.get("liquidationUsd", 0) or row.get("value", 0) or 0)
            max_val = max(max_val, v)
    return max_val


def _extract_funding(data: dict[str, Any]) -> float | None:
    """Average funding rate across exchanges from latest candle."""
    rows = _extract_list(data)
    if not rows:
        return None
    latest = rows[-1]
    if isinstance(latest, dict):
        # Aggregate field
        for k in ("fundingRate", "avgFundingRate", "funding_rate", "rate"):
            if k in latest:
                return float(latest[k])
        # Per-exchange list inside
        exchanges = latest.get("exchangeList", [])
        if exchanges:
            rates = [float(e.get("fundingRate", 0) or 0) for e in exchanges]
            return sum(rates) / len(rates)
    return None


def _ls_ratio(data: dict[str, Any]) -> float:
    """Long/short ratio → whale_buy_sell_ratio proxy. Returns 1.0 if missing."""
    rows = _extract_list(data)
    if not rows:
        return 1.0
    latest = rows[-1]
    if isinstance(latest, dict):
        long_pct = float(latest.get("longRatio", 0) or latest.get("longAccount", 0) or 0)
        short_pct = float(latest.get("shortRatio", 0) or latest.get("shortAccount", 0) or 0)
        if short_pct > _EPS:
            return long_pct / short_pct
    return 1.0
