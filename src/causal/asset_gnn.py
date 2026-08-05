"""
Asset GNN — Graph Attention Network over asset correlation graph.

Nodes: crypto assets (BTC, ETH, SOL, BNB, ...)
Edges: Pearson correlation (abs(rho) > threshold over a rolling window)
Node features: returns, volatility, volume, regime, on-chain metrics

Detects cross-asset momentum and contagion using a 3-layer GAT.

Output: dict mapping asset_id → 128-dim embedding + contagion_score
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd
import structlog


log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

_CORRELATION_THRESHOLD = 0.6


@dataclass
class AssetGraphResult:
    asset_embeddings: dict[str, list[float]]  # asset → 128-dim embedding
    edge_count: int
    contagion_scores: dict[str, float]  # asset → contagion risk score


def build_correlation_graph(
    price_df: pd.DataFrame,
    threshold: float = _CORRELATION_THRESHOLD,
    window: int = 60,
) -> tuple[list[tuple[int, int]], list[float]]:
    """
    Build a correlation-based edge list from a price DataFrame.

    price_df: columns = asset names, rows = time steps, values = prices.
    Returns (edge_index list of (src, dst) pairs, edge weights).
    """
    returns = price_df.pct_change().dropna()
    if len(returns) < window:
        returns_win = returns
    else:
        returns_win = returns.iloc[-window:]

    corr_matrix = returns_win.corr().fillna(0.0)
    n_assets = len(corr_matrix)
    edges = []
    weights = []

    for i in range(n_assets):
        for j in range(i + 1, n_assets):
            rho = abs(float(corr_matrix.iloc[i, j]))
            if rho >= threshold:
                edges.append((i, j))
                edges.append((j, i))
                weights.extend([rho, rho])

    return edges, weights


class AssetGNNLayer:
    """
    Single GAT layer using numpy (no torch_geometric dependency required).

    Falls back to a simpler weighted-mean aggregation when torch_geometric
    is not available.
    """

    def __init__(self, in_dim: int, out_dim: int, n_heads: int = 8) -> None:
        self._in_dim = in_dim
        self._out_dim = out_dim
        self._n_heads = n_heads
        self._pyg = self._try_import_pyg()

    def _try_import_pyg(self) -> bool:
        try:
            import torch_geometric  # type: ignore[import]  # noqa: F401

            return True
        except ImportError:
            return False

    def aggregate(
        self,
        node_features: np.ndarray,
        edge_list: list[tuple[int, int]],
        edge_weights: list[float],
    ) -> np.ndarray:
        """
        One pass of weighted-mean aggregation (simplified GAT without softmax attention).

        node_features: [N, in_dim]
        Returns: [N, in_dim] aggregated features
        """
        n = len(node_features)
        agg = np.zeros_like(node_features)
        weight_sum = np.zeros(n)

        for (src, dst), w in zip(edge_list, edge_weights, strict=False):
            agg[dst] += w * node_features[src]
            weight_sum[dst] += w

        mask = weight_sum > 0
        agg[mask] = agg[mask] / weight_sum[mask, np.newaxis]
        # Residual connection
        return node_features + agg


class AssetGNN:
    """
    3-layer GAT-inspired aggregation over the crypto asset correlation graph.

    When torch_geometric is installed, uses proper GATConv with 8 heads.
    Falls back to the numpy aggregation above.
    """

    def __init__(
        self,
        n_layers: int = 3,
        hidden_dim: int = 64,
        n_heads: int = 8,
        edge_dropout: float = 0.1,
        d_model: int = 128,
    ) -> None:
        self._layers = [AssetGNNLayer(hidden_dim, hidden_dim, n_heads) for _ in range(n_layers)]
        self._hidden = hidden_dim
        self._d_model = d_model
        self._pyg_model: object | None = None
        self._try_build_pyg_model(hidden_dim, n_layers, n_heads, edge_dropout, d_model)

    def _try_build_pyg_model(
        self, hidden_dim: int, n_layers: int, n_heads: int, edge_dropout: float, d_model: int
    ) -> None:
        try:
            import torch
            import torch.nn as nn
            from torch_geometric.nn import GATConv  # type: ignore[import]

            class _PYGModel(nn.Module):
                def __init__(self) -> None:
                    super().__init__()
                    self.convs = nn.ModuleList()
                    in_ch = hidden_dim
                    for _ in range(n_layers - 1):
                        self.convs.append(
                            GATConv(
                                in_ch, hidden_dim, heads=n_heads, dropout=edge_dropout, concat=True
                            )
                        )
                        in_ch = hidden_dim * n_heads
                    self.convs.append(
                        GATConv(in_ch, hidden_dim, heads=1, dropout=edge_dropout, concat=False)
                    )
                    self.proj = nn.Linear(hidden_dim, d_model)
                    self.elu = nn.ELU()

                def forward(self, x: torch.Tensor, edge_index: torch.Tensor) -> torch.Tensor:
                    for conv in self.convs:
                        x = self.elu(conv(x, edge_index))
                    return self.proj(x)

            self._pyg_model = _PYGModel()
            log.info("asset_gnn_pyg_model_ready")
        except ImportError:
            pass

    def run(
        self,
        price_df: pd.DataFrame,
        node_features_df: pd.DataFrame,
    ) -> AssetGraphResult:
        """
        Run the GNN over the asset correlation graph.

        price_df: [T, N_assets] prices for correlation computation
        node_features_df: [N_assets, F] per-asset feature vectors (returns, vol, etc.)
        """
        if price_df.empty or node_features_df.empty:
            return AssetGraphResult(asset_embeddings={}, edge_count=0, contagion_scores={})

        edges, weights = build_correlation_graph(price_df)
        asset_names = list(price_df.columns)
        features = node_features_df.values.astype(np.float32)  # [N, F]

        if self._pyg_model is not None:
            embeddings = self._run_pyg(features, edges)
        else:
            embeddings = self._run_numpy(features, edges, weights)

        contagion = self._compute_contagion(embeddings, edges)

        return AssetGraphResult(
            asset_embeddings={
                asset_names[i]: embeddings[i].tolist() for i in range(len(asset_names))
            },
            edge_count=len(edges) // 2,
            contagion_scores={asset_names[i]: float(contagion[i]) for i in range(len(asset_names))},
        )

    def _run_pyg(self, features: np.ndarray, edges: list[tuple[int, int]]) -> np.ndarray:
        import torch

        x = torch.tensor(features)
        if x.shape[1] != self._hidden:
            proj = torch.nn.Linear(x.shape[1], self._hidden, bias=False)
            x = proj(x)
        if not edges:
            return x.detach().numpy()
        edge_index = torch.tensor(edges, dtype=torch.long).T
        out = self._pyg_model(x, edge_index)  # type: ignore[call-arg]
        return out.detach().numpy()

    def _run_numpy(
        self, features: np.ndarray, edges: list[tuple[int, int]], weights: list[float]
    ) -> np.ndarray:
        h = features
        if h.shape[1] < self._hidden:
            pad = np.zeros((h.shape[0], self._hidden - h.shape[1]))
            h = np.concatenate([h, pad], axis=1)
        for layer in self._layers:
            h = layer.aggregate(h, edges, weights)
        return h

    def _compute_contagion(
        self, embeddings: np.ndarray, edges: list[tuple[int, int]]
    ) -> np.ndarray:
        """Contagion score = number of high-corr connections x embedding norm."""
        n = len(embeddings)
        degree = np.zeros(n)
        for src, _dst in edges:
            degree[src] += 1
        norms = np.linalg.norm(embeddings, axis=1)
        return degree * norms / (np.max(degree * norms) + 1e-9)
