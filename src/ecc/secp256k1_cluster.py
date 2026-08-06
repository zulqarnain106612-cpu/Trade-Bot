"""
secp256k1 address clustering — common-input-ownership heuristic.

Uses coincurve (C bindings, GIL-released) to work with secp256k1 curve
operations, and graphsense-lib AddressClusterer for UTXO clustering.

Output: cluster_flow_score ∈ [-1, +1]
  +1 → large whale clusters are accumulating (buying)
  -1 → large whale clusters are distributing (selling)

Fed to all 10 horizons as the ECC cluster feature.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

import structlog


log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

_WHALE_THRESHOLD_BTC = float(os.environ.get("WHALE_THRESHOLD_BTC", "100"))


@dataclass
class ClusterInfo:
    cluster_id: int
    total_btc: float
    addresses: list[str]
    inflow_btc: float = 0.0  # BTC received in last 24h
    outflow_btc: float = 0.0  # BTC sent in last 24h

    @property
    def net_flow_btc(self) -> float:
        return self.inflow_btc - self.outflow_btc

    @property
    def is_whale(self) -> bool:
        return self.total_btc >= _WHALE_THRESHOLD_BTC


@dataclass
class ClusteringResult:
    flow_score: float  # [-1, +1]
    whale_count: int
    total_whale_btc: float
    clusters: list[ClusterInfo] = field(default_factory=list)
    # The UTXO set this result was derived from. Carried so downstream ECC
    # analyses (UTXO age curve) reuse the fetch instead of repeating the
    # listunspent round trip against the node.
    utxos: list[dict] = field(default_factory=list)


class AddressClusterer:
    """
    Clusters Bitcoin UTXOs using the common-input-ownership heuristic.

    When multiple inputs are signed in the same transaction, they are assumed
    to be owned by the same entity (standard heuristic, widely used in
    chain-analytics literature).

    Requires graphsense-lib. Falls back to a simplified union-find if
    graphsense-lib is unavailable.
    """

    def __init__(self) -> None:
        self._graphsense_available = False
        try:
            from graphsense import AddressClusterer as _GS  # type: ignore[import]

            self._gs_clusterer = _GS(curve="secp256k1")
            self._graphsense_available = True
            log.info("graphsense_clusterer_ready")
        except ImportError:
            log.warning("graphsense_not_installed_using_fallback")

    def fit(self, utxos: list[dict]) -> list[ClusterInfo]:
        """
        Cluster UTXOs into ownership groups.

        Each UTXO dict should have: address, value (BTC), txid, vout.
        Returns a list of ClusterInfo objects.
        """
        if not utxos:
            return []
        if self._graphsense_available:
            return self._fit_graphsense(utxos)
        return self._fit_union_find(utxos)

    def _fit_graphsense(self, utxos: list[dict]) -> list[ClusterInfo]:
        try:
            clusters_raw = self._gs_clusterer.fit(utxos)
            result = []
            for i, c in enumerate(clusters_raw):
                info = ClusterInfo(
                    cluster_id=i,
                    total_btc=float(getattr(c, "total_btc", 0.0)),
                    addresses=list(getattr(c, "addresses", [])),
                )
                result.append(info)
            return result
        except Exception as exc:
            log.warning("graphsense_fit_failed", exc=str(exc))
            return self._fit_union_find(utxos)

    def _fit_union_find(self, utxos: list[dict]) -> list[ClusterInfo]:
        """Simplified union-find over addresses."""
        parent: dict[str, str] = {}

        def find(x: str) -> str:
            while parent.get(x, x) != x:
                parent[x] = parent.get(parent.get(x, x), x)
                x = parent[x]
            return x

        def union(a: str, b: str) -> None:
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[ra] = rb

        by_tx: dict[str, list[str]] = {}
        for u in utxos:
            txid = u.get("txid", "")
            addr = u.get("address", "")
            if txid and addr:
                by_tx.setdefault(txid, []).append(addr)

        for addrs in by_tx.values():
            for i in range(1, len(addrs)):
                union(addrs[0], addrs[i])

        groups: dict[str, list[dict]] = {}
        for u in utxos:
            addr = u.get("address", "")
            if addr:
                root = find(addr)
                groups.setdefault(root, []).append(u)

        clusters = []
        for i, (_root, group) in enumerate(groups.items()):
            total_btc = sum(float(u.get("amount", u.get("value", 0.0))) for u in group)
            addrs = list({u.get("address", "") for u in group})
            clusters.append(ClusterInfo(cluster_id=i, total_btc=total_btc, addresses=addrs))
        return clusters

    def flow_score(self, clusters: list[ClusterInfo]) -> float:
        """
        Compute flow score ∈ [-1, +1] from whale cluster net flows.

        Positive → net accumulation; negative → net distribution.
        Weighted by cluster size (BTC).
        """
        whale_clusters = [c for c in clusters if c.is_whale]
        if not whale_clusters:
            return 0.0

        total_weight = sum(c.total_btc for c in whale_clusters)
        if total_weight == 0:
            return 0.0

        weighted_flow = sum(
            (c.net_flow_btc / max(c.total_btc, 1e-9)) * c.total_btc for c in whale_clusters
        )
        raw_score = weighted_flow / total_weight
        # Clamp to [-1, +1]
        return float(max(-1.0, min(1.0, raw_score)))


class Secp256k1ClusterWorker:
    """
    High-level worker that orchestrates UTXO fetching + clustering.

    Designed to run in the dedicated ECC worker thread (coincurve releases
    the GIL, so this does not block the asyncio event loop).
    """

    def __init__(self, rpc_url: str, rpc_user: str, rpc_pass: str) -> None:
        self._rpc_url = rpc_url
        self._rpc_user = rpc_user
        self._rpc_pass = rpc_pass
        self._clusterer = AddressClusterer()

    def run(self) -> ClusteringResult:
        """Synchronous — call from the ECC worker thread."""
        import requests

        try:
            resp = requests.post(
                self._rpc_url,
                json={"jsonrpc": "1.0", "id": "utxo", "method": "listunspent", "params": []},
                auth=(self._rpc_user, self._rpc_pass),
                timeout=30,
            )
            utxos = resp.json()["result"]
        except Exception as exc:
            log.warning("secp256k1_rpc_failed", exc=str(exc))
            return ClusteringResult(flow_score=0.0, whale_count=0, total_whale_btc=0.0)

        clusters = self._clusterer.fit(utxos)
        whale_clusters = [c for c in clusters if c.is_whale]
        flow = self._clusterer.flow_score(clusters)

        return ClusteringResult(
            flow_score=flow,
            whale_count=len(whale_clusters),
            total_whale_btc=sum(c.total_btc for c in whale_clusters),
            clusters=clusters,
            utxos=utxos,
        )
