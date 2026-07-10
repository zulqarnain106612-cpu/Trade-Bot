import pytest

from src.tuning.evaluator import ChallengerEvaluator, InsufficientSampleError


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
