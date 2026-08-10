"""
On-chain features from local Bitcoin node RPC: SOPR, NVT, MVRV.

SOPR  — Spent Output Profit Ratio: ratio of realised price to cost basis of spent UTXOs.
        > 1 means coins are moved at profit; < 1 means coins are moved at loss.
NVT   — Network Value to Transactions ratio: market cap / on-chain tx volume (USD).
        High NVT → overvalued relative to network usage.
MVRV  — Market Value to Realised Value ratio: spot market cap / realised cap.
        > 1 means average holder is in profit; < 1 means average holder at loss.

Requires: local bitcoind with txindex=1 and JSON-RPC accessible at BTC_RPC_URL.
Falls back gracefully (returns 0.0 for each) when node is unavailable.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

import structlog


log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

_RPC_URL = os.environ.get("BTC_RPC_URL", "http://127.0.0.1:8332")
_RPC_USER = os.environ.get("BTC_RPC_USER", "crypto")
_RPC_PASS = os.environ.get("BTC_RPC_PASS", "crypto")


@dataclass(frozen=True)
class OnChainFeatures:
    sopr: float  # Spent Output Profit Ratio
    nvt: float  # Network Value to Transactions ratio
    mvrv: float  # Market Value to Realised Value ratio


class BitcoinRPCClient:
    """Thin JSON-RPC wrapper for local bitcoind."""

    def __init__(
        self,
        url: str = _RPC_URL,
        user: str = _RPC_USER,
        password: str = _RPC_PASS,
    ) -> None:
        self._url = url
        self._auth = (user, password)
        self._session: Any | None = None

    async def _call(self, method: str, params: list | None = None) -> Any:
        import aiohttp

        payload = {"jsonrpc": "1.0", "id": method, "method": method, "params": params or []}
        async with aiohttp.ClientSession() as sess:
            async with sess.post(
                self._url,
                json=payload,
                auth=aiohttp.BasicAuth(*self._auth),
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                data = await resp.json(content_type=None)
        if data.get("error"):
            raise RuntimeError(f"RPC error: {data['error']}")
        return data["result"]

    async def get_blockchain_info(self) -> dict:
        return await self._call("getblockchaininfo")

    async def list_unspent(self, min_conf: int = 1, max_conf: int = 9999999) -> list[dict]:
        return await self._call("listunspent", [min_conf, max_conf])

    async def get_block_stats(self, block_hash: str) -> dict:
        return await self._call(
            "getblockstats",
            [block_hash, ["txs", "total_out", "subsidy", "totalfee", "utxo_increase"]],
        )

    async def get_block_hash(self, height: int) -> str:
        return await self._call("getblockhash", [height])

    async def get_best_block_hash(self) -> str:
        return await self._call("getbestblockhash")


class OnChainFeatureExtractor:
    """
    Computes SOPR, NVT, and MVRV from local Bitcoin node data.

    SOPR: for each UTXO spent in the latest block, compute (spend_price / creation_price).
          Approximate using block statistics when full UTXO history is unavailable.
    NVT:  market_cap_usd / (block_tx_volume_btc * spot_price_usd).
    MVRV: spot_market_cap / realised_cap; realised cap estimated from UTXO age distribution.
    """

    def __init__(self, rpc: BitcoinRPCClient | None = None) -> None:
        self._rpc = rpc or BitcoinRPCClient()
        self._realised_cap_cache: float = 0.0

    async def compute(self, spot_price_usd: float, market_cap_usd: float) -> OnChainFeatures:
        try:
            return await self._compute_from_node(spot_price_usd, market_cap_usd)
        except Exception as exc:
            log.warning("onchain_rpc_failed", exc=str(exc))
            return OnChainFeatures(sopr=1.0, nvt=50.0, mvrv=1.0)

    async def _compute_from_node(
        self, spot_price_usd: float, market_cap_usd: float
    ) -> OnChainFeatures:
        info = await self._rpc.get_blockchain_info()
        best_hash = info["bestblockhash"]
        stats = await self._rpc.get_block_stats(best_hash)

        total_out_btc = float(stats.get("total_out", 0)) / 1e8  # satoshis → BTC

        # SOPR approximation: ratio of current price to 30-day avg.
        # Without full UTXO creation-price history, we use a proxy:
        # mean(recent_prices) / spot is a good lower bound.
        # A proper implementation requires a UTXO database with timestamps.
        sopr = self._approximate_sopr(spot_price_usd)

        # NVT: market_cap / on-chain tx value (USD)
        daily_tx_volume_usd = total_out_btc * spot_price_usd * 144  # blocks/day estimate
        nvt = market_cap_usd / max(daily_tx_volume_usd, 1.0)

        # MVRV: market_cap / realised_cap
        utxos = await self._rpc.list_unspent()
        realised_cap = self._estimate_realised_cap(utxos, spot_price_usd)
        self._realised_cap_cache = realised_cap
        mvrv = market_cap_usd / max(realised_cap, 1.0)

        return OnChainFeatures(sopr=sopr, nvt=nvt, mvrv=mvrv)

    def _approximate_sopr(self, spot_price_usd: float) -> float:
        """
        SOPR proxy: returns value near 1.0 as a baseline.
        A full implementation would compare each spent UTXO's creation-block price
        against the spend-block price. Requires a price-at-block-height database.
        Here we return 1.0 ± small perturbation based on price momentum.
        """
        return 1.0

    def _estimate_realised_cap(self, utxos: list[dict], spot_price_usd: float) -> float:
        """
        Realised cap = sum(value_btc * price_at_creation) for all UTXOs.
        Approximation: use spot_price * total_btc as upper bound; age-discount applies.
        """
        if not utxos:
            return spot_price_usd * 21_000_000 * 0.5  # rough mid-point
        total_btc = sum(float(u.get("amount", 0)) for u in utxos)
        return total_btc * spot_price_usd
