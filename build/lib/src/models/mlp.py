"""
MLP model head — macro dense features (DXY, gold, SPX, BTC.D, options, etc.).

4-layer residual MLP with LayerNorm and GELU activations.
Input:  [B, n_features]
Output: [B, 128]
"""

from __future__ import annotations

import torch
import torch.nn as nn


class ResidualBlock(nn.Module):
    def __init__(self, dim: int, dropout: float = 0.1) -> None:
        super().__init__()
        self.net = nn.Sequential(
            nn.Linear(dim, dim * 2),
            nn.GELU(),
            nn.Dropout(dropout),
            nn.Linear(dim * 2, dim),
            nn.LayerNorm(dim),
        )

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return x + self.net(x)


class MLPHead(nn.Module):
    """
    4-layer residual MLP for macro/dense features.

    Uses GELU activations throughout and LayerNorm in each residual block.
    """

    def __init__(
        self,
        input_dim: int = 32,
        hidden_dim: int = 256,
        n_layers: int = 4,
        dropout: float = 0.1,
        d_model: int = 128,
    ) -> None:
        super().__init__()
        self.input_proj = nn.Sequential(
            nn.Linear(input_dim, hidden_dim),
            nn.LayerNorm(hidden_dim),
            nn.GELU(),
        )
        self.blocks = nn.Sequential(*[ResidualBlock(hidden_dim, dropout) for _ in range(n_layers)])
        self.output_proj = nn.Linear(hidden_dim, d_model)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        """x: [B, F] → [B, 128]"""
        return self.output_proj(self.blocks(self.input_proj(x)))
