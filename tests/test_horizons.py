"""Tests for the 10-horizon neural model heads and fusion layer."""

from __future__ import annotations

import torch


class TestCNNHead:
    def test_output_shape(self) -> None:
        from src.models.cnn import CNNHead

        model = CNNHead(in_channels=200, d_model=128)
        x = torch.randn(2, 200, 64)
        out = model(x)
        assert out.shape == (2, 128)

    def test_no_nan(self) -> None:
        from src.models.cnn import CNNHead

        model = CNNHead()
        x = torch.randn(1, 200, 32)
        out = model(x)
        assert not torch.isnan(out).any()


class TestTCNHead:
    def test_output_shape(self) -> None:
        from src.models.tcn import TCNHead

        model = TCNHead(in_channels=16, d_model=128)
        x = torch.randn(2, 16, 64)
        out = model(x)
        assert out.shape == (2, 128)


class TestLSTMHead:
    def test_output_shape(self) -> None:
        from src.models.lstm import LSTMHead

        model = LSTMHead(input_size=16)
        x = torch.randn(2, 20, 16)
        out = model(x)
        assert out.shape == (2, 128)

    def test_different_seq_lengths(self) -> None:
        from src.models.lstm import LSTMHead

        model = LSTMHead(input_size=16)
        for seq_len in [1, 10, 50, 100]:
            x = torch.randn(1, seq_len, 16)
            out = model(x)
            assert out.shape == (1, 128)


class TestGRUHead:
    def test_output_shape(self) -> None:
        from src.models.gru import GRUHead

        model = GRUHead(input_size=16, regime_dim=32)
        x = torch.randn(2, 20, 16)
        regime = torch.randn(2, 32)
        out = model(x, regime)
        assert out.shape == (2, 128)

    def test_zero_regime(self) -> None:
        from src.models.gru import GRUHead

        model = GRUHead(input_size=16, regime_dim=32)
        x = torch.randn(1, 10, 16)
        regime = torch.zeros(1, 32)
        out = model(x, regime)
        assert not torch.isnan(out).any()


class TestMLPHead:
    def test_output_shape(self) -> None:
        from src.models.mlp import MLPHead

        model = MLPHead(input_dim=64, d_model=128)
        x = torch.randn(2, 64)
        out = model(x)
        assert out.shape == (2, 128)


class TestNBEATSHead:
    def test_output_shape(self) -> None:
        from src.models.nbeats import NBEATSHead

        model = NBEATSHead(input_size=48, d_model=128)
        x = torch.randn(2, 48)
        out = model(x)
        assert out.shape == (2, 128)


class TestECCHead:
    def test_output_shape(self) -> None:
        from src.models.ecc_head import ECCHead

        model = ECCHead(n_ecc_features=5, d_model=128)
        x = torch.randn(3, 5)
        out = model(x)
        assert out.shape == (3, 128)

    def test_no_nan_zero_input(self) -> None:
        from src.models.ecc_head import ECCHead

        model = ECCHead()
        x = torch.zeros(1, 5)
        out = model(x)
        assert not torch.isnan(out).any()


class TestBERTHead:
    def test_output_shape(self) -> None:
        from src.models.bert_head import BERTHead

        model = BERTHead(bert_dim=768, d_model=128)
        x = torch.randn(2, 768)
        out = model(x)
        assert out.shape == (2, 128)


class TestGNNHead:
    def test_output_shape_without_pyg(self) -> None:
        """A plain [B, F] tensor takes the fallback path and maps to [B, d_model]."""
        from src.models.gnn_head import GNNHead

        model = GNNHead(node_features=32, d_model=128)
        x = torch.randn(5, 32)
        # No PyG installed → forward takes the plain-tensor fallback path.
        out = model(x)
        assert out.shape == (1, 128)


class TestPatchTSTHead:
    def test_output_shape(self) -> None:
        from src.models.patchtst import PatchTSTHead

        model = PatchTSTHead(n_channels=5, patch_len=16, d_model=128)
        # x: [B, C, T] where T divisible by patch_len
        x = torch.randn(2, 5, 96)
        out = model(x)
        assert out.shape == (2, 128)


class TestConformerHead:
    def test_output_shape(self) -> None:
        from src.models.conformer import ConformerHead

        model = ConformerHead(input_dim=32, d_model=128, n_heads=4)
        x = torch.randn(2, 16, 32)
        out = model(x)
        assert out.shape == (2, 128)


class TestCrossAttentionFusion:
    def test_output_shape(self) -> None:
        from src.fusion.cross_attention import CrossAttentionFusion

        model = CrossAttentionFusion(n_heads=12, d_model=128, regime_dim=64)
        embeddings = torch.randn(2, 12, 128)
        regime = torch.randn(2, 64)
        fused, weights = model(embeddings, regime)
        assert fused.shape == (2, 128)
        assert weights.shape[0] == 2

    def test_ecc_boost_applied(self) -> None:
        from src.fusion.cross_attention import CrossAttentionFusion

        model = CrossAttentionFusion(n_heads=12, d_model=128, regime_dim=64)
        emb = torch.randn(1, 12, 128)
        regime = torch.randn(1, 64)
        # Should not raise
        fused_no_ecc, _ = model(emb, regime, ecc_anomaly=0.0)
        fused_ecc, _ = model(emb, regime, ecc_anomaly=1.0)
        # ECC boost affects the last embedding slot — outputs differ
        assert not torch.allclose(fused_no_ecc, fused_ecc)


class TestMetaNetwork:
    def test_output_count(self) -> None:
        from src.fusion.meta_network import MetaNetwork

        model = MetaNetwork(n_horizons=10, d_in=128)
        x = torch.randn(2, 128)
        outputs = model(x)
        assert len(outputs) == 10

    def test_direction_softmax(self) -> None:
        from src.fusion.meta_network import MetaNetwork

        model = MetaNetwork(n_horizons=10, d_in=128)
        batch = 1
        x = torch.randn(batch, 128)
        outputs = model(x)
        for out in outputs:
            # direction is [B, 3] softmax probabilities — each row sums to 1
            row_sums = out.direction.sum(dim=-1)
            assert torch.allclose(row_sums, torch.ones_like(row_sums), atol=1e-5)

    def test_timing_in_01(self) -> None:
        from src.fusion.meta_network import MetaNetwork

        model = MetaNetwork(n_horizons=10, d_in=128)
        x = torch.randn(1, 128)
        outputs = model(x)
        for out in outputs:
            assert 0.0 <= float(out.timing) <= 1.0

    def test_meta_network_loss(self) -> None:
        from src.fusion.meta_network import MetaNetwork, MetaNetworkLoss

        model = MetaNetwork(n_horizons=3, d_in=64)
        loss_fn = MetaNetworkLoss()
        x = torch.randn(2, 64)
        outputs = model(x)
        targets = [
            {
                "direction_label": torch.zeros(2, dtype=torch.long),
                "magnitude_y": torch.randn(2),
                "timing_label": torch.zeros(2, dtype=torch.long),
            }
            for _ in range(3)
        ]
        loss = loss_fn(outputs, targets)
        assert loss.item() > 0.0
        assert not torch.isnan(loss)
