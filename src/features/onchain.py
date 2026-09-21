"""
On-chain features from local Bitcoin node RPC: SOPR, NVT, MVRV.

SOPR  — Spent Output Profit Ratio: ratio of realised price to cost basis of spent UTXOs.
        > 1 means coins are moved at profit; < 1 means coins are moved at loss.
NVT   — Network Value to Transactions ratio: market cap / on-chain tx volume (USD).
        High NVT → overvalued relative to network usage.
MVRV  — Market Value to Realised Value ratio: spot market cap / realised cap.
        > 1 means average holder is in profit; < 1 means average holder at loss.

Requires: local bitcoind with txindex=1 and JSON-RPC accessible at BTC_RPC_URL.

Unavailable values are NaN, never a plausible-looking constant. SOPR needs a
price-at-block-height database this project does not have, so it is NaN
always; when the node is unreachable all three are NaN. See REG-0008 -- the
previous behaviour returned 1.0/50.0/1.0, which a consumer could not tell
apart from a measurement.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

import structlog

log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

# Unavailable on-chain values are NaN (REG-0008), matching the NaN convention
# in build_inference_features and the GAP-015 fix in intelligence/metrics.py.
_NAN = float("nan")

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

        from src.features._rpc_auth import basic_auth_header

        payload = {"jsonrpc": "1.0", "id": method, "method": method, "params": params or []}
        async with (
            aiohttp.ClientSession() as sess,
            sess.post(
                self._url,
                json=payload,
                headers=basic_auth_header(*self._auth),
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp,
        ):
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

    SOPR: unimplemented -- always NaN. It needs each spent UTXO's creation-block
          price, and no price-at-block-height source is wired in (REG-0008).
    NVT:  market_cap_usd / (block_tx_volume_btc * spot_price_usd). Computed.
    MVRV: spot_market_cap / realised_cap, where realised cap is approximated at
          spot rather than at each UTXO's creation price -- an upper bound, not
          a true realised cap. NaN when there are no UTXOs to sum.
    """

    def __init__(self, rpc: BitcoinRPCClient | None = None) -> None:
        self._rpc = rpc or BitcoinRPCClient()
        self._realised_cap_cache: float = 0.0

    async def compute(self, spot_price_usd: float, market_cap_usd: float) -> OnChainFeatures:
        try:
            return await self._compute_from_node(spot_price_usd, market_cap_usd)
        except Exception as exc:
            log.warning("onchain_rpc_failed", exc=str(exc))
            # REG-0008: NaN, not 1.0/50.0/1.0. A node outage must not be
            # indistinguishable from a neutral on-chain reading.
            return OnChainFeatures(sopr=_NAN, nvt=_NAN, mvrv=_NAN)

    async def _compute_from_node(
        self, spot_price_usd: float, market_cap_usd: float
    ) -> OnChainFeatures:
        info = await self._rpc.get_blockchain_info()
        best_hash = info["bestblockhash"]
        stats = await self._rpc.get_block_stats(best_hash)

        total_out_btc = float(stats.get("total_out", 0)) / 1e8  # satoshis → BTC

        # SOPR is not computed here and is not approximated: it needs each
        # spent UTXO's creation-block price, which requires a
        # price-at-block-height database this project does not have.
        sopr = self._sopr_unimplemented()

        # NVT: market_cap / on-chain tx value (USD)
        daily_tx_volume_usd = total_out_btc * spot_price_usd * 144  # blocks/day estimate
        nvt = market_cap_usd / max(daily_tx_volume_usd, 1.0)

        # MVRV: market_cap / realised_cap
        utxos = await self._rpc.list_unspent()
        realised_cap = self._estimate_realised_cap(utxos, spot_price_usd)
        self._realised_cap_cache = realised_cap
        mvrv = market_cap_usd / max(realised_cap, 1.0)

        return OnChainFeatures(sopr=sopr, nvt=nvt, mvrv=mvrv)

    def _sopr_unimplemented(self) -> float:
        """
        SOPR is unimplemented and reports itself as such: always NaN.

        A real SOPR compares each spent UTXO's creation-block price against its
        spend-block price, which needs a price-at-block-height database. Until
        one is wired in there is no input from which SOPR can be derived, so
        there is nothing to approximate -- a constant here would vary with
        nothing and be indistinguishable from a measurement that happened to
        sit at 1.0 (REG-0008, and the same fabricated-completion-state bug as
        GAP-015 in src/intelligence/metrics.py).
        """
        return _NAN

    def _estimate_realised_cap(self, utxos: list[dict], spot_price_usd: float) -> float:
        """
        Realised cap = sum(value_btc * price_at_creation) for all UTXOs.

        Approximated at spot rather than at creation price, so this is an upper
        bound on realised cap and the MVRV built from it is correspondingly a
        lower bound. With no UTXOs there is nothing to sum: NaN, not an invented
        fraction of the 21M supply (REG-0008).
        """
        if not utxos:
            return _NAN
        total_btc = sum(float(u.get("amount", 0)) for u in utxos)
        return total_btc * spot_price_usd
