"""
CNN model head — 1D dilated convolutional network for L2 order book data.

Input:  [B, C, T] — batch x channels x time  (C = order book levels x 2)
Output: [B, 128]  — 128-dim embedding for cross-attention fusion

Architecture: 3 parallel dilated-conv branches (kernels 3, 5, 7) with
batch normalisation and ReLU, concatenated and projected to 128-dim.
"""

from __future__ import annotations

import torch
import torch.nn as nn


class DilatedConvBlock(nn.Module):
    def __init__(self, in_channels: int, out_channels: int, kernel_size: int) -> None:
        super().__init__()
        padding = (kernel_size - 1) // 2
        self.conv = nn.Conv1d(
            in_channels,
            out_channels,
            kernel_size=kernel_size,
            padding=padding,
            dilation=1,
        )
        self.bn = nn.BatchNorm1d(out_channels)
        self.relu = nn.ReLU(inplace=True)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return self.relu(self.bn(self.conv(x)))


class CNNHead(nn.Module):
    """
    1D dilated CNN for L2 order book (100 price levels x 2 sides = 200 channels).

    Three parallel branches with kernels [3, 5, 7], each producing 64 channels,
    concatenated (192) and projected down to `d_model` dimensions.
    """

    def __init__(self, in_channels: int = 200, d_model: int = 128) -> None:
        super().__init__()
        branch_channels = 64
        self.branch3 = DilatedConvBlock(in_channels, branch_channels, kernel_size=3)
        self.branch5 = DilatedConvBlock(in_channels, branch_channels, kernel_size=5)
        self.branch7 = DilatedConvBlock(in_channels, branch_channels, kernel_size=7)
        concat_dim = branch_channels * 3
        self.pool = nn.AdaptiveAvgPool1d(1)
        self.proj = nn.Sequential(
            nn.Linear(concat_dim, d_model),
            nn.LayerNorm(d_model),
            nn.ReLU(inplace=True),
        )

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        """
        x: [B, C, T]  — order book channels x time steps
        returns: [B, 128]
        """
        b3 = self.pool(self.branch3(x)).squeeze(-1)
        b5 = self.pool(self.branch5(x)).squeeze(-1)
        b7 = self.pool(self.branch7(x)).squeeze(-1)
        cat = torch.cat([b3, b5, b7], dim=-1)
        return self.proj(cat)
