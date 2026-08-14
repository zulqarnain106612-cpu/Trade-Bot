"""Tests for derivatives features, NLP features, and TFT model head."""

from __future__ import annotations

import torch


# ─── Derivatives ──────────────────────────────────────────────────────────────


class TestDerivativesFeatureExtractor:
    def test_extract_empty_returns_zeros(self) -> None:
        from src.features.derivatives import DerivativesFeatureExtractor

        ext = DerivativesFeatureExtractor()
        ft = ext.extract({})
        assert ft.open_interest_usd == 0.0
        assert ft.funding_rate == 0.0
        assert ft.liquidation_pressure == 0.0

    def test_extract_with_data(self) -> None:
        from src.features.derivatives import DerivativesFeatureExtractor

        ext = DerivativesFeatureExtractor()
        data = {"oi_usd": 1e9, "funding_rate": 0.0001, "liquidations_usd": 5e6}
        ft = ext.extract(data)
        assert ft.open_interest_usd == 1e9
        assert ft.funding_rate == 0.0001

    def test_to_feature_vector_returns_floats(self) -> None:
        from src.features.derivatives import DerivativesFeatureExtractor

        ext = DerivativesFeatureExtractor()
        ft = ext.extract({"oi_usd": 1e9, "funding_rate": -0.0001, "liquidations_usd": 1e6})
        fv = ext.to_feature_vector(ft)
        assert isinstance(fv, dict)
        for v in fv.values():
            assert isinstance(v, float)

    def test_extract_partial_data(self) -> None:
        from src.features.derivatives import DerivativesFeatureExtractor

        ext = DerivativesFeatureExtractor()
        ft = ext.extract({"funding_rate": 0.0005})
        assert ft.funding_rate == 0.0005
        assert ft.open_interest_usd == 0.0

    def test_derivatives_features_dataclass(self) -> None:
        from src.features.derivatives import DerivativesFeatures

        ft = DerivativesFeatures(
            open_interest_usd=1e9,
            funding_rate=0.001,
            liquidation_pressure=2e6,
            oi_change_pct=0.0,
            funding_premium=0.0,
        )
        assert ft.open_interest_usd > 0
        assert isinstance(ft.funding_rate, float)


# ─── NLP ──────────────────────────────────────────────────────────────────────


class TestNLPFeatures:
    def test_get_nlp_features_empty_list(self) -> None:
        from src.features.nlp import get_nlp_features

        result = get_nlp_features([])
        assert hasattr(result, "sentiment_score")
        assert -1.0 <= result.sentiment_score <= 1.0

    def test_get_nlp_features_single_text(self) -> None:
        from src.features.nlp import get_nlp_features

        result = get_nlp_features(["Bitcoin price is rising strongly."])
        assert hasattr(result, "sentiment_score")
        assert hasattr(result, "embedding")

    def test_get_nlp_features_multiple_texts(self) -> None:
        from src.features.nlp import get_nlp_features

        texts = ["BTC up 5%", "Market looks bearish", "Hold strong"]
        result = get_nlp_features(texts)
        assert hasattr(result, "sentiment_score")

    def test_nlp_features_dataclass(self) -> None:
        import numpy as np

        from src.features.nlp import NLPFeatures

        ft = NLPFeatures(sentiment_score=0.5, embedding=np.zeros(128), confidence=0.9)
        assert ft.sentiment_score == 0.5
        assert ft.embedding.shape == (128,)

    def test_singleton_pipeline(self) -> None:
        from src.features.nlp import _CryptoBERTPipeline

        inst1 = _CryptoBERTPipeline.instance()
        inst2 = _CryptoBERTPipeline.instance()
        assert inst1 is inst2


# ─── TFT Model Head ───────────────────────────────────────────────────────────


class TestTFTHead:
    def test_output_shape_no_cov(self) -> None:
        from src.models.tft import TFTHead

        model = TFTHead(n_past_vars=16, d_model=128)
        x = torch.randn(2, 32, 16)
        out = model(x)
        assert out.shape == (2, 128)

    def test_output_shape_with_cov(self) -> None:
        from src.models.tft import TFTHead

        model = TFTHead(n_past_vars=16, n_cov_vars=8, d_model=128)
        x = torch.randn(2, 32, 16)
        cov = torch.randn(2, 32, 8)
        out = model(x, cov)
        assert out.shape == (2, 128)

    def test_no_nan_output(self) -> None:
        from src.models.tft import TFTHead

        model = TFTHead(n_past_vars=8, d_model=64)
        x = torch.randn(1, 16, 8)
        out = model(x)
        assert not torch.isnan(out).any()

    def test_variable_selection_network(self) -> None:
        from src.models.tft import VariableSelectionNetwork

        vsn = VariableSelectionNetwork(n_vars=8, hidden_dim=32)
        # forward expects [B, T, n_vars, hidden_dim]
        x = torch.randn(2, 16, 8, 32)
        out = vsn(x)
        assert out.shape == (2, 16, 32)

    def test_gated_residual_network(self) -> None:
        from src.models.tft import GatedResidualNetwork

        grn = GatedResidualNetwork(input_dim=32, hidden_dim=32, output_dim=32, dropout=0.0)
        x = torch.randn(2, 16, 32)
        out = grn(x)
        assert out.shape == (2, 16, 32)
