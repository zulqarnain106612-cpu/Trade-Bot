"""
E-18 — Network Centrality engine.

Exchange flow graph (networkx eigenvector centrality).
Whale cluster detection via PageRank.
Gap G-02 fix: concrete impl spec.
If no graph data available: confidence = 0 (engine abstains gracefully).
"""

from __future__ import annotations

from datetime import UTC, datetime

import structlog

from src.engines.schema import EngineOutput


log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

_ENGINE_ID = "E-18"
_SLA_SECONDS = 5

#: Mirrors src.data.exchange_flow_provider.SOURCE_TAG. Duplicated rather than
#: imported to keep the engine layer free of data-layer imports.
_NETFLOW_SOURCE = "defillama_cex_netflow"

#: Net inflow to exchanges above this share of gross flow reads as sell
#: pressure; the mirror value reads as accumulation. Provisional — see
#: DECISION_LOG.md, pending out-of-sample validation.
_IMBALANCE_THRESHOLD = 0.15


def exchange_flow_graph(flow_data: list[dict]) -> object:
    """Build directed exchange flow graph."""
    import networkx as nx  # type: ignore[import]

    g: nx.DiGraph = nx.DiGraph()
    for flow in flow_data:
        g.add_edge(
            flow["from"],
            flow["to"],
            weight=float(flow.get("usd_volume", 0.0)),
        )
    return g


def centrality_signal(g: object, target: str) -> float:
    """Eigenvector centrality of target exchange node."""
    import networkx as nx  # type: ignore[import]

    if not isinstance(g, nx.DiGraph) or g.number_of_nodes() == 0:
        return 0.0
    try:
        centrality = nx.eigenvector_centrality_numpy(g, weight="weight")
        return float(centrality.get(target, 0.0))
    except Exception:
        return 0.0


def whale_cluster_score(g: object, target: str) -> float:
    """PageRank-based whale concentration score."""
    import networkx as nx  # type: ignore[import]

    if not isinstance(g, nx.DiGraph) or g.number_of_nodes() == 0:
        return 0.0
    try:
        pr = nx.pagerank(g, weight="weight")
        return float(pr.get(target, 0.0))
    except Exception:
        return 0.0


# ---------------------------------------------------------------------------
# Netflow mode (free aggregate data)
#
# The functions above assume labelled pairwise transfers, which only paid
# providers supply. With free per-exchange netflow the graph is bipartite —
# every edge touches the synthetic MARKET hub — and eigenvector centrality is
# undefined there: networkx raises rather than returning a degenerate vector,
# so centrality_signal() would silently return 0.0 and pin direction to -1 on
# every tick. The functions below are the valid substitute for that topology.
# ---------------------------------------------------------------------------


def is_netflow_mode(flow_data: list[dict]) -> bool:
    """True when every record came from an aggregate-netflow source."""
    return bool(flow_data) and all(f.get("source") == _NETFLOW_SOURCE for f in flow_data)


def flow_concentration(flow_data: list[dict], target: str) -> float:
    """Share of total absolute exchange flow attributable to ``target``.

    Bounded [0, 1] and well defined on a star graph, unlike eigenvector
    centrality. High values mean flow is concentrated on one venue.
    """
    total = sum(abs(float(f.get("usd_volume", 0.0))) for f in flow_data)
    if total <= 0.0:
        return 0.0
    hit = sum(
        abs(float(f.get("usd_volume", 0.0))) for f in flow_data if f.get("exchange") == target
    )
    return float(min(1.0, hit / total))


def netflow_imbalance(flow_data: list[dict]) -> float:
    """Signed market-wide netflow as a fraction of gross flow, in [-1, 1].

    Positive means net capital moving *into* exchanges (supply available to
    sell); negative means net withdrawal to self-custody.
    """
    gross = sum(abs(float(f.get("net_usd", 0.0))) for f in flow_data)
    if gross <= 0.0:
        return 0.0
    net = sum(float(f.get("net_usd", 0.0)) for f in flow_data)
    return float(max(-1.0, min(1.0, net / gross)))


class E18Network:
    def __init__(self, horizon_hours: int = 4) -> None:
        self._horizon = horizon_hours

    async def run(self, symbol: str, data: dict) -> EngineOutput:
        spot: float = data.get("spot", 0.0)
        flow_data: list[dict] = data.get("exchange_flows", [])

        if spot <= 0:
            return EngineOutput.abstain(_ENGINE_ID, symbol, spot, self._horizon, "no_spot")

        if not flow_data:
            return EngineOutput.abstain(_ENGINE_ID, symbol, spot, self._horizon, "no_flow_data")

        # Aggregate-netflow data needs no graph library; dispatch before the
        # networkx import so this path survives networkx being absent.
        if is_netflow_mode(flow_data):
            return self._run_netflow(symbol, spot, flow_data, data)

        try:
            import networkx as nx  # type: ignore[import]

            g = exchange_flow_graph(flow_data)
            target_exchange = data.get("primary_exchange", "Binance")

            ec = centrality_signal(g, target_exchange)
            ws = whale_cluster_score(g, target_exchange)

            # High centrality + high whale score → accumulation signal
            combined = (ec + ws) / 2.0
            direction = 1 if combined > 0.6 else (-1 if combined < 0.3 else 0)

            dominant = max(
                (n for n in nx.nodes(g)),
                key=lambda n: g.out_degree(n, weight="weight"),  # type: ignore[attr-defined]
                default="unknown",
            )

            return EngineOutput(
                engine_id=_ENGINE_ID,
                symbol=symbol,
                timestamp_utc=datetime.now(UTC),
                predicted_price=spot * (1 + direction * 0.001),
                confidence=combined,
                direction=direction,
                horizon_hours=self._horizon,
                metadata={
                    "exchange_centrality": ec,
                    "whale_cluster_score": ws,
                    "dominant_exchange": dominant,
                },
            )
        except ImportError:
            return EngineOutput.abstain(
                _ENGINE_ID, symbol, spot, self._horizon, "networkx_not_installed"
            )
        except Exception as exc:
            log.warning("e18_error", exc=str(exc))
            return EngineOutput.abstain(_ENGINE_ID, symbol, spot, self._horizon, str(exc))

    def _run_netflow(
        self, symbol: str, spot: float, flow_data: list[dict], data: dict
    ) -> EngineOutput:
        """Signal from aggregate per-exchange netflow (free data path).

        Net inflow to exchanges is sell pressure (bearish); net withdrawal is
        accumulation (bullish). Confidence scales with how decisive the
        imbalance is, damped by how concentrated flow is on a single venue —
        a market-wide move is more informative than one exchange's rebalance.
        """
        try:
            target = str(data.get("primary_exchange", "Binance"))
            imbalance = netflow_imbalance(flow_data)
            concentration = flow_concentration(flow_data, target)

            if imbalance >= _IMBALANCE_THRESHOLD:
                direction = -1
            elif imbalance <= -_IMBALANCE_THRESHOLD:
                direction = 1
            else:
                direction = 0

            # Single-venue dominance means the aggregate reads as one desk's
            # internal transfer, so discount it rather than trusting it.
            confidence = min(1.0, abs(imbalance)) * (1.0 - min(1.0, concentration))

            dominant = max(
                flow_data,
                key=lambda f: abs(float(f.get("usd_volume", 0.0))),
                default={},
            ).get("exchange", "unknown")

            return EngineOutput(
                engine_id=_ENGINE_ID,
                symbol=symbol,
                timestamp_utc=datetime.now(UTC),
                predicted_price=spot * (1 + direction * 0.001),
                confidence=float(confidence),
                direction=direction,
                horizon_hours=self._horizon,
                metadata={
                    "mode": "netflow",
                    "netflow_imbalance": imbalance,
                    "flow_concentration": concentration,
                    "target_exchange": target,
                    "dominant_exchange": dominant,
                    "venue_count": len(flow_data),
                },
            )
        except Exception as exc:
            log.warning("e18_netflow_error", exc=str(exc))
            return EngineOutput.abstain(_ENGINE_ID, symbol, spot, self._horizon, str(exc))
