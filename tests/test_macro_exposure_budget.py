"""Tests for the v7 macro-conditioned exposure budget."""

from __future__ import annotations

import pytest

from src.intelligence.macro_regime import MacroRegime, MacroRegimeResult
from src.risk.macro_exposure_budget import (
    apply_macro_budget_to_kelly_fraction,
    compute_macro_exposure_scalar,
)


def test_max_risk_on_gives_scalar_near_one() -> None:
    macro = MacroRegimeResult(regime=MacroRegime.RISK_ON, risk_appetite=1.0)
    budget = compute_macro_exposure_scalar(macro)
    assert budget.scalar == pytest.approx(1.0)


def test_max_risk_off_gives_minimum_scalar() -> None:
    macro = MacroRegimeResult(regime=MacroRegime.RISK_OFF, risk_appetite=-1.0)
    budget = compute_macro_exposure_scalar(macro)
    assert budget.scalar == pytest.approx(0.25)


def test_neutral_gives_midpoint_scalar() -> None:
    macro = MacroRegimeResult(regime=MacroRegime.NEUTRAL, risk_appetite=0.0)
    budget = compute_macro_exposure_scalar(macro)
    assert budget.scalar == pytest.approx(0.625)


def test_scalar_never_exceeds_one() -> None:
    macro = MacroRegimeResult(regime=MacroRegime.RISK_ON, risk_appetite=5.0)
    budget = compute_macro_exposure_scalar(macro)
    assert budget.scalar <= 1.0


def test_apply_budget_never_increases_kelly_fraction() -> None:
    macro = MacroRegimeResult(regime=MacroRegime.RISK_ON, risk_appetite=1.0)
    budget = compute_macro_exposure_scalar(macro)
    scaled = apply_macro_budget_to_kelly_fraction(0.10, budget)
    assert scaled <= 0.10


def test_apply_budget_shrinks_on_risk_off() -> None:
    macro = MacroRegimeResult(regime=MacroRegime.RISK_OFF, risk_appetite=-1.0)
    budget = compute_macro_exposure_scalar(macro)
    scaled = apply_macro_budget_to_kelly_fraction(0.10, budget)
    assert scaled == pytest.approx(0.025)


def test_apply_budget_rejects_negative_kelly_fraction() -> None:
    macro = MacroRegimeResult(regime=MacroRegime.NEUTRAL, risk_appetite=0.0)
    budget = compute_macro_exposure_scalar(macro)
    with pytest.raises(ValueError, match="kelly_fraction"):
        apply_macro_budget_to_kelly_fraction(-0.01, budget)
