"""
DeFiLlama provider — OCI-003.

Populates:
  staking_unlock_risk       — proxy via Ethereum TVL 7d/14d change
  defi_tvl_7d_change_pct   — new field (gated inactive until OCI-007 schema)
  stablecoin_reserve_ratio  — fallback if CoinGecko returned neutral 0.5

No API key required. Rate limit: generous public tier (~300 req/min).
Cache TTL: 300s (defillama_cache_ttl_s).

Authority: https://defillama.com/docs/api
"""

from __future__ import annotations

import logging
import time
from typing import Any

from src.intelligence.onchain.base import OnChainProvider


logger = logging.getLogger(__name__)

_BASE = "https://api.llama.fi"
_STABLE_BASE = "https://stablecoins.llama.fi"
_EPS = 1e-9

_NEUTRAL: dict[str, float] = {
    "staking_unlock_risk": 0.0,
    "defi_tvl_7d_change_pct": 0.0,
    "stablecoin_reserve_ratio": 0.5,
}
_CONFIDENCE_PENALTY = 0.05


class DeFiLlamaProvider(OnChainProvider):
    """
    DeFiLlama on-chain provider (no API key required).

    staking_unlock_risk is a proxy: large TVL drops correlate with
    mass unstaking/unlock events (best freely-available signal).
    """

    _BASE_URL = _BASE
    _CACHE_TTL_S = 300
    _RATE = 5.0  # conservative; DeFiLlama public limit is high but unspecified

    def __init__(self, cache_ttl_s: int = 300) -> None:
        super().__init__()
        self._CACHE_TTL_S = cache_ttl_s

    @property
    def exchange_id(self) -> str:
        return "defillama"

    async def initialize(self) -> None:
        # No auth; no warmup needed
        pass

    async def close(self) -> None:
        await super().close()

    async def fetch_metrics(self) -> dict[str, float]:
        result = dict(_NEUTRAL)
        confidence = 1.0

        # 1. Ethereum historical TVL → staking_unlock_risk + defi_tvl_7d_change_pct
        eth_tvl = await self._get(f"{_BASE}/v2/historicalChainTvl/Ethereum")
        if eth_tvl is not None:
            unlock_risk, tvl_change = _compute_tvl_metrics(eth_tvl)
            result["staking_unlock_risk"] = unlock_risk
            result["defi_tvl_7d_change_pct"] = tvl_change
        else:
            confidence -= _CONFIDENCE_PENALTY

        # 2. Stablecoin reserve ratio (fallback only; primary = CoinGecko)
        stable_data = await self._get(f"{_STABLE_BASE}/stablecoins")
        if stable_data is not None:
            ratio = _stablecoin_ratio(stable_data)
            if ratio is not None:
                result["stablecoin_reserve_ratio"] = ratio
        else:
            confidence -= _CONFIDENCE_PENALTY

        result["confidence"] = max(0.0, confidence)
        result["timestamp"] = int(time.time())
        return result


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _compute_tvl_metrics(data: Any) -> tuple[float, float]:
    """
    Returns (staking_unlock_risk, defi_tvl_7d_change_pct).

    data: list of {"date": int, "tvl": float} sorted ascending.
    """
    if not isinstance(data, list) or len(data) < 2:
        return 0.0, 0.0

    # Most recent 14 entries (each entry = 1 day)
    recent = data[-14:]
    tvl_now = float(recent[-1].get("tvl", 0) or 0)

    # 7d change
    if len(recent) >= 8:
        tvl_7d_ago = float(recent[-8].get("tvl", 0) or 0)
    else:
        tvl_7d_ago = float(recent[0].get("tvl", 0) or 0)

    change_pct = (tvl_now - tvl_7d_ago) / (abs(tvl_7d_ago) + _EPS) * 100.0

    # Staking unlock risk thresholds (from ONCHAIN_TASKS spec)
    if change_pct < -10.0:
        unlock_risk = 0.8
    elif change_pct < -5.0:
        unlock_risk = 0.5
    else:
        unlock_risk = 0.1

    return unlock_risk, change_pct


def _stablecoin_ratio(data: Any) -> float | None:
    """
    Compute (USDT_mcap + USDC_mcap) / total_stablecoin_mcap as proxy for
    stablecoin_reserve_ratio. Returns None if data is malformed.
    """
    pegs = data.get("peggedAssets", []) if isinstance(data, dict) else []
    if not pegs:
        return None

    total = 0.0
    usd_major = 0.0
    for asset in pegs:
        circ = asset.get("circulating", {})
        usd_val = float(circ.get("peggedUSD", 0) or 0)
        total += usd_val
        symbol = (asset.get("symbol") or "").upper()
        if symbol in ("USDT", "USDC"):
            usd_major += usd_val

    if total < _EPS:
        return None
    return max(0.0, min(1.0, usd_major / total))
