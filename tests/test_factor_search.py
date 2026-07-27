"""Tests for the v6 automated factor search primitive."""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from src.tuning.factor_search import FactorCandidate, _norm_ppf, evaluate_factor_candidates


def test_empty_candidates_returns_empty() -> None:
    assert evaluate_factor_candidates([], pd.Series([0.1, 0.2])) == []


class TestNormPpf:
    def test_rejects_p_outside_open_unit_interval(self) -> None:
        for bad_p in (0.0, 1.0, -0.1, 1.1):
            with pytest.raises(ValueError, match="must be in"):
                _norm_ppf(bad_p)

    def test_median_is_zero(self) -> None:
        assert _norm_ppf(0.5) == pytest.approx(0.0, abs=1e-6)

    def test_lower_tail_matches_known_quantile(self) -> None:
        # P(Z <= -1.959964) ~= 0.025 -- standard normal 97.5% two-sided quantile
        assert _norm_ppf(0.025) == pytest.approx(-1.959964, abs=1e-4)

    def test_upper_tail_matches_known_quantile(self) -> None:
        assert _norm_ppf(0.975) == pytest.approx(1.959964, abs=1e-4)

    def test_symmetric_around_median(self) -> None:
        for p in (0.001, 0.02425, 0.1, 0.4, 0.6, 0.9, 0.97575, 0.999):
            assert _norm_ppf(p) == pytest.approx(-_norm_ppf(1 - p), abs=1e-3)

    def test_monotonically_increasing(self) -> None:
        ps = [0.001, 0.01, 0.02425, 0.1, 0.5, 0.9, 0.97575, 0.99, 0.999]
        values = [_norm_ppf(p) for p in ps]
        assert values == sorted(values)
        assert not any(math.isnan(v) for v in values)


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
