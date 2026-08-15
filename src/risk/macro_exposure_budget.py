"""
Macro-conditioned exposure budget — v7 Portfolio-Level Macro Overlay.

Scales aggregate portfolio exposure with macro regime confidence
(src/intelligence/macro_regime.py), layered strictly *underneath* the
existing Kelly ceiling — this budget can only shrink exposure below what
Kelly already allows, never expand beyond it (Domain Prior: Kelly is a
ceiling, not a target, and this module must not become a backdoor around
that ceiling).

Authority:
  - Domain Prior: Kelly is a ceiling, not a target; enforce drawdown and
    position limits — this module composes with, never replaces, Kelly.
"""

from __future__ import annotations

from dataclasses import dataclass

from src.intelligence.macro_regime import MacroRegimeResult


_MIN_BUDGET_SCALAR: float = 0.25  # Even at max risk-off, never fully zero out
_MAX_BUDGET_SCALAR: float = 1.0  # Never scale ABOVE the Kelly-derived size


@dataclass(frozen=True, slots=True)
class MacroExposureBudget:
    """Multiplicative scalar derived from macro risk appetite."""

    scalar: float
    reason: str


def compute_macro_exposure_scalar(macro: MacroRegimeResult) -> MacroExposureBudget:
    """
    Linear map from risk_appetite in [-1, 1] to a scalar in
    [_MIN_BUDGET_SCALAR, _MAX_BUDGET_SCALAR]. Never exceeds 1.0 — this
    function can only shrink Kelly-derived sizing, never amplify it.
    """
    # risk_appetite=-1 -> _MIN_BUDGET_SCALAR; risk_appetite=+1 -> _MAX_BUDGET_SCALAR
    normalized = (macro.risk_appetite + 1.0) / 2.0  # [0, 1]
    scalar = _MIN_BUDGET_SCALAR + normalized * (_MAX_BUDGET_SCALAR - _MIN_BUDGET_SCALAR)
    scalar = max(_MIN_BUDGET_SCALAR, min(_MAX_BUDGET_SCALAR, scalar))

    return MacroExposureBudget(
        scalar=scalar,
        reason=(
            f"macro regime={macro.regime.value} risk_appetite={macro.risk_appetite:.3f} "
            f"-> exposure_scalar={scalar:.3f}"
        ),
    )


def apply_macro_budget_to_kelly_fraction(
    kelly_fraction: float, budget: MacroExposureBudget
) -> float:
    """
    Applies the macro scalar to an already-computed Kelly fraction.
    Guaranteed <= kelly_fraction since budget.scalar <= 1.0 by construction.
    """
    if kelly_fraction < 0.0:
        raise ValueError(f"kelly_fraction must be non-negative, got {kelly_fraction}")
    return kelly_fraction * budget.scalar
