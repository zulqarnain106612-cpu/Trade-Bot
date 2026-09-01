"""Covers src/models/gnn_head.py's GAT path (torch_geometric installed).

The MLP-fallback path is covered in ``test_models_gnn_head.py``; CI has no
torch_geometric, so these tests install a stub package to run the real GAT
branch of GNNHead.__init__/forward.
"""

from __future__ import annotations

import importlib

import torch
from pyg_stub import installed

gnn_head = importlib.import_module("src.models.gnn_head")


def _head(**kwargs):
    return gnn_head.GNNHead(node_features=8, hidden_dim=6, n_heads=2, d_model=16, **kwargs)


def test_init_builds_three_gat_layers_when_pyg_is_available():
    with installed():
        head = _head()
    assert head._pyg_available is True
    assert head.conv1.lin.in_features == 8
    # layer 2 consumes layer 1's concatenated heads
    assert head.conv2.lin.in_features == 6 * 2
    # the final layer collapses the heads (concat=False)
    assert head.conv3.concat is False


def test_forward_pools_graph_nodes_through_the_gat_stack():
    with installed():
        head = _head()
        data = type(
            "Data",
            (),
            {
                "x": torch.randn(6, 8),
                "edge_index": torch.tensor([[0, 1, 2], [1, 2, 3]]),
                "batch": torch.tensor([0, 0, 0, 1, 1, 1]),
            },
        )()
        out = head.forward(data)
    assert out.shape == (2, 16)
