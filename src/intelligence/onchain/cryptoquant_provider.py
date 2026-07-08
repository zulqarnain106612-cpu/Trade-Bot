"""
CryptoQuant provider — OCI-005.

Supplements Arkham (exchange flows) and Dune (miner flows) with
CryptoQuant's exchange-specific data. Disabled when key is empty
(fail-open: all fields return neutral, confidence=0.0).

Populated fields:
  exchange_reserve_ratio          (supplement — blended with Arkham)
  exchange_netflow_7d_zscore      (supplement — blended with Arkham)
  miner_netflow_signal            (supplement — blended with Dune)
  binance_funding_rate_pct        (supplement — fallback when Binance provider failed)
  exchange_stress_score           (additive MVRV contribution)

Auth: Authorization: Bearer {cryptoquant_api_key}
Rate: 10 req/min (Basic tier) → RateLimiter(10, window_s=60.0)
Cache: 300s (daily-resolution data — sub-5min refresh has zero value)

Authority: https://docs.cryptoquant.com/api-reference/
"""
from __future__ import annotations

import logging
import math
import time
from typing import Any

from src.intelligence.onchain.base import OnChainProvider

logger = logging.getLogger(__name__)

_BASE = "https://api.cryptoquant.com/v1"
_EPS = 1e-9
_DAILY_WINDOW = 30          # candles to fetch for z-score baseline
_MVRV_STRESS_THRESHOLD = 3.5
_MVRV_STRESS_ADD = 0.3
_CONFIDENCE_PENALTY = 0.05

_NEUTRAL: dict[str, float] = {
    "exchange_reserve_ratio": 0.5,
    "exchange_netflow_7d_zscore": 0.0,
    "miner_netflow_signal": 0.0,
    "binance_funding_rate_pct": 0.0,
    "exchange_stress_score_mvrv_contrib": 0.0,   # internal; merged by aggregator
}


class CryptoQuantProvider(OnChainProvider):
    """
    CryptoQuant on-chain provider.

    Disabled (fail-open) when cryptoquant_api_key is empty — no exception raised.
    """

    _BASE_URL = _BASE
    _CACHE_TTL_S = 300
    _RATE = 10.0 / 60.0   # 10 req/min expressed as req/s for RateLimiter window_s=1.0

    def __init__(self, api_key: str, cache_ttl_s: int = 300) -> None:
        super().__init__()
        self._api_key = api_key
        self._CACHE_TTL_S = cache_ttl_s
        self._disabled = not bool(api_key)
        if self._disabled:
            logger.warning("CryptoQuantProvider: no API key — all fields neutral (fail-open)")

    @property
    def exchange_id(self) -> str:
        return "cryptoquant"

    def _auth(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._api_key}", "Accept": "application/json"}

    async def initialize(self) -> None:
        if self._disabled:
            return
        # Warm cache: reserve + netflow are most expensive; fetch on init.
        await self._get(f"{_BASE}/btc/exchange-flows/reserve",
                        headers=self._auth(),
                        params={"window": "day", "limit": str(_DAILY_WINDOW)})

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

        # 1. exchange_reserve_ratio
        reserve_data = await self._get(f"{_BASE}/btc/exchange-flows/reserve",
                                       headers=h,
                                       params={"window": "day", "limit": str(_DAILY_WINDOW)})
        if reserve_data is not None:
            result["exchange_reserve_ratio"] = _reserve_ratio(reserve_data)
        else:
            confidence -= _CONFIDENCE_PENALTY

        # 2. exchange_netflow_7d_zscore
        netflow_data = await self._get(f"{_BASE}/btc/exchange-flows/netflow",
                                       headers=h,
                                       params={"window": "day", "limit": str(_DAILY_WINDOW)})
        if netflow_data is not None:
            result["exchange_netflow_7d_zscore"] = _netflow_zscore(netflow_data)
        else:
            confidence -= _CONFIDENCE_PENALTY

        # 3. miner_netflow_signal
        miner_data = await self._get(f"{_BASE}/btc/miner-flows/netflow",
                                     headers=h,
                                     params={"window": "day", "limit": str(_DAILY_WINDOW)})
        if miner_data is not None:
            result["miner_netflow_signal"] = _miner_signal(miner_data)
        else:
            confidence -= _CONFIDENCE_PENALTY

        # 4. binance_funding_rate_pct (fallback supplement)
        fr_data = await self._get(f"{_BASE}/btc/derivatives/funding-rates",
                                  headers=h,
                                  params={"window": "day", "limit": "7"})
        if fr_data is not None:
            fr = _extract_binance_funding(fr_data)
            if fr is not None:
                result["binance_funding_rate_pct"] = fr
        else:
            confidence -= _CONFIDENCE_PENALTY

        # 5. MVRV → exchange_stress_score additive contribution
        mvrv_data = await self._get(f"{_BASE}/btc/market-data/market-cap",
                                    headers=h,
                                    params={"window": "day", "limit": "2"})
        if mvrv_data is not None:
            contrib = _mvrv_stress_contrib(mvrv_data)
            result["exchange_stress_score_mvrv_contrib"] = contrib
        else:
            confidence -= _CONFIDENCE_PENALTY

        result["confidence"] = max(0.0, confidence)
        return result


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _extract_rows(data: dict[str, Any]) -> list[dict[str, Any]]:
    """CryptoQuant v1 wraps rows under data.result.data or data.result."""
    result = data.get("result", data)
    if isinstance(result, dict):
        return result.get("data", []) or []
    if isinstance(result, list):
        return result
    return []


def _reserve_ratio(data: dict[str, Any]) -> float:
    rows = _extract_rows(data)
    if not rows:
        return 0.5
    latest = rows[-1]
    reserve_usd = float(latest.get("reserve_usd", 0) or 0)
    # Approximate BTC total supply × price for normalization
    btc_supply = 21_000_000.0
    btc_price_approx = float(latest.get("price", 60_000) or 60_000)
    total_cap = btc_supply * btc_price_approx
    return max(0.0, min(1.0, reserve_usd / (total_cap + _EPS)))


def _netflow_zscore(data: dict[str, Any]) -> float:
    rows = _extract_rows(data)
    if len(rows) < 2:
        return 0.0
    # Sum 7 most recent days; z-score vs full window
    netflows = [float(r.get("netflow_usd", 0) or 0) for r in rows]
    recent_7d = sum(netflows[-7:])
    mean = sum(netflows) / len(netflows)
    variance = sum((x - mean) ** 2 for x in netflows) / len(netflows)
    std = math.sqrt(variance) if variance > 0 else _EPS
    return (recent_7d / 7 - mean) / std


def _miner_signal(data: dict[str, Any]) -> float:
    rows = _extract_rows(data)
    if len(rows) < 2:
        return 0.0
    netflows = [float(r.get("netflow_usd", 0) or 0) for r in rows]
    latest = netflows[-1]
    mean = sum(netflows) / len(netflows)
    variance = sum((x - mean) ** 2 for x in netflows) / len(netflows)
    std = math.sqrt(variance) if variance > 0 else _EPS
    zscore = (latest - mean) / std
    # Clamp to [-1, +1]; positive = miner selling = bearish
    return max(-1.0, min(1.0, zscore / 3.0))


def _extract_binance_funding(data: dict[str, Any]) -> float | None:
    rows = _extract_rows(data)
    for row in reversed(rows):
        exchange = str(row.get("exchange", "")).lower()
        if "binance" in exchange:
            fr = row.get("funding_rate") or row.get("fundingRate")
            if fr is not None:
                return float(fr)
    return None


def _mvrv_stress_contrib(data: dict[str, Any]) -> float:
    rows = _extract_rows(data)
    if not rows:
        return 0.0
    latest = rows[-1]
    market_cap = float(latest.get("market_cap", 0) or 0)
    realized_cap = float(latest.get("realized_cap", 1) or 1)
    mvrv = market_cap / (realized_cap + _EPS)
    if mvrv > _MVRV_STRESS_THRESHOLD:
        return min(_MVRV_STRESS_ADD, (mvrv - _MVRV_STRESS_THRESHOLD) / 10.0)
    return 0.0
