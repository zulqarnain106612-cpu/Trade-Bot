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
