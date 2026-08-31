"""A minimal in-memory stand-in for ``torch_geometric``.

torch_geometric is an optional dependency that is not installed in CI, so the
GAT code paths in :mod:`src.causal.asset_gnn` and :mod:`src.models.gnn_head`
never run there. These stubs are real ``nn.Module``s built on the installed
torch, so the production code under test executes for real -- only the
third-party layer is substituted.
"""

from __future__ import annotations

import sys
import types
from contextlib import contextmanager
from typing import Any

import torch
import torch.nn as nn


class FakeGATConv(nn.Module):
    """Linear stand-in with GATConv's constructor and ``forward(x, edge_index)``."""

    def __init__(
        self,
        in_channels: int,
        out_channels: int,
        heads: int = 1,
        dropout: float = 0.0,
        concat: bool = True,
    ) -> None:
        super().__init__()
        self.heads = heads
        self.concat = concat
        self.out_channels = out_channels
        fan_out = out_channels * heads if concat else out_channels
        self.lin = nn.Linear(in_channels, fan_out)

    def forward(self, x: torch.Tensor, edge_index: torch.Tensor) -> torch.Tensor:
        return self.lin(x)


def fake_global_mean_pool(x: torch.Tensor, batch: Any) -> torch.Tensor:
    """Mean-pool node embeddings per graph, mirroring the real signature."""
    if batch is None:
        return x.mean(dim=0, keepdim=True)
    batch = torch.as_tensor(batch)
    n_graphs = int(batch.max().item()) + 1
    return torch.stack([x[batch == g].mean(dim=0) for g in range(n_graphs)])


def build_modules() -> dict[str, types.ModuleType]:
    """Return the ``sys.modules`` entries that make ``import torch_geometric`` work."""
    root = types.ModuleType("torch_geometric")
    nn_mod = types.ModuleType("torch_geometric.nn")
    nn_mod.GATConv = FakeGATConv
    nn_mod.global_mean_pool = fake_global_mean_pool
    root.nn = nn_mod
    return {"torch_geometric": root, "torch_geometric.nn": nn_mod}


@contextmanager
def installed():
    """Context manager installing the stub package into ``sys.modules``."""
    saved = {k: sys.modules.get(k) for k in ("torch_geometric", "torch_geometric.nn")}
    sys.modules.update(build_modules())
    try:
        yield
    finally:
        for key, value in saved.items():
            if value is None:
                sys.modules.pop(key, None)
            else:
                sys.modules[key] = value
