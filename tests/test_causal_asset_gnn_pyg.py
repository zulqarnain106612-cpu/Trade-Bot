"""Covers src/causal/asset_gnn.py's torch_geometric-backed GAT path.

CI has no torch_geometric installed, so AssetGNN falls back to numpy and the
GAT branch never executes. These tests install a stub package (see
``pyg_stub``) so the real production branch runs against real torch tensors.
"""

from __future__ import annotations

import importlib

import numpy as np
import pandas as pd
import pytest
from pyg_stub import installed

asset_gnn = importlib.import_module("src.causal.asset_gnn")


def _price_df(n_rows: int = 80) -> pd.DataFrame:
    rng = np.random.default_rng(7)
    btc = np.cumsum(rng.normal(0, 1, n_rows)) + 100
    eth = btc * 0.9 + rng.normal(0, 0.05, n_rows)
    return pd.DataFrame({"BTC": btc, "ETH": eth})


def test_layer_reports_pyg_available_when_package_imports():
    with installed():
        layer = asset_gnn.AssetGNNLayer(4, 4)
    assert layer._pyg is True


def test_model_is_built_when_torch_geometric_is_importable():
    with installed():
        gnn = asset_gnn.AssetGNN(n_layers=3, hidden_dim=8, n_heads=2, d_model=16)
    assert gnn._pyg_model is not None


def test_run_uses_the_gat_model_and_returns_d_model_embeddings():
    with installed():
        gnn = asset_gnn.AssetGNN(n_layers=2, hidden_dim=8, n_heads=2, d_model=16)
        features = pd.DataFrame(np.random.default_rng(1).normal(size=(2, 5)))
        result = gnn.run(_price_df(), features)

    assert set(result.asset_embeddings) == {"BTC", "ETH"}
    # d_model wide, because the GAT projection head ran rather than the
    # numpy fallback (which would have kept hidden_dim=8 columns).
    assert len(result.asset_embeddings["BTC"]) == 16
    assert result.edge_count >= 1


def test_run_pyg_skips_the_projection_when_features_already_match_hidden_dim():
    with installed():
        gnn = asset_gnn.AssetGNN(n_layers=2, hidden_dim=5, n_heads=2, d_model=16)
        features = np.random.default_rng(2).normal(size=(2, 5)).astype(np.float32)
        out = gnn._run_pyg(features, [(0, 1), (1, 0)])
    assert out.shape == (2, 16)


def test_run_pyg_returns_unconvolved_nodes_when_the_graph_has_no_edges():
    with installed():
        gnn = asset_gnn.AssetGNN(n_layers=2, hidden_dim=5, n_heads=2, d_model=16)
        features = np.random.default_rng(3).normal(size=(3, 5)).astype(np.float32)
        out = gnn._run_pyg(features, [])
    # No edges -> the projected node features come straight back, still
    # hidden_dim wide because the GAT stack was skipped entirely.
    assert out.shape == (3, 5)


@pytest.mark.parametrize("empty", ["prices", "features"])
def test_run_returns_empty_result_for_empty_inputs(empty):
    with installed():
        gnn = asset_gnn.AssetGNN(n_layers=2, hidden_dim=8, n_heads=2, d_model=16)
        prices = pd.DataFrame() if empty == "prices" else _price_df()
        features = pd.DataFrame() if empty == "features" else pd.DataFrame(np.zeros((2, 5)))
        result = gnn.run(prices, features)
    assert result.edge_count == 0
    assert result.asset_embeddings == {}
