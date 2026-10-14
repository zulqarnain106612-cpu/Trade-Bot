"""
PatchTST model head — multivariate OHLCV with patch-based transformer.

Based on Nie et al. (2023) "A Time Series is Worth 64 Words: Long-term
Forecasting with Transformers."

Segments the input time series into non-overlapping patches (len=16, stride=8)
and applies a 6-layer transformer encoder over the patch sequence.

Input:  [B, C, T] — batch x channels x time (C = OHLCV = 5)
Output: [B, 128]
"""

from __future__ import annotations

import math

import torch
import torch.nn as nn


class PatchEmbedding(nn.Module):
    """Extract patches and project to d_model."""

    def __init__(self, n_channels: int, patch_len: int, d_model: int) -> None:
        super().__init__()
        self.patch_len = patch_len
        self.proj = nn.Linear(patch_len * n_channels, d_model)
        self.norm = nn.LayerNorm(d_model)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        """x: [B, C, T] → [B, n_patches, d_model]"""
        B, C, T = x.shape
        n_patches = T // self.patch_len
        # Reshape to patches
        x = x[:, :, : n_patches * self.patch_len]
        x = x.reshape(B, C, n_patches, self.patch_len)
        x = x.permute(0, 2, 3, 1)  # [B, n_patches, patch_len, C]
        x = x.reshape(B, n_patches, self.patch_len * C)
        return self.norm(self.proj(x))


class PatchTSTHead(nn.Module):
    """
    PatchTST: 6-layer transformer encoder over OHLCV patches.

    patch_len=16, stride=8 (overlapping by half), d_model=64,
    nhead=4, d_ff=256.
    """

    def __init__(
        self,
        n_channels: int = 5,  # OHLCV
        seq_len: int = 96,
        patch_len: int = 16,
        d_model: int = 64,
        nhead: int = 4,
        n_layers: int = 6,
        d_ff: int = 256,
        dropout: float = 0.1,
        out_d_model: int = 128,
    ) -> None:
        super().__init__()
        self.patch_embed = PatchEmbedding(n_channels, patch_len, d_model)
        n_patches = seq_len // patch_len

        encoder_layer = nn.TransformerEncoderLayer(
            d_model=d_model,
            nhead=nhead,
            dim_feedforward=d_ff,
            dropout=dropout,
            batch_first=True,
            norm_first=True,
        )
        self.encoder = nn.TransformerEncoder(encoder_layer, num_layers=n_layers)

        # Positional encoding
        pe = torch.zeros(n_patches, d_model)
        pos = torch.arange(0, n_patches, dtype=torch.float).unsqueeze(1)
        div = torch.exp(torch.arange(0, d_model, 2).float() * (-math.log(10000.0) / d_model))
        pe[:, 0::2] = torch.sin(pos * div)
        pe[:, 1::2] = torch.cos(pos * div)
        self.register_buffer("pe", pe.unsqueeze(0))

        self.pool = nn.AdaptiveAvgPool1d(1)
        self.proj = nn.Linear(d_model, out_d_model)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        """x: [B, C, T] → [B, 128]"""
        patches = self.patch_embed(x)  # [B, n_patches, d_model]
        patches = patches + self.pe  # positional encoding
        encoded = self.encoder(patches)  # [B, n_patches, d_model]
        pooled = self.pool(encoded.transpose(1, 2)).squeeze(-1)  # [B, d_model]
        return self.proj(pooled)
