"""
Conformer model head — HFT microstructure signal.

Combines convolutional modules (local pattern extraction) with
relative-position multi-head self-attention (global context), following
Gulati et al. (2020) "Conformer: Convolution-augmented Transformer for
Speech Recognition."

4 Conformer blocks with feed-forward, attention, convolution, feed-forward
sandwich structure. Adapted for financial microstructure data.

Input:  [B, T, d_model] — batch x time x features
Output: [B, 128]
"""

from __future__ import annotations

import torch
import torch.nn as nn


class ConvModule(nn.Module):
    """Point-wise conv → gating → depth-wise causal conv → BN → Swish → point-wise conv."""

    def __init__(self, d_model: int, kernel_size: int = 31, dropout: float = 0.1) -> None:
        super().__init__()
        self.ln = nn.LayerNorm(d_model)
        self.pw1 = nn.Conv1d(d_model, 2 * d_model, kernel_size=1)
        self.glu = nn.GLU(dim=1)
        padding = (kernel_size - 1) // 2
        self.dw = nn.Conv1d(
            d_model, d_model, kernel_size=kernel_size, padding=padding, groups=d_model
        )
        self.bn = nn.BatchNorm1d(d_model)
        self.swish = nn.SiLU()
        self.pw2 = nn.Conv1d(d_model, d_model, kernel_size=1)
        self.drop = nn.Dropout(dropout)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        """x: [B, T, d_model]"""
        residual = x
        x = self.ln(x).transpose(1, 2)  # [B, d_model, T]
        x = self.glu(self.pw1(x))  # [B, d_model, T] (GLU halves channels)
        x = self.swish(self.bn(self.dw(x)))
        x = self.drop(self.pw2(x)).transpose(1, 2)  # [B, T, d_model]
        return x + residual


class ConformerBlock(nn.Module):
    """Conformer block: FF → MHA → Conv → FF sandwich."""

    def __init__(self, d_model: int, n_heads: int, d_ff: int, dropout: float = 0.1) -> None:
        super().__init__()
        self.ff1 = nn.Sequential(
            nn.LayerNorm(d_model),
            nn.Linear(d_model, d_ff),
            nn.SiLU(),
            nn.Dropout(dropout),
            nn.Linear(d_ff, d_model),
            nn.Dropout(dropout),
        )
        self.attn_ln = nn.LayerNorm(d_model)
        self.attn = nn.MultiheadAttention(
            d_model, num_heads=n_heads, dropout=dropout, batch_first=True
        )
        self.conv_module = ConvModule(d_model, dropout=dropout)
        self.ff2 = nn.Sequential(
            nn.LayerNorm(d_model),
            nn.Linear(d_model, d_ff),
            nn.SiLU(),
            nn.Dropout(dropout),
            nn.Linear(d_ff, d_model),
            nn.Dropout(dropout),
        )
        self.ln_out = nn.LayerNorm(d_model)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        x = x + 0.5 * self.ff1(x)
        attn_in = self.attn_ln(x)
        attn_out, _ = self.attn(attn_in, attn_in, attn_in)
        x = x + attn_out
        x = self.conv_module(x)
        x = x + 0.5 * self.ff2(x)
        return self.ln_out(x)


class ConformerHead(nn.Module):
    """4-block Conformer encoder for HFT microstructure sequences."""

    def __init__(
        self,
        input_dim: int = 16,
        d_model: int = 64,
        n_heads: int = 4,
        d_ff: int = 256,
        n_blocks: int = 4,
        dropout: float = 0.1,
        out_d_model: int = 128,
    ) -> None:
        super().__init__()
        self.input_proj = nn.Linear(input_dim, d_model)
        self.blocks = nn.ModuleList(
            [ConformerBlock(d_model, n_heads, d_ff, dropout) for _ in range(n_blocks)]
        )
        self.pool = nn.AdaptiveAvgPool1d(1)
        self.proj = nn.Sequential(
            nn.Linear(d_model, out_d_model),
            nn.LayerNorm(out_d_model),
        )

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        """x: [B, T, F] → [B, 128]"""
        h = self.input_proj(x)
        for block in self.blocks:
            h = block(h)
        pooled = self.pool(h.transpose(1, 2)).squeeze(-1)
        return self.proj(pooled)
