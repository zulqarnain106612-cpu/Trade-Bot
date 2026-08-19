"""
MAML fast-adaptation for crypto-intel-v6 horizon models (h8, h10).

Model-Agnostic Meta-Learning (Finn et al. 2017) lets the optimizer learn an
initialization point that can adapt to a new market regime in K gradient steps
from a handful of examples — critical for crypto's rapid structural breaks.

This module implements:
  - MAMLOptimizer: wraps any nn.Module with inner-loop gradient tracking
  - HorizonMAMLAdapter: applies MAML to h8 and h10 head checkpoints
  - fast_adapt(): K-step inner-loop update returning adapted parameters
"""

from __future__ import annotations

import copy
from pathlib import Path

import structlog
import torch
import torch.nn as nn


log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

_DEFAULT_LR_INNER = 0.01
_DEFAULT_LR_OUTER = 0.001
_DEFAULT_K_STEPS = 5
_TARGET_HORIZONS = (7, 9)  # h8 and h10 (0-indexed)


def fast_adapt(
    model: nn.Module,
    support_x: torch.Tensor,
    support_y: torch.Tensor,
    loss_fn: nn.Module,
    k_steps: int = _DEFAULT_K_STEPS,
    lr_inner: float = _DEFAULT_LR_INNER,
) -> dict[str, torch.Tensor]:
    """
    Perform K gradient steps on support set and return adapted parameters.

    Does NOT update the original model. Returns a dict of cloned adapted params.
    """
    adapted = {name: param.clone() for name, param in model.named_parameters()}

    for _ in range(k_steps):
        # Forward with adapted params via functional_call
        try:
            from torch.func import functional_call  # type: ignore[import]
        except ImportError:
            from torch._functorch.eager_transforms import (
                functional_call,  # type: ignore[import, attr-defined, no-redef]
            )

        pred = functional_call(model, adapted, (support_x,))  # type: ignore[arg-type]
        loss = loss_fn(pred, support_y)

        grads = torch.autograd.grad(
            loss, list(adapted.values()), create_graph=False, allow_unused=True
        )

        adapted = {
            name: param - lr_inner * (grad if grad is not None else torch.zeros_like(param))
            for (name, param), grad in zip(adapted.items(), grads, strict=False)
        }

    return adapted


class MAMLOptimizer:
    """
    Outer-loop MAML optimizer for a horizon model head.

    Meta-trains the model's initialization point so it can adapt
    to new regime data in K gradient steps.
    """

    def __init__(
        self,
        model: nn.Module,
        loss_fn: nn.Module | None = None,
        lr_inner: float = _DEFAULT_LR_INNER,
        lr_outer: float = _DEFAULT_LR_OUTER,
        k_steps: int = _DEFAULT_K_STEPS,
    ) -> None:
        self._model = model
        self._loss_fn = loss_fn or nn.CrossEntropyLoss()
        self._lr_inner = lr_inner
        self._k_steps = k_steps
        self._outer_opt = torch.optim.Adam(model.parameters(), lr=lr_outer)

    def meta_update(
        self,
        tasks: list[dict[str, torch.Tensor]],
    ) -> float:
        """
        Perform one outer-loop meta-update over a batch of tasks.

        Each task is a dict with keys: support_x, support_y, query_x, query_y.
        Returns the mean outer-loop loss.
        """
        # An empty batch leaves outer_loss a grad-free constant, and backward()
        # on it raises. A meta-step over no tasks is a no-op, not an error --
        # drift can clear before a task batch is assembled.
        if not tasks:
            return 0.0

        self._outer_opt.zero_grad()
        outer_loss = torch.tensor(0.0)

        for task in tasks:
            adapted_params = fast_adapt(
                self._model,
                task["support_x"],
                task["support_y"],
                self._loss_fn,
                k_steps=self._k_steps,
                lr_inner=self._lr_inner,
            )

            # Query loss with adapted parameters
            try:
                from torch.func import functional_call  # type: ignore[import]
            except ImportError:
                from torch._functorch.eager_transforms import (
                    functional_call,  # type: ignore[import, attr-defined, no-redef]
                )

            pred_q = functional_call(self._model, adapted_params, (task["query_x"],))  # type: ignore[arg-type]
            outer_loss = outer_loss + self._loss_fn(pred_q, task["query_y"])

        outer_loss = outer_loss / max(len(tasks), 1)
        outer_loss.backward()
        self._outer_opt.step()
        return float(outer_loss.item())


class HorizonMAMLAdapter:
    """
    Applies MAML to h8 and h10 (indices 7, 9) models on drift events.

    When ADWIN detects drift on h8/h10, this class loads the checkpoint,
    fast-adapts it on the N most recent bars, and saves the adapted weights
    back to the checkpoint directory.
    """

    def __init__(
        self,
        checkpoint_dir: Path,
        lr_inner: float = _DEFAULT_LR_INNER,
        k_steps: int = _DEFAULT_K_STEPS,
    ) -> None:
        self._ckpt_dir = checkpoint_dir
        self._lr_inner = lr_inner
        self._k_steps = k_steps

    def adapt_on_drift(
        self,
        horizon_idx: int,
        model: nn.Module,
        recent_x: torch.Tensor,
        recent_y: torch.Tensor,
        loss_fn: nn.Module | None = None,
    ) -> nn.Module:
        """
        Fast-adapt the model on recent support data and update its weights in place.

        Returns the adapted model.
        """
        if horizon_idx not in _TARGET_HORIZONS:
            log.debug("maml_skip_non_target_horizon", horizon_idx=horizon_idx)
            return model

        _loss = loss_fn or nn.CrossEntropyLoss()
        adapted_params = fast_adapt(model, recent_x, recent_y, _loss, self._k_steps, self._lr_inner)

        # Load adapted params into a copy of the model
        adapted_model = copy.deepcopy(model)
        with torch.no_grad():
            for name, param in adapted_model.named_parameters():
                if name in adapted_params:
                    param.copy_(adapted_params[name])

        ckpt_path = self._ckpt_dir / f"h{horizon_idx + 1}_adapted.pt"
        torch.save(adapted_model.state_dict(), ckpt_path)
        log.info("maml_fast_adapt_saved", horizon_idx=horizon_idx, path=str(ckpt_path))

        return adapted_model
