"""Tests for the v6 automated factor search primitive."""

from __future__ import annotations

import numpy as np
import pandas as pd

from src.tuning.factor_search import FactorCandidate, evaluate_factor_candidates


def test_empty_candidates_returns_empty() -> None:
    assert evaluate_factor_candidates([], pd.Series([0.1, 0.2])) == []


def test_strong_signal_passes_correction() -> None:
    rng = np.random.default_rng(0)
    n = 500
    factor = pd.Series(rng.normal(0, 1, n))
    forward_returns = factor * 0.8 + rng.normal(0, 0.1, n)
    candidates = [FactorCandidate("strong_factor", factor)]
    results = evaluate_factor_candidates(candidates, forward_returns)
    assert len(results) == 1
    assert abs(results[0].information_coefficient) > 0.5


def test_pure_noise_factor_usually_fails() -> None:
    rng = np.random.default_rng(1)
    n = 200
    factor = pd.Series(rng.normal(0, 1, n))
    forward_returns = pd.Series(rng.normal(0, 1, n))
    candidates = [FactorCandidate(f"noise_{i}", pd.Series(rng.normal(0, 1, n))) for i in range(20)]
    candidates.append(FactorCandidate("target_noise", factor))
    results = evaluate_factor_candidates(candidates, forward_returns)
    # With Bonferroni correction across 21 trials, pure noise should mostly fail.
    pass_count = sum(1 for r in results if r.passes_multiple_testing_correction)
    assert pass_count <= 3


def test_more_trials_raises_bar_via_deflation() -> None:
    rng = np.random.default_rng(2)
    n = 300
    factor = pd.Series(rng.normal(0, 1, n))
    forward_returns = factor * 0.15 + rng.normal(0, 1, n)

    few = evaluate_factor_candidates([FactorCandidate("f", factor)], forward_returns)
    many_candidates = [
        FactorCandidate(f"noise_{i}", pd.Series(rng.normal(0, 1, n))) for i in range(99)
    ]
    many_candidates.append(FactorCandidate("f", factor))
    many = evaluate_factor_candidates(many_candidates, forward_returns)

    f_few = next(r for r in few if r.name == "f")
    f_many = next(r for r in many if r.name == "f")
    assert f_many.deflated_sharpe <= f_few.deflated_sharpe


def test_short_series_returns_zero_ic() -> None:
    candidates = [FactorCandidate("short", pd.Series([1.0, 2.0]))]
    results = evaluate_factor_candidates(candidates, pd.Series([0.1, 0.2]))
    assert results[0].information_coefficient == 0.0
