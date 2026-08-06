"""
Meta-Network — 10 output heads (one per horizon).

Each head produces:
  direction:  [B, 3]   — logits for long/flat/short (CE loss)
  magnitude:  [B, 2]   — μ and log_s for Gaussian NLL (price move size)
  timing:     [B, 1]   — entry delay probability in [0, 1] (CE loss)

Loss (per horizon):
  L = CE(direction) + GaussianNLL(magnitude_μ, magnitude_log_s, y_move) + CE(timing)

Optimizer: AdamW(lr=3e-4, weight_decay=1e-2), gradient clip = 1.0
"""

from __future__ import annotations

from dataclasses import dataclass

import torch
import torch.nn as nn
import torch.nn.functional as F


@dataclass
class HorizonOutput:
    direction: torch.Tensor  # [B, 3] — softmax probabilities (long/flat/short)
    magnitude: torch.Tensor  # [B, 2] — (μ, log_s) of price move
    timing: torch.Tensor  # [B, 1] — entry delay probability


class MetaNetwork(nn.Module):
    """
    Multi-task meta-network with 10 parallel output heads (one per horizon).

    Shared backbone: Linear(128, 256) → GELU → LayerNorm(256)
    Per-horizon heads: direction(3), magnitude(2), timing(1)
    """

    def __init__(self, n_horizons: int = 10, d_in: int = 128) -> None:
        super().__init__()
        self.n_horizons = n_horizons
        self.shared = nn.Sequential(
            nn.Linear(d_in, 256),
            nn.GELU(),
            nn.LayerNorm(256),
        )
        self.direction_heads = nn.ModuleList([nn.Linear(256, 3) for _ in range(n_horizons)])
        self.magnitude_heads = nn.ModuleList([nn.Linear(256, 2) for _ in range(n_horizons)])
        self.timing_heads = nn.ModuleList([nn.Linear(256, 1) for _ in range(n_horizons)])

    def forward(self, x: torch.Tensor) -> list[HorizonOutput]:
        """
        x: [B, 128] fused embedding from CrossAttentionFusion
        returns: list of 10 HorizonOutput objects
        """
        h = self.shared(x)
        return [
            HorizonOutput(
                direction=torch.softmax(self.direction_heads[i](h), dim=-1),
                magnitude=self.magnitude_heads[i](h),
                timing=torch.sigmoid(self.timing_heads[i](h)),
            )
            for i in range(self.n_horizons)
        ]


class MetaNetworkLoss(nn.Module):
    """
    Joint loss: CE(direction) + GaussianNLL(magnitude) + CE(timing).

    Applied independently per horizon; averaged over all active horizons.
    """

    def __init__(self) -> None:
        super().__init__()
        self.ce = nn.CrossEntropyLoss()
        self.gaussian_nll = nn.GaussianNLLLoss()

    def forward(
        self,
        outputs: list[HorizonOutput],
        targets: list[dict],
    ) -> torch.Tensor:
        """
        outputs: list of HorizonOutput (one per horizon)
        targets: list of dicts with keys:
          direction_label: [B] long(0)/flat(1)/short(2)
          magnitude_y:     [B] true price move
          timing_label:    [B] binary entry delay
        """
        total_loss = torch.tensor(0.0, requires_grad=True)
        n_active = 0
        for out, tgt in zip(outputs, targets, strict=False):
            if tgt is None:
                continue
            dir_label = tgt["direction_label"]
            mag_y = tgt["magnitude_y"]
            timing_label = tgt["timing_label"].float()

            l_dir = self.ce(out.direction, dir_label)

            mu = out.magnitude[:, 0]
            log_var = out.magnitude[:, 1]
            var = torch.exp(log_var).clamp(min=1e-6)
            l_mag = self.gaussian_nll(mu, mag_y, var)

            l_timing = F.binary_cross_entropy(out.timing.squeeze(-1), timing_label)
            total_loss = total_loss + l_dir + l_mag + l_timing
            n_active += 1

        return total_loss / max(n_active, 1)
