"""
TCN (Temporal Convolutional Network) model head — trade flow sequence.

Input:  [B, C, T] — batch x features x time
Output: [B, 128]  — 128-dim embedding

Architecture: stack of causal dilated conv residual blocks with exponentially
growing dilation rates (1, 2, 4, 8, ...) and dropout. Final global average
pool → linear projection to d_model.
"""

from __future__ import annotations

import torch
import torch.nn as nn
from torch.nn.utils import weight_norm


class TCNResidualBlock(nn.Module):
    """
    A single TCN residual block: two causal dilated conv layers with
    weight normalisation, dropout, and a residual connection.
    """

    def __init__(
        self,
        n_inputs: int,
        n_outputs: int,
        kernel_size: int,
        stride: int,
        dilation: int,
        dropout: float = 0.2,
    ) -> None:
        super().__init__()
        padding = (kernel_size - 1) * dilation
        self.conv1 = weight_norm(
            nn.Conv1d(
                n_inputs, n_outputs, kernel_size, stride=stride, padding=padding, dilation=dilation
            )
        )
        self.chomp1 = _Chomp1d(padding)
        self.relu1 = nn.ReLU()
        self.dropout1 = nn.Dropout(dropout)

        self.conv2 = weight_norm(
            nn.Conv1d(
                n_outputs, n_outputs, kernel_size, stride=stride, padding=padding, dilation=dilation
            )
        )
        self.chomp2 = _Chomp1d(padding)
        self.relu2 = nn.ReLU()
        self.dropout2 = nn.Dropout(dropout)

        self.net = nn.Sequential(
            self.conv1,
            self.chomp1,
            self.relu1,
            self.dropout1,
            self.conv2,
            self.chomp2,
            self.relu2,
            self.dropout2,
        )
        self.downsample = nn.Conv1d(n_inputs, n_outputs, 1) if n_inputs != n_outputs else None
        self.relu = nn.ReLU()

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        out = self.net(x)
        res = x if self.downsample is None else self.downsample(x)
        return self.relu(out + res)


class _Chomp1d(nn.Module):
    """Remove padding added to the right to maintain causality."""

    def __init__(self, chomp_size: int) -> None:
        super().__init__()
        self.chomp_size = chomp_size

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return x[:, :, : -self.chomp_size].contiguous()


class TCNHead(nn.Module):
    """
    Temporal Convolutional Network for trade flow sequences.

    n_layers residual blocks with exponentially growing dilation (2^i).
    """

    def __init__(
        self,
        in_channels: int = 16,
        hidden_channels: int = 64,
        n_layers: int = 4,
        kernel_size: int = 3,
        dropout: float = 0.2,
        d_model: int = 128,
    ) -> None:
        super().__init__()
        channels = [hidden_channels] * n_layers
        blocks = []
        n_in = in_channels
        for i, n_out in enumerate(channels):
            dilation = 2**i
            blocks.append(
                TCNResidualBlock(
                    n_in, n_out, kernel_size, stride=1, dilation=dilation, dropout=dropout
                )
            )
            n_in = n_out
        self.network = nn.Sequential(*blocks)
        self.pool = nn.AdaptiveAvgPool1d(1)
        self.proj = nn.Sequential(
            nn.Linear(hidden_channels, d_model),
            nn.LayerNorm(d_model),
        )

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        """x: [B, C, T] → [B, 128]"""
        out = self.network(x)
        pooled = self.pool(out).squeeze(-1)
        return self.proj(pooled)
