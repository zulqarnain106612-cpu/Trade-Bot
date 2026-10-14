"""
ECC head — ECC feature vector → 128-dim embedding.

Takes the 5-element ECC feature vector produced by the ECC pipeline
(cluster_flow, ecdsa_weakness, schnorr_divergence, hodler_index,
dark_pool_pressure) and any supplementary on-chain ECC features, then
projects them to a 128-dim embedding via a 2-layer MLP with LayerNorm.

This embedding is the 12th (index 11) head in the cross-attention fusion
and receives the learnable `ecc_boost` scalar when ECC anomaly is detected.

Input:  [B, n_ecc_features] — default 5 (one per ECC module)
Output: [B, 128]
"""

from __future__ import annotations

import torch
import torch.nn as nn


class ECCHead(nn.Module):
    """
    2-layer MLP for ECC feature vector → 128-dim embedding.

    Architecture: Linear(n_in, 64) → GELU → LayerNorm → Linear(64, 128) → LayerNorm
    """

    def __init__(self, n_ecc_features: int = 5, d_model: int = 128) -> None:
        super().__init__()
        self.net = nn.Sequential(
            nn.Linear(n_ecc_features, 64),
            nn.GELU(),
            nn.LayerNorm(64),
            nn.Linear(64, d_model),
            nn.LayerNorm(d_model),
        )

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        """x: [B, n_ecc_features] → [B, 128]"""
        return self.net(x)


def build_ecc_feature_tensor(
    cluster_flow_score: float,
    ecdsa_weakness: float,
    schnorr_divergence: float,
    hodler_index: float,
    dark_pool_pressure: float,
    device: torch.device | str = "cpu",
) -> torch.Tensor:
    """Pack scalar ECC outputs into a [1, 5] tensor for the ECC head."""
    vec = [cluster_flow_score, ecdsa_weakness, schnorr_divergence, hodler_index, dark_pool_pressure]
    return torch.tensor([vec], dtype=torch.float32, device=device)
