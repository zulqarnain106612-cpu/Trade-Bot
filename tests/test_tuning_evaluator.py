import random

import pytest

from src.tuning.evaluator import (
    ChallengerEvaluator,
    InsufficientSampleError,
    deflated_sharpe_ratio,
    probabilistic_sharpe_ratio,
)


def test_compare_metric_significant_improvement() -> None:
    evaluator = ChallengerEvaluator()
    champion = [0.01 + 0.001 * (i % 5) for i in range(40)]
    challenger = [0.05 + 0.001 * (i % 5) for i in range(40)]
    comparison = evaluator.compare_metric("oos_sharpe", champion, challenger)
    assert comparison.significant_improvement
    assert not comparison.significant_regression
    assert comparison.delta == pytest.approx(0.04)


def test_compare_metric_significant_regression() -> None:
    evaluator = ChallengerEvaluator()
    champion = [0.05 + 0.001 * (i % 5) for i in range(40)]
    challenger = [0.01 + 0.001 * (i % 5) for i in range(40)]
    comparison = evaluator.compare_metric("oos_sharpe", champion, challenger)
    assert comparison.significant_regression
    assert not comparison.significant_improvement


def test_compare_metric_noise_not_significant() -> None:
    evaluator = ChallengerEvaluator()
    champion = [0.01, 0.02, -0.01, 0.03, -0.02, 0.015, 0.005, -0.005, 0.02, 0.01] * 2
    challenger = [0.011, 0.021, -0.011, 0.031, -0.021, 0.016, 0.006, -0.006, 0.021, 0.011] * 2
    comparison = evaluator.compare_metric("oos_sharpe", champion, challenger)
    assert not comparison.significant_improvement
    assert not comparison.significant_regression


def test_compare_metric_insufficient_sample_raises() -> None:
    evaluator = ChallengerEvaluator()
    with pytest.raises(InsufficientSampleError):
        evaluator.compare_metric("oos_sharpe", [0.01] * 3, [0.02] * 3)


def test_compare_proportion_significant_improvement() -> None:
    evaluator = ChallengerEvaluator()
    comparison = evaluator.compare_proportion(
        "win_rate", champion_p=0.45, champion_n=500, challenger_p=0.60, challenger_n=500
    )
    assert comparison.significant_improvement
    assert not comparison.significant_regression


def test_compare_proportion_significant_regression() -> None:
    evaluator = ChallengerEvaluator()
    comparison = evaluator.compare_proportion(
        "win_rate", champion_p=0.60, champion_n=500, challenger_p=0.45, challenger_n=500
    )
    assert comparison.significant_regression


def test_compare_proportion_noise_not_significant() -> None:
    evaluator = ChallengerEvaluator()
    comparison = evaluator.compare_proportion(
        "win_rate", champion_p=0.50, champion_n=50, challenger_p=0.52, challenger_n=50
    )
    assert not comparison.significant_improvement
    assert not comparison.significant_regression


def test_compare_proportion_insufficient_sample_raises() -> None:
    evaluator = ChallengerEvaluator()
    with pytest.raises(InsufficientSampleError):
        evaluator.compare_proportion("win_rate", 0.5, 3, 0.5, 3)


def test_compare_proportion_degenerate_variance_not_significant() -> None:
    evaluator = ChallengerEvaluator()
    comparison = evaluator.compare_proportion(
        "win_rate", champion_p=0.0, champion_n=50, challenger_p=0.0, challenger_n=50
    )
    assert not comparison.significant_improvement
    assert not comparison.significant_regression


def test_evaluation_result_any_significant_regression() -> None:
    evaluator = ChallengerEvaluator()
    good = evaluator.compare_metric(
        "oos_sharpe",
        [0.01 + 0.001 * (i % 5) for i in range(40)],
        [0.05 + 0.001 * (i % 5) for i in range(40)],
    )
    bad = evaluator.compare_proportion(
        "max_drawdown_inverted",
        champion_p=0.85,
        champion_n=500,
        challenger_p=0.60,
        challenger_n=500,
    )
    result = evaluator.evaluate("hmm.entropy_threshold", 0.6, [good, bad])
    assert result.any_significant_regression
    assert result.improved("oos_sharpe")


def test_evaluation_result_improved_unknown_metric_raises() -> None:
    evaluator = ChallengerEvaluator()
    result = evaluator.evaluate("p", 0.6, [])
    with pytest.raises(KeyError):
        result.improved("does_not_exist")


# ---------------------------------------------------------------------------
# Probabilistic / Deflated Sharpe Ratio (Bailey & Lopez de Prado)
# ---------------------------------------------------------------------------


def test_psr_high_confidence_for_strong_positive_sharpe() -> None:
    rng = random.Random(1)
    returns = [rng.gauss(0.02, 0.01) for _ in range(200)]  # SR ~= 2.0
    psr = probabilistic_sharpe_ratio(returns, benchmark_sr=0.0)
    assert psr > 0.99


def test_psr_low_confidence_for_negative_mean_returns() -> None:
    rng = random.Random(2)
    returns = [rng.gauss(-0.02, 0.01) for _ in range(200)]
    psr = probabilistic_sharpe_ratio(returns, benchmark_sr=0.0)
    assert psr < 0.01


def test_psr_averages_near_half_for_zero_mean_noise() -> None:
    """PSR(true SR == 0) is, by construction, ~Uniform(0, 1) for any single
    noise realization -- so a single sample can legitimately land anywhere
    in (0, 1). Averaged over many independent realizations it must center
    on 0.5, which is what this checks."""
    rng = random.Random(3)
    psr_values = [
        probabilistic_sharpe_ratio([rng.gauss(0.0, 0.01) for _ in range(60)], benchmark_sr=0.0)
        for _ in range(300)
    ]
    assert 0.4 < (sum(psr_values) / len(psr_values)) < 0.6


def test_psr_degenerate_zero_variance_above_benchmark() -> None:
    assert probabilistic_sharpe_ratio([0.01] * 20, benchmark_sr=0.0) == 1.0
    assert probabilistic_sharpe_ratio([-0.01] * 20, benchmark_sr=0.0) == 0.0


def test_psr_insufficient_samples_returns_no_information() -> None:
    assert probabilistic_sharpe_ratio([0.01], benchmark_sr=0.0) == 0.5
    assert probabilistic_sharpe_ratio([], benchmark_sr=0.0) == 0.5


def test_dsr_deflates_relative_to_plain_psr_as_trials_increase() -> None:
    """More independent trials must never make the deflated confidence
    higher than fewer trials -- otherwise the multiple-testing correction
    would be backwards."""
    rng = random.Random(4)
    returns = [rng.gauss(0.01, 0.01) for _ in range(200)]

    psr_1_trial = deflated_sharpe_ratio(returns, n_trials=1)
    dsr_10_trials = deflated_sharpe_ratio(returns, n_trials=10)
    dsr_100_trials = deflated_sharpe_ratio(returns, n_trials=100)

    assert dsr_10_trials <= psr_1_trial
    assert dsr_100_trials <= dsr_10_trials


def test_dsr_deflation_is_scale_invariant() -> None:
    """
    The multiple-testing deflation belongs in Sharpe units (cross-trial
    dispersion ~ 1/sqrt(n)), not in return units. Two series with the same
    Sharpe ratio and length must deflate identically no matter how the
    returns are scaled — scaling by the returns' own stdev, as this did
    before, made a low-volatility strategy look better corrected than a
    high-volatility one with identical risk-adjusted performance.
    """
    rng = random.Random(11)
    base = [rng.gauss(0.01, 0.01) for _ in range(200)]
    scaled = [r * 10.0 for r in base]

    assert deflated_sharpe_ratio(base, n_trials=50) == pytest.approx(
        deflated_sharpe_ratio(scaled, n_trials=50)
    )


def test_dsr_deflation_is_strict_for_multiple_trials() -> None:
    """The correction must actually bite, not round to nothing."""
    rng = random.Random(12)
    returns = [rng.gauss(0.002, 0.01) for _ in range(200)]
    assert deflated_sharpe_ratio(returns, n_trials=50) < probabilistic_sharpe_ratio(returns)


def test_dsr_with_one_trial_equals_plain_psr() -> None:
    rng = random.Random(5)
    returns = [rng.gauss(0.01, 0.01) for _ in range(100)]
    assert deflated_sharpe_ratio(returns, n_trials=1) == pytest.approx(
        probabilistic_sharpe_ratio(returns, benchmark_sr=0.0)
    )
