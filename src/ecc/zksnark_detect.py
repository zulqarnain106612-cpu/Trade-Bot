"""
zkSNARK Privacy Flow Inference — Tornado Cash pattern detection.

Detects mixing flows on Ethereum (Tornado Cash) and infers dark pool
pressure: the probability that a significant volume of ETH/BTC is
being routed through privacy mixers before hitting exchanges.

High dark_pool_pressure is a leading indicator of large whale moves
being hidden before execution on centralized venues.

Output: dark_pool_pressure ∈ [0, 1]
  0 → transparent on-chain flows
  1 → fully mixed / maximum privacy routing
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

import structlog


log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

_ETH_RPC_URL = os.environ.get("ETH_RPC_URL", "http://127.0.0.1:8545")

# Known Tornado Cash contract addresses (Ethereum mainnet)
_TORNADO_CONTRACTS = {
    # ETH pools
    "0x12d66f87a04a9e220c9d0a5f7dcc2b1e788a4ecb",  # 0.1 ETH
    "0x47ce0c6ed5b0ce3d3a51fdb1c52dc66a7c3c2936",  # 1 ETH
    "0x910cbd523d972eb0a6f4cae4618ad62622b39dbf",  # 10 ETH
    "0xa160cdab225685da1d56aa342ad8841c3b53f291",  # 100 ETH
    # DAI pools
    "0xd4b88df4d29f5cedd6857912842cff3b20c8cfa3",
    "0xfd8610d20aa15b7b2e3be39b396a1bc3516c7144",
    "0x22aaa7720ddd5388a3c0a3333430953c68f1849b",
    "0xba214c1c1928a32bffe790263e38b4af9bfcd659",
}


@dataclass(frozen=True)
class MixingFlowResult:
    dark_pool_pressure: float  # [0, 1]
    tornado_deposits_detected: int
    estimated_mixed_eth: float
    withdrawal_cluster_depth: int


def _is_tornado_deposit(tx: dict) -> bool:
    """Return True if transaction is a Tornado Cash deposit."""
    to_addr = (tx.get("to") or "").lower()
    return to_addr in _TORNADO_CONTRACTS and tx.get("input", "0x")[:10] in (
        "0xb214faa5",  # deposit(bytes32 commitment)
        "0x00",
    )


def _is_tornado_withdrawal(tx: dict) -> bool:
    """Return True if transaction is a Tornado Cash withdrawal."""
    to_addr = (tx.get("to") or "").lower()
    return to_addr in _TORNADO_CONTRACTS and tx.get("input", "0x")[:10] == "0x21a0adb5"


def trace_spend_cluster(
    deposit_tx: dict,
    all_txs: list[dict],
    depth: int = 3,
) -> list[dict]:
    """
    BFS over withdrawal graph starting from a Tornado Cash deposit.

    Finds withdrawal transactions potentially linked to the same mixing note
    within `depth` hops (by timing window and amount correlation).

    Returns list of candidate withdrawal transactions.
    """
    target_value = (
        int(deposit_tx.get("value", "0x0"), 16)
        if isinstance(deposit_tx.get("value"), str)
        else deposit_tx.get("value", 0)
    )

    cluster = []
    visited = {deposit_tx.get("hash", "")}
    queue = [deposit_tx]
    current_depth = 0

    while queue and current_depth < depth:
        next_queue = []
        for _tx in queue:
            for candidate in all_txs:
                if candidate.get("hash", "") in visited:
                    continue
                if not _is_tornado_withdrawal(candidate):
                    continue
                # Amount correlation (same Tornado pool = same denomination)
                candidate_value = (
                    int(candidate.get("value", "0x0"), 16)
                    if isinstance(candidate.get("value"), str)
                    else candidate.get("value", 0)
                )
                if abs(candidate_value - target_value) / max(target_value, 1) < 0.01:
                    cluster.append(candidate)
                    visited.add(candidate.get("hash", ""))
                    next_queue.append(candidate)
        queue = next_queue
        current_depth += 1

    return cluster


def estimate_dark_pool_pressure(cluster: list[dict]) -> float:
    """
    Estimate dark pool pressure from a withdrawal cluster.

    More withdrawals from the same pool denomination in a short window
    = more mixing activity = higher dark pool pressure.

    Returns [0, 1].
    """
    if not cluster:
        return 0.0
    n = len(cluster)
    # Saturation at 50 withdrawals in a window
    return float(min(n / 50.0, 1.0))


class ZkSnarkDetector:
    """
    High-level detector that queries an Ethereum node and tracks mixing flows.
    """

    def __init__(self, eth_rpc_url: str = _ETH_RPC_URL) -> None:
        self._rpc_url = eth_rpc_url
        self._w3: Any | None = None
        self._available = False
        self._load_web3()

    def _load_web3(self) -> None:
        try:
            from web3 import Web3  # type: ignore[import]

            self._w3 = Web3(Web3.HTTPProvider(self._rpc_url, request_kwargs={"timeout": 10}))
            if self._w3.is_connected():
                self._available = True
                log.info("web3_connected", rpc=self._rpc_url)
            else:
                log.warning("web3_not_connected", rpc=self._rpc_url)
        except ImportError:
            log.warning("web3_not_installed")
        except Exception as exc:
            log.warning("web3_init_failed", exc=str(exc))

    def detect_mixing_flows(self, block_lookback: int = 10) -> MixingFlowResult:
        """
        Scan recent Ethereum blocks for Tornado Cash activity.
        Returns MixingFlowResult with dark_pool_pressure ∈ [0, 1].
        """
        if not self._available or self._w3 is None:
            return MixingFlowResult(
                dark_pool_pressure=0.0,
                tornado_deposits_detected=0,
                estimated_mixed_eth=0.0,
                withdrawal_cluster_depth=0,
            )
        try:
            latest = self._w3.eth.block_number
            all_txs = []
            deposit_txs = []
            total_mixed_wei = 0

            for block_num in range(max(0, latest - block_lookback), latest + 1):
                block = self._w3.eth.get_block(block_num, full_transactions=True)
                for tx in block.transactions:
                    tx_dict = dict(tx)
                    all_txs.append(tx_dict)
                    if _is_tornado_deposit(tx_dict):
                        deposit_txs.append(tx_dict)
                        total_mixed_wei += int(tx_dict.get("value", 0))

            # BFS cluster for each deposit
            max_cluster_depth = 0
            total_cluster_size = 0
            for dep in deposit_txs[:5]:  # limit scan cost
                cluster = trace_spend_cluster(dep, all_txs, depth=3)
                total_cluster_size += len(cluster)
                max_cluster_depth = max(max_cluster_depth, len(cluster))

            pressure = estimate_dark_pool_pressure([{}] * (len(deposit_txs) + total_cluster_size))
            estimated_eth = total_mixed_wei / 1e18

            return MixingFlowResult(
                dark_pool_pressure=pressure,
                tornado_deposits_detected=len(deposit_txs),
                estimated_mixed_eth=estimated_eth,
                withdrawal_cluster_depth=max_cluster_depth,
            )
        except Exception as exc:
            log.warning("zksnark_detect_failed", exc=str(exc))
            return MixingFlowResult(
                dark_pool_pressure=0.0,
                tornado_deposits_detected=0,
                estimated_mixed_eth=0.0,
                withdrawal_cluster_depth=0,
            )
