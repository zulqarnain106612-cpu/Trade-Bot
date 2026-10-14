"""
LSTM model head — derivatives slow signal.

2-layer LSTM with attention gate.
Input:  [B, T, n_features]
Output: [B, 128]
"""

from __future__ import annotations

import torch
import torch.nn as nn


class LSTMHead(nn.Module):
    """
    2-layer bidirectional LSTM with a soft attention gate over the output sequence.

    hidden_size=256 as specified; attention projects the full sequence to a
    single context vector then passes through a projection layer.
    """

    def __init__(
        self,
        input_size: int = 16,
        hidden_size: int = 256,
        num_layers: int = 2,
        dropout: float = 0.2,
        d_model: int = 128,
    ) -> None:
        super().__init__()
        self.lstm = nn.LSTM(
            input_size=input_size,
            hidden_size=hidden_size,
            num_layers=num_layers,
            batch_first=True,
            dropout=dropout if num_layers > 1 else 0.0,
            bidirectional=False,
        )
        self.attn = nn.Linear(hidden_size, 1)
        self.proj = nn.Sequential(
            nn.Linear(hidden_size, d_model),
            nn.LayerNorm(d_model),
        )

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        """x: [B, T, F] → [B, 128]"""
        out, _ = self.lstm(x)  # [B, T, H]
        weights = torch.softmax(self.attn(out), dim=1)  # [B, T, 1]
        context = (out * weights).sum(dim=1)  # [B, H]
        return self.proj(context)
