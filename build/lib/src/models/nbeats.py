"""
N-BEATS model head — univariate OHLCV decomposition.

Based on Oreshkin et al. (2020) "N-BEATS: Neural basis expansion analysis
for interpretable time series forecasting."

Decomposes the input time series into trend, seasonality, and residual
stacks. The final embedding is the concatenation of the basis expansions.

Input:  [B, T] — univariate time series (e.g. close prices)
Output: [B, 128]
"""

from __future__ import annotations

import torch
import torch.nn as nn


class NBEATSBlock(nn.Module):
    """Single N-BEATS block with fully-connected expansion and basis functions."""

    def __init__(
        self,
        input_size: int,
        theta_dim: int,
        hidden_dim: int = 256,
        n_layers: int = 4,
    ) -> None:
        super().__init__()
        layers = [nn.Linear(input_size, hidden_dim), nn.ReLU()]
        for _ in range(n_layers - 2):
            layers += [nn.Linear(hidden_dim, hidden_dim), nn.ReLU()]
        layers += [nn.Linear(hidden_dim, theta_dim)]
        self.fc = nn.Sequential(*layers)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        """x: [B, T] → theta [B, theta_dim]"""
        return self.fc(x)


class TrendBasis(nn.Module):
    """Polynomial trend basis: θ^T * T^p for p = 0, ..., degree."""

    def __init__(self, degree: int, backcast_length: int) -> None:
        super().__init__()
        self.degree = degree
        t = torch.linspace(0, 1, backcast_length)
        T = torch.stack([t**i for i in range(degree + 1)], dim=0)  # [degree+1, T]
        self.register_buffer("T", T)

    def forward(self, theta: torch.Tensor) -> torch.Tensor:
        """theta: [B, degree+1] → [B, T]"""
        return torch.einsum("bd,dt->bt", theta, self.T)  # type: ignore[arg-type]


class SeasonalityBasis(nn.Module):
    """Fourier seasonality basis."""

    def __init__(self, n_harmonics: int, backcast_length: int) -> None:
        super().__init__()
        t = torch.linspace(0, 1, backcast_length)
        freqs = torch.arange(1, n_harmonics + 1, dtype=torch.float32)
        cos_terms = torch.cos(2 * torch.pi * freqs.unsqueeze(1) * t.unsqueeze(0))
        sin_terms = torch.sin(2 * torch.pi * freqs.unsqueeze(1) * t.unsqueeze(0))
        basis = torch.cat([cos_terms, sin_terms], dim=0)  # [2*n_harmonics, T]
        self.register_buffer("basis", basis)

    def forward(self, theta: torch.Tensor) -> torch.Tensor:
        """theta: [B, 2*n_harmonics] → [B, T]"""
        return torch.einsum("bd,dt->bt", theta, self.basis)  # type: ignore[arg-type]


class NBEATSHead(nn.Module):
    """
    N-BEATS ensemble: trend + seasonality + residual stacks.
    Each stack produces a backcast and forecast; residuals are passed
    to the next stack.  Final output is a pooled embedding.
    """

    def __init__(
        self,
        input_size: int = 96,
        n_trend_degree: int = 3,
        n_seasonality_harmonics: int = 8,
        hidden_dim: int = 256,
        n_blocks_per_stack: int = 3,
        d_model: int = 128,
    ) -> None:
        super().__init__()
        trend_theta = n_trend_degree + 1
        season_theta = 2 * n_seasonality_harmonics

        # Trend stack
        self.trend_blocks = nn.ModuleList(
            [NBEATSBlock(input_size, trend_theta, hidden_dim) for _ in range(n_blocks_per_stack)]
        )
        self.trend_basis = TrendBasis(n_trend_degree, input_size)

        # Seasonality stack
        self.season_blocks = nn.ModuleList(
            [NBEATSBlock(input_size, season_theta, hidden_dim) for _ in range(n_blocks_per_stack)]
        )
        self.season_basis = SeasonalityBasis(n_seasonality_harmonics, input_size)

        # Residual stack → generic MLP block
        self.residual_blocks = nn.ModuleList(
            [NBEATSBlock(input_size, input_size, hidden_dim) for _ in range(n_blocks_per_stack)]
        )

        # Final projection from [trend+season+residual decompositions] → 128
        # We concatenate the 3 theta outputs and project
        embed_dim = trend_theta + season_theta + input_size
        self.proj = nn.Sequential(
            nn.Linear(embed_dim, d_model),
            nn.LayerNorm(d_model),
        )

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        """x: [B, T] univariate series → [B, 128]"""
        residual = x
        trend_thetas = []
        for block in self.trend_blocks:
            theta = block(residual)
            trend_thetas.append(theta)
            backcast = self.trend_basis(theta)
            residual = residual - backcast
        trend_emb = torch.stack(trend_thetas, dim=1).mean(dim=1)

        season_thetas = []
        for block in self.season_blocks:
            theta = block(residual)
            season_thetas.append(theta)
            backcast = self.season_basis(theta)
            residual = residual - backcast
        season_emb = torch.stack(season_thetas, dim=1).mean(dim=1)

        res_thetas = []
        for block in self.residual_blocks:
            theta = block(residual)
            res_thetas.append(theta)
            residual = residual - theta
        res_emb = torch.stack(res_thetas, dim=1).mean(dim=1)

        combined = torch.cat([trend_emb, season_emb, res_emb], dim=-1)
        return self.proj(combined)
