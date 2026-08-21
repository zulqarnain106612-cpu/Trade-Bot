"""
Arkham Intel provider — OCI-002.

Populates IntelligenceMetrics fields:
  exchange_netflow_7d_zscore, exchange_reserve_ratio,
  entity_exchange_imbalance, whale_buy_sell_ratio.

Auth: API-Key header. Rate: 20 req/s (enforced via RateLimiter).
All HTTP errors degrade gracefully: neutral value + confidence penalty.

API reference: https://intel.arkm.com/api (public docs)
"""

from __future__ import annotations

import math
import time
from typing import Any

import structlog

from src.intelligence.onchain.base import OnChainProvider


log = structlog.get_logger(__name__)

_BASE = "https://api.arkm.com"
_EXCHANGES = ["binance", "coinbase", "kraken", "okx", "bybit"]
_EPS = 1e-9

# Neutral values when a field cannot be populated
_NEUTRAL = {
    "exchange_netflow_7d_zscore": 0.0,
    "exchange_reserve_ratio": 0.5,
    "entity_exchange_imbalance": 0.0,
    "whale_buy_sell_ratio": 1.0,
}
_CONFIDENCE_PENALTY = 0.05


class ArkhamProvider(OnChainProvider):
    """
    Arkham Intel on-chain provider.

    Requires INTELLIGENCE_ARKHAM_API_KEY in env / config.
    If key is empty: all fields return neutral, confidence=0.0.
    """

    _BASE_URL = _BASE
    _RATE = 20.0  # 20 req/s per Arkham docs

    def __init__(self, api_key: str, cache_ttl_s: int = 60) -> None:
        super().__init__()
        self._api_key = api_key
        self._CACHE_TTL_S = cache_ttl_s
        # Rolling netflow history for z-score (30 samples x 7d = 210d)
        self._netflow_history: list[float] = []

    @property
    def exchange_id(self) -> str:
        return "arkham_intel"

    def _auth_headers(self) -> dict[str, str]:
        return {"API-Key": self._api_key, "Accept": "application/json"}

    async def initialize(self) -> None:
        if not self._api_key:
            log.warning("arkham_no_api_key_skip_warmup")
            return
        # Warm cache for exchange reserve summaries
        for ex in _EXCHANGES[:3]:
            await self._get(
                f"{_BASE}/intelligence/entity/{ex}/summary",
                headers=self._auth_headers(),
            )

    async def close(self) -> None:
        await super().close()

    # ------------------------------------------------------------------
    # fetch_metrics
    # ------------------------------------------------------------------

    async def fetch_metrics(self) -> dict[str, float]:
        result = dict(_NEUTRAL)
        confidence = 1.0

        if not self._api_key:
            result["confidence"] = 0.0
            result["timestamp"] = int(time.time())
            return result

        h = self._auth_headers()

        # 1. whale_buy_sell_ratio
        buy_data = await self._get(
            f"{_BASE}/intelligence/transfers",
            headers=h,
            params={"direction": "in", "timeframe": "24h", "usd_gte": "1000000"},
        )
        sell_data = await self._get(
            f"{_BASE}/intelligence/transfers",
            headers=h,
            params={"direction": "out", "timeframe": "24h", "usd_gte": "1000000"},
        )
        if buy_data is not None and sell_data is not None:
            buy_vol = _sum_usd(buy_data)
            sell_vol = _sum_usd(sell_data)
            ratio = buy_vol / (sell_vol + _EPS)
            result["whale_buy_sell_ratio"] = max(0.1, min(10.0, ratio))
        else:
            confidence -= _CONFIDENCE_PENALTY

        # 2. exchange_netflow_7d_zscore
        flow_data = await self._get(
            f"{_BASE}/intelligence/transfers",
            headers=h,
            params={"entity": ",".join(_EXCHANGES), "timeframe": "7d"},
        )
        if flow_data is not None:
            inflow = _sum_usd_direction(flow_data, "in")
            outflow = _sum_usd_direction(flow_data, "out")
            netflow = inflow - outflow
            self._netflow_history.append(netflow)
            if len(self._netflow_history) > 30:
                self._netflow_history = self._netflow_history[-30:]
            result["exchange_netflow_7d_zscore"] = _zscore(netflow, self._netflow_history)
        else:
            confidence -= _CONFIDENCE_PENALTY

        # 3. exchange_reserve_ratio
        total_balance = 0.0
        reserve_ok = True
        for ex in _EXCHANGES:
            summary = await self._get(
                f"{_BASE}/intelligence/entity/{ex}/summary",
                headers=h,
            )
            if summary is None:
                reserve_ok = False
                break
            total_balance += float(summary.get("totalUsdValue", 0) or 0)
        if reserve_ok:
            # Denominator: BTC market cap proxy (21M x ~$60k default; actual from summary)
            btc_cap = 21_000_000 * 60_000.0  # conservative floor
            ratio = total_balance / (btc_cap + _EPS)
            result["exchange_reserve_ratio"] = max(0.0, min(1.0, ratio))
        else:
            confidence -= _CONFIDENCE_PENALTY

        # 4. entity_exchange_imbalance — Herfindahl index of top-10 sender sizes
        hist_data = await self._get(
            f"{_BASE}/intelligence/transfers/histogram",
            headers=h,
            params={"entity": "binance", "timeframe": "30d"},
        )
        if hist_data is not None:
            result["entity_exchange_imbalance"] = _herfindahl(hist_data)
        else:
            confidence -= _CONFIDENCE_PENALTY

        result["confidence"] = max(0.0, confidence)
        result["timestamp"] = int(time.time())
        return result


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sum_usd(data: dict[str, Any]) -> float:
    transfers = data.get("transfers", []) or []
    return sum(float(t.get("usdValue", 0) or 0) for t in transfers)


def _sum_usd_direction(data: dict[str, Any], direction: str) -> float:
    transfers = data.get("transfers", []) or []
    return sum(
        float(t.get("usdValue", 0) or 0) for t in transfers if t.get("direction") == direction
    )


def _zscore(value: float, history: list[float]) -> float:
    if len(history) < 2:
        return 0.0
    mean = sum(history) / len(history)
    variance = sum((x - mean) ** 2 for x in history) / len(history)
    std = math.sqrt(variance) if variance > 0 else _EPS
    return (value - mean) / std


def _herfindahl(data: dict[str, Any]) -> float:
    """Herfindahl-Hirschman Index of top-10 transfer sizes, normalized to [0,1]."""
    buckets = data.get("histogram", []) or data.get("buckets", []) or []
    sizes = sorted(
        (float(b.get("usdValue", 0) or b.get("value", 0) or 0) for b in buckets),
        reverse=True,
    )[:10]
    total = sum(sizes) + _EPS
    shares = [s / total for s in sizes]
    hhi = sum(s**2 for s in shares)  # raw HHI in [1/n, 1]
    # Normalize: min=1/n (equal), max=1 (monopoly)
    n = max(len(shares), 1)
    hhi_min = 1.0 / n
    return max(0.0, min(1.0, (hhi - hhi_min) / (1.0 - hhi_min + _EPS)))
