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
        # Not enough data — should return empty or partial result
        assert result is None or isinstance(result, list)

    def test_update_with_sufficient_data(self) -> None:
        from src.causal.granger import GrangerCausalityDetector

        gcd = GrangerCausalityDetector(window=30, max_lag=2)
        n = 60
        btc = np.random.randn(n)
        alt_prices = {
            "ETH": (btc + np.random.randn(n) * 0.1).tolist(),
            "SOL": np.random.randn(n).tolist(),
        }
        result = gcd.update(btc, alt_prices)
        assert result is None or isinstance(result, list)

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

        gr = GrangerResult(symbol="ETH", f_stat=3.5, p_value=0.02, is_causal=True, lag=1)
        assert gr.symbol == "ETH"
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
        assert hasattr(result, "effect")
        assert isinstance(result.effect, float)

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
        results = scm.batch_estimate(data)
        assert isinstance(results, list)

    def test_causal_estimate_fields(self) -> None:
        from src.causal.dowhy_scm import CausalEstimate

        est = CausalEstimate(treatment="X", outcome="Y", effect=0.5, p_value=0.01, method="linear")
        assert est.effect == 0.5
        assert isinstance(est.p_value, float)


# ─── AssetGNN ─────────────────────────────────────────────────────────────────


class TestBuildCorrelationGraph:
    def test_returns_asset_graph_result(self) -> None:
        from src.causal.asset_gnn import build_correlation_graph

        prices = {
            "BTC": np.random.randn(60).tolist(),
            "ETH": np.random.randn(60).tolist(),
            "SOL": np.random.randn(60).tolist(),
        }
        result = build_correlation_graph(prices)
        assert hasattr(result, "assets")
        assert hasattr(result, "edges")
        assert "BTC" in result.assets

    def test_empty_prices_returns_result(self) -> None:
        from src.causal.asset_gnn import build_correlation_graph

        result = build_correlation_graph({})
        assert hasattr(result, "assets")

    def test_threshold_filters_edges(self) -> None:
        from src.causal.asset_gnn import build_correlation_graph

        np.random.seed(42)
        prices = {
            "A": np.random.randn(60).tolist(),
            "B": np.random.randn(60).tolist(),
        }
        result_strict = build_correlation_graph(prices, corr_threshold=0.99)
        result_loose = build_correlation_graph(prices, corr_threshold=0.0)
        assert len(result_loose.edges) >= len(result_strict.edges)


class TestAssetGNN:
    def test_run_with_numpy_fallback(self) -> None:
        from src.causal.asset_gnn import AssetGNN

        gnn = AssetGNN(n_assets=3, d_in=4, d_out=8)
        features = np.random.randn(3, 4)
        edges = [(0, 1), (1, 2)]
        result = gnn.run(features, edges)
        assert hasattr(result, "embeddings")
        assert hasattr(result, "contagion_scores")

    def test_run_empty_edges(self) -> None:
        from src.causal.asset_gnn import AssetGNN

        gnn = AssetGNN(n_assets=2, d_in=4, d_out=8)
        features = np.random.randn(2, 4)
        result = gnn.run(features, [])
        assert hasattr(result, "embeddings")
