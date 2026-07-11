import random

import pytest

from src.config import FeatureSettings
from src.tuning.backtest_harness import (
    InsufficientDataError,
    TradeSample,
    _fold_sharpe,
    _make_folds,
    _max_drawdown_inverted,
    _position_scalar,
    run_entropy_threshold_backtest,
)


def test_position_scalar_matches_regime_detector_formula() -> None:
    # Below threshold -> full size
    assert _position_scalar(0.2, threshold=0.5, floor=0.5) == 1.0
    # At max entropy -> floor
    assert _position_scalar(1.0, threshold=0.5, floor=0.5) == pytest.approx(0.5)
    # Halfway between threshold and 1.0 -> halfway between 1.0 and floor
    assert _position_scalar(0.75, threshold=0.5, floor=0.5) == pytest.approx(0.75)


def test_make_folds_respects_purge_gap() -> None:
    folds = _make_folds(n=100, n_splits=5, purge_gap=2)
    assert len(folds) == 5
    for start, end in folds:
        assert start <= end


def test_make_folds_insufficient_data_raises() -> None:
    with pytest.raises(InsufficientDataError):
        _make_folds(n=10, n_splits=10, purge_gap=5)


def test_max_drawdown_inverted_no_drawdown() -> None:
    assert _max_drawdown_inverted([0.01, 0.01, 0.01]) == pytest.approx(1.0)


def test_max_drawdown_inverted_with_drawdown() -> None:
    # Cumulative: 0.1, -0.1 (peak 0.1, trough -0.1 -> dd 0.2)
    result = _max_drawdown_inverted([0.1, -0.2])
    assert result == pytest.approx(0.8)


def test_fold_sharpe_zero_variance_returns_zero() -> None:
    assert _fold_sharpe([0.01, 0.01, 0.01]) == 0.0


def test_fold_sharpe_single_sample_returns_zero() -> None:
    assert _fold_sharpe([0.01]) == 0.0


def _synthetic_samples(n: int, seed: int = 0) -> list[TradeSample]:
    rng = random.Random(seed)
    samples = []
    for _ in range(n):
        entropy = rng.uniform(0.0, 1.0)
        raw_return = rng.gauss(0.005, 0.02)
        samples.append(TradeSample(entropy=entropy, raw_return=raw_return))
    return samples


def test_run_entropy_threshold_backtest_produces_four_comparisons() -> None:
    samples = _synthetic_samples(300, seed=1)
    cfg = FeatureSettings(cpcv_n_splits=10, purge_gap_bars=1, triple_barrier_max_holding_bars=1)
    comparisons = run_entropy_threshold_backtest(
        samples,
        champion_threshold=0.5,
        champion_floor=0.5,
        challenger_threshold=0.4,
        challenger_floor=0.6,
        features_cfg=cfg,
    )
    names = {c.metric_name for c in comparisons}
    assert names == {
        "oos_sharpe",
        "max_drawdown_inverted",
        "win_rate",
        "probabilistic_sharpe_ratio",
    }


def test_identical_champion_and_challenger_never_significant() -> None:
    """Sanity/null-effect control: same threshold+floor on both arms must
    never produce a significant improvement or regression -- this is the
    harness self-check described in the Phase 4 exit criteria."""
    samples = _synthetic_samples(300, seed=2)
    cfg = FeatureSettings(cpcv_n_splits=10, purge_gap_bars=1, triple_barrier_max_holding_bars=1)
    comparisons = run_entropy_threshold_backtest(
        samples,
        champion_threshold=0.5,
        champion_floor=0.5,
        challenger_threshold=0.5,
        challenger_floor=0.5,
        features_cfg=cfg,
    )
    for c in comparisons:
        assert not c.significant_improvement
        assert not c.significant_regression


def test_lower_floor_can_only_shrink_or_equal_scaled_returns() -> None:
    """A challenger with a lower floor (more aggressive de-risking under
    uncertainty) must never scale a positive-entropy trade's return up
    relative to a higher-floor champion -- a basic monotonicity sanity
    check on the position_scalar math itself."""
    entropy = 0.9
    champion_scalar = _position_scalar(entropy, threshold=0.5, floor=0.6)
    challenger_scalar = _position_scalar(entropy, threshold=0.5, floor=0.3)
    assert challenger_scalar <= champion_scalar
