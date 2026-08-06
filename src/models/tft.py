"""
TFT (Temporal Fusion Transformer) model head — OHLCV + covariates.

Adapted from Lim et al. (2021) "Temporal Fusion Transformers for Interpretable
Multi-horizon Time Series Forecasting."

Input:  past OHLCV [B, T, n_past], future covariates [B, T_f, n_cov]
Output: [B, 128] — 128-dim embedding for cross-attention fusion

Key components:
  - Variable Selection Networks (VSN) for static and temporal inputs
  - LSTM encoder over selected features
  - Multi-head Self-Attention with gating
  - Feed-forward output projection
"""

from __future__ import annotations

import torch
import torch.nn as nn


class GatedResidualNetwork(nn.Module):
    """GRN: linear gate applied to residual connection (TFT building block)."""

    def __init__(
        self, input_dim: int, hidden_dim: int, output_dim: int, dropout: float = 0.1
    ) -> None:
        super().__init__()
        self.fc1 = nn.Linear(input_dim, hidden_dim)
        self.fc2 = nn.Linear(hidden_dim, output_dim)
        self.gate = nn.Linear(hidden_dim, output_dim)
        self.skip = nn.Linear(input_dim, output_dim) if input_dim != output_dim else nn.Identity()
        self.ln = nn.LayerNorm(output_dim)
        self.dropout = nn.Dropout(dropout)
        self.elu = nn.ELU()

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        h = self.elu(self.fc1(x))
        h = self.dropout(h)
        out = self.fc2(h) * torch.sigmoid(self.gate(h))
        return self.ln(out + self.skip(x))


class VariableSelectionNetwork(nn.Module):
    """Soft variable selection with per-variable GRNs and a selection GRN."""

    def __init__(self, n_vars: int, hidden_dim: int, dropout: float = 0.1) -> None:
        super().__init__()
        self.var_grns = nn.ModuleList(
            [
                GatedResidualNetwork(hidden_dim, hidden_dim, hidden_dim, dropout)
                for _ in range(n_vars)
            ]
        )
        self.select_grn = GatedResidualNetwork(n_vars * hidden_dim, hidden_dim, n_vars, dropout)
        self.softmax = nn.Softmax(dim=-1)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        """x: [B, T, n_vars, hidden_dim] → [B, T, hidden_dim]"""
        B, T, V, H = x.shape
        processed = torch.stack([self.var_grns[i](x[:, :, i, :]) for i in range(V)], dim=2)
        flat = x.reshape(B, T, V * H)
        weights = self.softmax(self.select_grn(flat))
        combined = (processed * weights.unsqueeze(-1)).sum(dim=2)
        return combined


class TFTHead(nn.Module):
    """
    Temporal Fusion Transformer encoder for OHLCV + covariates.

    Simplified to produce a single 128-dim embedding (no multi-horizon decoding
    needed here since the meta-network handles multiple output heads).
    """

    def __init__(
        self,
        n_past_vars: int = 5,  # OHLCV
        n_cov_vars: int = 8,  # covariates (macro, derivatives, etc.)
        hidden_dim: int = 64,
        lstm_layers: int = 2,
        n_heads: int = 4,
        dropout: float = 0.1,
        d_model: int = 128,
    ) -> None:
        super().__init__()
        self.embed_past = nn.Linear(n_past_vars, hidden_dim)
        self.embed_cov = nn.Linear(n_cov_vars, hidden_dim)
        self.vsn = VariableSelectionNetwork(n_vars=2, hidden_dim=hidden_dim, dropout=dropout)
        self.lstm = nn.LSTM(
            hidden_dim, hidden_dim, num_layers=lstm_layers, batch_first=True, dropout=dropout
        )
        self.attn = nn.MultiheadAttention(
            hidden_dim, num_heads=n_heads, dropout=dropout, batch_first=True
        )
        self.attn_gate = nn.Linear(hidden_dim, hidden_dim)
        self.ln = nn.LayerNorm(hidden_dim)
        self.ffn = nn.Sequential(
            nn.Linear(hidden_dim, hidden_dim * 2), nn.ReLU(), nn.Linear(hidden_dim * 2, hidden_dim)
        )
        self.pool = nn.AdaptiveAvgPool1d(1)
        self.proj = nn.Linear(hidden_dim, d_model)

    def forward(self, past: torch.Tensor, cov: torch.Tensor | None = None) -> torch.Tensor:
        """
        past: [B, T, n_past_vars]
        cov:  [B, T, n_cov_vars] — optional, zeros if not available
        returns: [B, 128]
        """
        B, T, _ = past.shape
        if cov is None:
            cov = torch.zeros(B, T, self.embed_cov.in_features, device=past.device)

        ep = self.embed_past(past)  # [B, T, H]
        ec = self.embed_cov(cov)  # [B, T, H]
        stacked = torch.stack([ep, ec], dim=2)  # [B, T, 2, H]
        selected = self.vsn(stacked)  # [B, T, H]

        lstm_out, _ = self.lstm(selected)  # [B, T, H]
        attn_out, _ = self.attn(lstm_out, lstm_out, lstm_out)
        gated = lstm_out + torch.sigmoid(self.attn_gate(attn_out)) * attn_out
        out = self.ln(gated + self.ffn(gated))  # [B, T, H]

        pooled = self.pool(out.transpose(1, 2)).squeeze(-1)  # [B, H]
        return self.proj(pooled)
