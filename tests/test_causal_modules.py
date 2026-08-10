"""Tests for src/causal/ — DoWhy SCM, Granger, AssetGNN."""

from __future__ import annotations

import numpy as np


# ─── Granger ──────────────────────────────────────────────────────────────────


class TestGrangerCausalityDetector:
    def test_initial_state(self) -> None:
        from src.causal.granger import GrangerCausalityDetector

        gcd = GrangerCausalityDetector(window=60)
        assert gcd.causal_symbols == []
        assert isinstance(gcd.to_feature_vector(), dict)

    def test_update_with_insufficient_data_no_crash(self) -> None:
        from src.causal.granger import GrangerCausalityDetector

        gcd = GrangerCausalityDetector(window=60)
        btc = np.random.randn(5)
        alt_prices = {"ETH": np.random.randn(5).tolist()}
        result = gcd.update(btc, alt_prices)
        # Below _MIN_WINDOW the detector returns its (still empty) result cache.
        assert result == {}

    def test_update_with_sufficient_data(self) -> None:
        from src.causal.granger import GrangerCausalityDetector, GrangerResult

        gcd = GrangerCausalityDetector(window=30, max_lag=2)
        n = 60
        btc = np.random.randn(n)
        alt_prices = {
            "ETH": (btc + np.random.randn(n) * 0.1).tolist(),
            "SOL": np.random.randn(n).tolist(),
        }
        result = gcd.update(btc, alt_prices)
        assert isinstance(result, dict)
        assert all(isinstance(r, GrangerResult) for r in result.values())

    def test_feature_vector_returns_floats(self) -> None:
        from src.causal.granger import GrangerCausalityDetector

        gcd = GrangerCausalityDetector(window=30, max_lag=2)
        n = 60
        btc = np.random.randn(n)
        alt = {"ETH": np.random.randn(n).tolist()}
        gcd.update(btc, alt)
        fv = gcd.to_feature_vector()
        for v in fv.values():
            assert isinstance(v, float)

    def test_granger_result_fields(self) -> None:
        from src.causal.granger import GrangerResult

        gr = GrangerResult(
            treatment="BTC",
            outcome="ETH",
            is_causal=True,
            min_pvalue=0.02,
            best_lag=1,
            f_stat=3.5,
        )
        assert gr.outcome == "ETH"
        assert gr.is_causal is True


# ─── DoWhy SCM ────────────────────────────────────────────────────────────────


class TestDoWhySCM:
    def test_estimate_effect_returns_causal_estimate(self) -> None:
        import pandas as pd

        from src.causal.dowhy_scm import DoWhySCM

        scm = DoWhySCM()
        np.random.seed(0)
        n = 200
        data = pd.DataFrame(
            {
                "whale_selling": np.random.randn(n),
                "btc_return": np.random.randn(n),
                "volatility": np.abs(np.random.randn(n)),
                "funding_rate": np.random.randn(n) * 0.001,
            }
        )
        result = scm.estimate_effect(data, treatment="whale_selling", outcome="btc_return")
        assert hasattr(result, "ate")
        assert isinstance(result.ate, float)

    def test_causal_signal_returns_dict(self) -> None:
        import pandas as pd

        from src.causal.dowhy_scm import DoWhySCM

        scm = DoWhySCM()
        data = pd.DataFrame(
            {
                "whale_selling": np.random.randn(50),
                "btc_return": np.random.randn(50),
                "volatility": np.abs(np.random.randn(50)),
                "funding_rate": np.random.randn(50) * 0.001,
            }
        )
        signals = scm.causal_signal(data)
        assert isinstance(signals, dict)

    def test_batch_estimate(self) -> None:
        import pandas as pd

        from src.causal.dowhy_scm import DoWhySCM

        scm = DoWhySCM()
        data = pd.DataFrame(
            {
                "whale_selling": np.random.randn(100),
                "btc_return": np.random.randn(100),
                "volatility": np.abs(np.random.randn(100)),
                "funding_rate": np.random.randn(100) * 0.001,
            }
        )
        pairs = [("whale_selling", "btc_return")]
        results = scm.batch_estimate(data, pairs)
        assert isinstance(results, list)
        assert len(results) == 1

    def test_causal_estimate_fields(self) -> None:
        from src.causal.dowhy_scm import CausalEstimate

        est = CausalEstimate(treatment="X", outcome="Y", ate=0.5, confidence=0.01, method="linear")
        assert est.ate == 0.5
        assert isinstance(est.confidence, float)


# ─── AssetGNN ─────────────────────────────────────────────────────────────────


class TestBuildCorrelationGraph:
    def test_returns_edge_list_and_weights(self) -> None:
        import pandas as pd

        from src.causal.asset_gnn import build_correlation_graph

        np.random.seed(0)
        price_df = pd.DataFrame(
            {
                "BTC": np.cumprod(1 + np.random.randn(80) * 0.01) * 50000,
                "ETH": np.cumprod(1 + np.random.randn(80) * 0.01) * 3000,
                "SOL": np.cumprod(1 + np.random.randn(80) * 0.01) * 100,
            }
        )
        edges, weights = build_correlation_graph(price_df)
        assert isinstance(edges, list)
        assert isinstance(weights, list)
        assert len(edges) == len(weights)

    def test_empty_df_returns_empty_graph(self) -> None:
        import pandas as pd

        from src.causal.asset_gnn import build_correlation_graph

        edges, weights = build_correlation_graph(pd.DataFrame())
        assert edges == []
        assert weights == []

    def test_threshold_filters_edges(self) -> None:
        import pandas as pd

        from src.causal.asset_gnn import build_correlation_graph

        np.random.seed(42)
        btc = np.cumprod(1 + np.random.randn(80) * 0.01) * 50000
        price_df = pd.DataFrame({"A": btc, "B": btc * 0.9})  # highly correlated
        edges_strict, _ = build_correlation_graph(price_df, threshold=0.99)
        edges_loose, _ = build_correlation_graph(price_df, threshold=0.0)
        assert len(edges_loose) >= len(edges_strict)


class TestAssetGNN:
    def test_run_empty_dfs_returns_empty_result(self) -> None:
        import pandas as pd

        from src.causal.asset_gnn import AssetGNN

        gnn = AssetGNN()
        result = gnn.run(pd.DataFrame(), pd.DataFrame())
        assert hasattr(result, "asset_embeddings")
        assert result.asset_embeddings == {}

    def test_run_with_data(self) -> None:
        import pandas as pd

        from src.causal.asset_gnn import AssetGNN

        np.random.seed(1)
        price_df = pd.DataFrame(
            {
                "BTC": np.cumprod(1 + np.random.randn(80) * 0.01),
                "ETH": np.cumprod(1 + np.random.randn(80) * 0.01),
            }
        )
        node_df = pd.DataFrame(
            {
                "vol": [0.01, 0.015],
                "momentum": [0.5, -0.3],
            },
            index=["BTC", "ETH"],
        )
        gnn = AssetGNN()
        result = gnn.run(price_df, node_df)
        assert hasattr(result, "asset_embeddings")
        assert hasattr(result, "contagion_scores")
