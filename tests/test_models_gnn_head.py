"""Tests for src/models/gnn_head.py -- the GAT/MLP-fallback asset-graph head.

torch_geometric is an optional dependency not installed in CI, so
GNNHead's constructor and forward() naturally take the MLP-fallback path
here -- these tests cover exactly that path with real torch tensors.
"""

from __future__ import annotations

import torch

from src.models.gnn_head import GNNHead


def test_init_falls_back_when_torch_geometric_missing():
    head = GNNHead(node_features=8, hidden_dim=16, d_model=32)
    assert head._pyg_available is False


def test_forward_with_plain_2d_tensor_uses_fallback_and_pools_over_dim0():
    head = GNNHead(node_features=8, hidden_dim=16, d_model=32)
    x = torch.randn(5, 8)  # [N, F], one graph, no batch dim
    out = head.forward(x)
    assert out.shape == (1, 32)


def test_forward_with_batched_3d_tensor_pools_over_dim1():
    head = GNNHead(node_features=8, hidden_dim=16, d_model=32)
    x = torch.randn(4, 5, 8)  # [B, N, F]
    out = head.forward(x)
    assert out.shape == (4, 32)


def test_forward_with_object_exposing_x_attribute():
    head = GNNHead(node_features=8, hidden_dim=16, d_model=32)

    class _Data:
        x = torch.randn(6, 8)

    out = head.forward(_Data())
    assert out.shape == (1, 32)
