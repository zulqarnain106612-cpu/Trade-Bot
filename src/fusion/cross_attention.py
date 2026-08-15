"""
Cross-Attention Fusion — 12 model heads → 1 fused embedding.

Implements the CrossAttentionFusion module from crypto-intel-v6 spec.

The regime vector acts as a query to attend over the 12 model embeddings.
A sparse gating layer further selects which heads are active. The ECC head
(index 11) receives an additional learnable boost when ECC anomaly is high.

Input:
  embeddings: [B, 12, 128] — one 128-dim embedding per model head
  regime:     [B, 64]      — regime vector from HMM/regime detector
  ecc_anomaly: float       — scalar ECC anomaly score [0, 1]

Output:
  fused: [B, 128]          — fused embedding ready for meta-network
  attn_weights: [B, 1, 12] — attention weights for interpretability
"""

from __future__ import annotations

import torch
import torch.nn as nn


class CrossAttentionFusion(nn.Module):
    """
    12-head cross-attention fusion with regime query and ECC anomaly boost.

    Directly implements the architecture from crypto-intel-v6 spec:
      query   = Linear(regime, d_model)
      MHA     = MultiheadAttention(d_model, num_heads=8)
      gates   = sigmoid(Linear(d_model, n_heads))  ← sparse gating
      ecc_boost = learnable scalar
    """

    def __init__(self, n_heads: int = 12, d_model: int = 128, regime_dim: int = 64) -> None:
        super().__init__()
        self.n_heads = n_heads
        self.d_model = d_model
        self.regime_query = nn.Linear(regime_dim, d_model)
        self.mha = nn.MultiheadAttention(d_model, num_heads=8, dropout=0.1, batch_first=True)
        self.gate = nn.Linear(d_model, n_heads)
        self.ecc_boost = nn.Parameter(torch.ones(1))
        self._ecc_head_idx = n_heads - 1  # ECC head is always last

    def forward(
        self,
        embeddings: torch.Tensor,
        regime: torch.Tensor,
        ecc_anomaly: float = 0.0,
    ) -> tuple[torch.Tensor, torch.Tensor]:
        """
        embeddings: [B, 12, 128]
        regime:     [B, 64]
        ecc_anomaly: scalar float [0, 1]

        returns: (fused [B, 128], attn_weights [B, 1, 12])
        """
        query = self.regime_query(regime).unsqueeze(1)  # [B, 1, 128]
        attn_out, attn_weights = self.mha(query, embeddings, embeddings)  # [B, 1, 128]

        gates = torch.sigmoid(self.gate(attn_out))  # [B, 1, 12]
        gated = embeddings * gates.transpose(1, 2)  # [B, 12, 128]

        # Apply learnable ECC anomaly boost to ECC head embedding
        if ecc_anomaly > 0.0:
            gated[:, self._ecc_head_idx, :] = gated[:, self._ecc_head_idx, :] * (
                1.0 + self.ecc_boost * ecc_anomaly
            )

        fused = gated.sum(dim=1)  # [B, 128]
        return fused, attn_weights
