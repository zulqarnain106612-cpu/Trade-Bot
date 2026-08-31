"""Tests for src/causal/asset_gnn.py -- correlation-graph GAT (numpy fallback).

torch_geometric is optional and not installed in CI, so AssetGNNLayer/
AssetGNN naturally exercise their numpy-fallback paths here, matching
production CI.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from src.causal.asset_gnn import AssetGNN, AssetGNNLayer, build_correlation_graph


def _price_df(n_rows: int = 80) -> pd.DataFrame:
    rng = np.random.default_rng(42)
    btc = np.cumsum(rng.normal(0, 1, n_rows)) + 100
    eth = btc * 0.9 + rng.normal(0, 0.1, n_rows)  # highly correlated with btc
    sol = np.cumsum(rng.normal(0, 1, n_rows)) + 50  # independent
    return pd.DataFrame({"BTC": btc, "ETH": eth, "SOL": sol})


def test_build_correlation_graph_links_correlated_assets():
    edges, weights = build_correlation_graph(_price_df(), threshold=0.6, window=60)
    assert len(edges) == len(weights)
    assert len(edges) > 0
    # every edge must appear in both directions
    assert all((b, a) in edges for a, b in edges)


def test_build_correlation_graph_high_threshold_yields_no_edges():
    edges, weights = build_correlation_graph(_price_df(), threshold=1.1, window=60)
    assert edges == []
    assert weights == []


def test_build_correlation_graph_uses_full_history_when_shorter_than_window():
    edges, weights = build_correlation_graph(_price_df(n_rows=10), threshold=0.6, window=60)
    assert isinstance(edges, list)


def test_asset_gnn_layer_falls_back_without_torch_geometric():
    layer = AssetGNNLayer(in_dim=4, out_dim=4)
    assert layer._pyg is False


def test_asset_gnn_layer_aggregate_applies_weighted_mean_with_residual():
    layer = AssetGNNLayer(in_dim=2, out_dim=2)
    features = np.array([[1.0, 1.0], [2.0, 2.0], [3.0, 3.0]])
    edges = [(0, 1), (2, 1)]
    weights = [1.0, 1.0]
    out = layer.aggregate(features, edges, weights)
    # node 1 receives mean of nodes 0 and 2 (weights equal) plus its own residual
    expected_node1 = features[1] + (features[0] + features[2]) / 2
    assert np.allclose(out[1], expected_node1)
    # node 0 has no incoming edges -> residual only, no aggregation added
    assert np.allclose(out[0], features[0])


def test_asset_gnn_layer_aggregate_handles_no_edges():
    layer = AssetGNNLayer(in_dim=2, out_dim=2)
    features = np.array([[1.0, 1.0], [2.0, 2.0]])
    out = layer.aggregate(features, [], [])
    assert np.allclose(out, features)


def test_asset_gnn_init_without_pyg_model():
    gnn = AssetGNN(n_layers=2, hidden_dim=8, d_model=16)
    assert gnn._pyg_model is None


def test_asset_gnn_run_empty_price_df_returns_empty_result():
    gnn = AssetGNN(n_layers=2, hidden_dim=8, d_model=16)
    result = gnn.run(pd.DataFrame(), pd.DataFrame({"BTC": [1.0]}))
    assert result.asset_embeddings == {}
    assert result.edge_count == 0
    assert result.contagion_scores == {}


def test_asset_gnn_run_empty_node_features_returns_empty_result():
    gnn = AssetGNN(n_layers=2, hidden_dim=8, d_model=16)
    result = gnn.run(_price_df(), pd.DataFrame())
    assert result.asset_embeddings == {}


def test_asset_gnn_run_numpy_path_produces_embeddings_and_contagion():
    gnn = AssetGNN(n_layers=2, hidden_dim=8, d_model=16)
    prices = _price_df()
    node_features = pd.DataFrame(
        {"ret": [0.01, 0.01, 0.01], "vol": [0.2, 0.2, 0.2]}, index=["BTC", "ETH", "SOL"]
    )
    result = gnn.run(prices, node_features)
    assert set(result.asset_embeddings) == {"BTC", "ETH", "SOL"}
    assert set(result.contagion_scores) == {"BTC", "ETH", "SOL"}
    assert result.edge_count >= 1
    for emb in result.asset_embeddings.values():
        assert len(emb) == 8  # hidden_dim, since features get padded not projected in numpy path


def test_asset_gnn_run_numpy_path_pads_features_narrower_than_hidden_dim():
    gnn = AssetGNN(n_layers=1, hidden_dim=6, d_model=16)
    prices = _price_df()
    node_features = pd.DataFrame({"ret": [0.01, 0.01, 0.01]}, index=["BTC", "ETH", "SOL"])
    result = gnn.run(prices, node_features)
    for emb in result.asset_embeddings.values():
        assert len(emb) == 6
