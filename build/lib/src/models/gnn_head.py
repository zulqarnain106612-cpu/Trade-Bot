"""
GNN head — 3-layer Graph Attention Network (GAT) over asset correlation graph.

Uses torch_geometric for graph operations.
Nodes = crypto assets; edges = rolling Pearson correlation (abs(rho) > threshold).
Each node holds a feature vector; GAT aggregates neighbor information with
8 attention heads per layer and edge dropout 0.1.

Input:  torch_geometric.data.Data with x=[N, F], edge_index=[2, E], batch=[N]
Output: [B, 128] — global mean pool of node embeddings
"""

from __future__ import annotations

from typing import Any

import structlog
import torch
import torch.nn as nn

log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)


class GNNHead(nn.Module):
    """
    3-layer Graph Attention Network (GAT) for asset correlation graph.

    Falls back gracefully to a simple MLP when torch_geometric is not installed.
    """

    def __init__(
        self,
        node_features: int = 32,
        hidden_dim: int = 64,
        n_heads: int = 8,
        edge_dropout: float = 0.1,
        d_model: int = 128,
    ) -> None:
        super().__init__()
        self._pyg_available = False
        try:
            from torch_geometric.nn import GATConv  # type: ignore[import]

            self.conv1 = GATConv(
                node_features, hidden_dim, heads=n_heads, dropout=edge_dropout, concat=True
            )
            self.conv2 = GATConv(
                hidden_dim * n_heads, hidden_dim, heads=n_heads, dropout=edge_dropout, concat=True
            )
            self.conv3 = GATConv(
                hidden_dim * n_heads, hidden_dim, heads=1, dropout=edge_dropout, concat=False
            )
            self._pyg_available = True
        except ImportError:
            log.warning("torch_geometric_not_installed_using_mlp_fallback_head")

        self.fallback_mlp = nn.Sequential(
            nn.Linear(node_features, hidden_dim),
            nn.ReLU(),
            nn.Linear(hidden_dim, hidden_dim),
        )
        self.proj = nn.Sequential(
            nn.Linear(hidden_dim, d_model),
            nn.LayerNorm(d_model),
        )
        self.elu = nn.ELU()

    def forward(self, data: Any) -> torch.Tensor:  # type: ignore[override]
        """
        data: torch_geometric Data object with x, edge_index, batch
              OR a plain tensor [B*N, F] (fallback path)
        returns: [B, 128]
        """
        if self._pyg_available:
            from torch_geometric.nn import global_mean_pool  # type: ignore[import]

            x, edge_index, batch = data.x, data.edge_index, data.batch
            x = self.elu(self.conv1(x, edge_index))
            x = self.elu(self.conv2(x, edge_index))
            x = self.elu(self.conv3(x, edge_index))
            pooled = global_mean_pool(x, batch)  # [B, hidden_dim]
            return self.proj(pooled)

        # Fallback: mean-pool over nodes, mirroring global_mean_pool above.
        x = data if isinstance(data, torch.Tensor) else data.x
        return self.proj(self._pool_nodes(x))

    def _pool_nodes(self, x: torch.Tensor) -> torch.Tensor:
        """[B, N, F] → [B, hidden]; [N, F] (one graph) → [1, hidden]."""
        h = self.fallback_mlp(x)
        return h.mean(dim=1) if x.dim() == 3 else h.mean(dim=0, keepdim=True)
