"""
Meta-Network — 10 output heads (one per horizon).

Each head produces:
  direction_logits: [B, 3] — raw long/flat/short scores, what CE loss consumes
  direction:        [B, 3] — softmax of the above, for inference/reporting
  magnitude:        [B, 2] — μ and log_s (log STANDARD DEVIATION) for Gaussian
                             NLL over the price move. MetaNetworkLoss converts
                             it with exp(2*log_s); a head emitting a
                             log-variance would make every uncertainty this
                             model reports wrong by a square.
  timing:           [B, 1] — entry delay probability in [0, 1] (BCE loss)

Loss (per horizon):
  L = CE(direction_logits) + GaussianNLL(μ, exp(2*log_s), y_move) + BCE(timing)

The logits/probabilities split is not redundancy. nn.CrossEntropyLoss applies
its own log-softmax, so feeding it the softmaxed head squashes the input
range, flattens the gradient and trains a materially different objective than
the documented one — while raising nothing and producing a loss curve that
descends convincingly. Keeping both means the loss reads logits and every
consumer that wants probabilities still gets them.

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
    magnitude: torch.Tensor  # [B, 2] — (μ, log_s) of price move; log_s is a
    # log STANDARD DEVIATION, not a log variance — see MetaNetworkLoss.
    timing: torch.Tensor  # [B, 1] — entry delay probability
    # Pre-softmax scores. Defaults to None only so existing callers that
    # construct HorizonOutput by hand keep working; MetaNetwork always sets
    # it, and MetaNetworkLoss requires it rather than silently falling back
    # to the probabilities, which is the bug this field exists to close.
    direction_logits: torch.Tensor | None = None


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
        outputs: list[HorizonOutput] = []
        for i in range(self.n_horizons):
            logits = self.direction_heads[i](h)
            outputs.append(
                HorizonOutput(
                    direction=torch.softmax(logits, dim=-1),
                    magnitude=self.magnitude_heads[i](h),
                    timing=torch.sigmoid(self.timing_heads[i](h)),
                    direction_logits=logits,
                )
            )
        return outputs


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
        # A caller with fewer targets than horizons was previously truncated
        # silently by zip(): the tail horizons trained on nothing and the run
        # looked healthy. Sparse supervision is expressed by passing None for
        # a horizon, which is handled below — a length mismatch is a bug.
        if len(targets) != len(outputs):
            raise ValueError(
                f"expected one target per horizon, got {len(targets)} for {len(outputs)} horizons"
            )

        # Seeded from an output so the accumulator inherits the model's device
        # and dtype. A bare torch.tensor(0.0) is CPU-float32 and would raise
        # the moment this trains anywhere else.
        total_loss = outputs[0].direction.sum() * 0.0
        n_active = 0
        for out, tgt in zip(outputs, targets, strict=True):
            if tgt is None:
                continue
            dir_label = tgt["direction_label"]
            mag_y = tgt["magnitude_y"]
            # BCE needs a floating-point target, but pinning it to float32
            # breaks the moment the model runs in any other dtype: a .double()
            # network emits a float64 probability and BCE refuses the pair.
            timing_label = tgt["timing_label"].to(out.timing.dtype)

            if out.direction_logits is None:
                # Falling back to out.direction here is what the original code
                # did implicitly, and it is precisely the double-softmax this
                # field was added to prevent. Refuse rather than train the
                # wrong objective silently.
                raise ValueError(
                    "HorizonOutput.direction_logits is required for the CE term; "
                    "pass pre-softmax scores, not probabilities"
                )
            l_dir = self.ce(out.direction_logits, dir_label)

            mu = out.magnitude[:, 0]
            # The head emits log_s — log standard deviation — as documented on
            # MetaNetwork and HorizonOutput. GaussianNLLLoss takes a variance,
            # so it is exp(2*log_s), not exp(log_s). Passing the latter used
            # the standard deviation as if it were a variance, which never
            # raises and never diverges: the model simply learns a mis-scaled
            # uncertainty, and every downstream confidence derived from it is
            # wrong by a square. Clamped in log space so the exponential
            # cannot overflow before the floor is applied.
            log_s = out.magnitude[:, 1].clamp(min=-10.0, max=10.0)
            var = torch.exp(2.0 * log_s).clamp(min=1e-6)
            l_mag = self.gaussian_nll(mu, mag_y, var)

            l_timing = F.binary_cross_entropy(out.timing.squeeze(-1), timing_label)
            total_loss = total_loss + l_dir + l_mag + l_timing
            n_active += 1

        # All-None targets means nothing was supervised. Returning 0.0 would
        # make optimizer.step() a silent no-op that looks like a completed
        # training step, so say so instead.
        if n_active == 0:
            raise ValueError("every horizon target was None — nothing to train on")

        return total_loss / n_active
