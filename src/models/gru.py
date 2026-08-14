"""
GRU model head — on-chain slow signal with regime conditioning.

2-layer GRU with regime vector injection (concatenated to each time step).
Input:  [B, T, n_features], regime: [B, 64]
Output: [B, 128]
"""

from __future__ import annotations

import torch
import torch.nn as nn


class GRUHead(nn.Module):
    """
    2-layer GRU with regime conditioning.

    Regime vector is projected to hidden_size and added to the initial hidden
    state of both GRU layers, effectively conditioning the GRU's initial
    state on the current regime.
    """

    def __init__(
        self,
        input_size: int = 16,
        hidden_size: int = 128,
        num_layers: int = 2,
        dropout: float = 0.2,
        regime_dim: int = 64,
        d_model: int = 128,
    ) -> None:
        super().__init__()
        self.hidden_size = hidden_size
        self.num_layers = num_layers
        self.gru = nn.GRU(
            input_size=input_size,
            hidden_size=hidden_size,
            num_layers=num_layers,
            batch_first=True,
            dropout=dropout if num_layers > 1 else 0.0,
        )
        self.regime_proj = nn.Linear(regime_dim, hidden_size * num_layers)
        self.attn = nn.Linear(hidden_size, 1)
        self.proj = nn.Sequential(
            nn.Linear(hidden_size, d_model),
            nn.LayerNorm(d_model),
        )

    def forward(self, x: torch.Tensor, regime: torch.Tensor | None = None) -> torch.Tensor:
        """
        x:      [B, T, F]
        regime: [B, regime_dim] — optional; zeros if not provided
        returns: [B, 128]
        """
        B = x.shape[0]
        if regime is not None:
            h0 = self.regime_proj(regime)  # [B, H * num_layers]
            h0 = h0.view(B, self.num_layers, self.hidden_size).transpose(0, 1).contiguous()
        else:
            h0 = torch.zeros(self.num_layers, B, self.hidden_size, device=x.device)

        out, _ = self.gru(x, h0)  # [B, T, H]
        weights = torch.softmax(self.attn(out), dim=1)  # [B, T, 1]
        context = (out * weights).sum(dim=1)  # [B, H]
        return self.proj(context)
