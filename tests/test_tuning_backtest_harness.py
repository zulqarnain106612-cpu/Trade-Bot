import random

import numpy as np
import pandas as pd
import pytest
from xgboost import XGBClassifier

from src.config import FeatureSettings
from src.features.pipeline import FEATURE_COLUMNS
from src.tuning.backtest_harness import (
    InsufficientDataError,
    SlippageFillSample,
    TradeSample,
    UnknownFeatureWindowFieldError,
    _fold_sharpe,
    _make_folds,
    _max_drawdown_inverted,
    _position_scalar,
    _predict_direction_batch,
    _predicted_slippage_bps,
    _realized_slippage_bps,
    run_entropy_threshold_backtest,
    run_feature_window_backtest,
    run_slippage_coeff_backtest,
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


def test_realized_slippage_bps_long_and_short_sign() -> None:
    long_sample = SlippageFillSample(
        reference_price=100.0, fill_price=100.5, qty=1.0, adv_20d=10.0, spread_bps=2.0, direction=1
    )
    short_sample = SlippageFillSample(
        reference_price=100.0,
        fill_price=100.5,
        qty=1.0,
        adv_20d=10.0,
        spread_bps=2.0,
        direction=-1,
    )
    # Long fills above reference -> positive (costly) slippage.
    assert _realized_slippage_bps(long_sample) == pytest.approx(50.0)
    # Same fill/reference, but short -> the move is favorable, not costly.
    assert _realized_slippage_bps(short_sample) == pytest.approx(-50.0)


def test_predicted_slippage_bps_matches_almgren_chriss_formula() -> None:
    sample = SlippageFillSample(
        reference_price=100.0, fill_price=100.0, qty=4.0, adv_20d=16.0, spread_bps=2.0, direction=1
    )
    # participation = 4/16 = 0.25, sqrt(0.25) = 0.5
    assert _predicted_slippage_bps(sample, impact_coeff_bps=10.0) == pytest.approx(2.0 + 5.0)


def test_predicted_slippage_bps_zero_adv_is_spread_only() -> None:
    sample = SlippageFillSample(
        reference_price=100.0, fill_price=100.0, qty=1.0, adv_20d=0.0, spread_bps=2.0, direction=1
    )
    assert _predicted_slippage_bps(sample, impact_coeff_bps=10.0) == pytest.approx(2.0)


def _synthetic_slippage_samples(
    n: int, true_impact_coeff: float, seed: int = 0
) -> list[SlippageFillSample]:
    rng = random.Random(seed)
    samples = []
    for _ in range(n):
        qty = rng.uniform(0.5, 5.0)
        adv_20d = rng.uniform(50.0, 200.0)
        direction = rng.choice([1, -1])
        reference_price = 100.0
        participation = qty / adv_20d
        true_slippage_bps = 2.0 + true_impact_coeff * participation**0.5 + rng.gauss(0.0, 0.5)
        sign = 1.0 if direction == 1 else -1.0
        fill_price = reference_price * (1.0 + sign * true_slippage_bps / 10_000.0)
        samples.append(
            SlippageFillSample(
                reference_price=reference_price,
                fill_price=fill_price,
                qty=qty,
                adv_20d=adv_20d,
                spread_bps=2.0,
                direction=direction,
            )
        )
    return samples


def test_run_slippage_coeff_backtest_produces_two_comparisons() -> None:
    samples = _synthetic_slippage_samples(300, true_impact_coeff=10.0, seed=1)
    cfg = FeatureSettings(cpcv_n_splits=10, purge_gap_bars=1, triple_barrier_max_holding_bars=1)
    comparisons = run_slippage_coeff_backtest(
        samples, champion_coeff=8.0, challenger_coeff=12.0, features_cfg=cfg
    )
    names = {c.metric_name for c in comparisons}
    assert names == {"slippage_prediction_accuracy", "slippage_prediction_bias"}


def test_slippage_coeff_closer_to_truth_scores_higher_accuracy() -> None:
    """A challenger coefficient closer to the true generating coefficient
    must produce a less-negative (better) mean prediction-accuracy score
    than a champion far from the truth."""
    samples = _synthetic_slippage_samples(300, true_impact_coeff=10.0, seed=3)
    cfg = FeatureSettings(cpcv_n_splits=10, purge_gap_bars=1, triple_barrier_max_holding_bars=1)
    comparisons = run_slippage_coeff_backtest(
        samples, champion_coeff=2.0, challenger_coeff=10.0, features_cfg=cfg
    )
    accuracy = next(c for c in comparisons if c.metric_name == "slippage_prediction_accuracy")
    assert accuracy.challenger_mean > accuracy.champion_mean
    assert accuracy.significant_improvement


def test_identical_slippage_coeff_never_significant() -> None:
    samples = _synthetic_slippage_samples(300, true_impact_coeff=10.0, seed=4)
    cfg = FeatureSettings(cpcv_n_splits=10, purge_gap_bars=1, triple_barrier_max_holding_bars=1)
    comparisons = run_slippage_coeff_backtest(
        samples, champion_coeff=10.0, challenger_coeff=10.0, features_cfg=cfg
    )
    for c in comparisons:
        assert not c.significant_improvement
        assert not c.significant_regression


# ---------------------------------------------------------------------------
# Phase 8 item 3 -- feature-window parameters
# ---------------------------------------------------------------------------

_FEATURE_WINDOW_CFG = FeatureSettings(
    cpcv_n_splits=10, purge_gap_bars=1, triple_barrier_max_holding_bars=1
)


def _synthetic_bars(n: int = 1000, seed: int = 0) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    returns = rng.normal(0.0002, 0.01, n)
    close = 100.0 * np.cumprod(1.0 + returns)
    high = close * (1.0 + np.abs(rng.normal(0.0, 0.002, n)))
    low = close * (1.0 - np.abs(rng.normal(0.0, 0.002, n)))
    open_ = np.concatenate(([close[0]], close[:-1]))
    volume = rng.uniform(50.0, 150.0, n)
    idx = (np.arange(n) * 900_000).astype(np.int64)  # 15m bars, Unix-ms
    return pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": volume}, index=idx
    )


def _fitted_direction_model(seed: int = 0) -> XGBClassifier:
    rng = np.random.default_rng(seed)
    X = rng.standard_normal((300, len(FEATURE_COLUMNS)))
    y = (X[:, 0] > 0).astype(int)
    model = XGBClassifier(n_estimators=3, max_depth=2, verbosity=0)
    model.fit(X, y)
    return model


def test_predict_direction_batch_matches_model_predict() -> None:
    model = _fitted_direction_model()
    rng = np.random.default_rng(1)
    X = pd.DataFrame(rng.standard_normal((20, len(FEATURE_COLUMNS))), columns=FEATURE_COLUMNS)
    pred = _predict_direction_batch(model, X)
    expected = model.predict(X.to_numpy(dtype=np.float64))
    assert np.array_equal(pred, expected)


def test_predict_direction_batch_slices_extra_columns() -> None:
    """Mirrors ModelTrainer.predict_direction's n_features_in_-based
    slicing -- a wider feature frame than the model expects is truncated,
    not rejected."""
    model = _fitted_direction_model()
    rng = np.random.default_rng(2)
    cols = [*FEATURE_COLUMNS, "extra_intel_col"]
    X = pd.DataFrame(rng.standard_normal((20, len(cols))), columns=cols)
    pred = _predict_direction_batch(model, X)
    expected = model.predict(X[FEATURE_COLUMNS].to_numpy(dtype=np.float64))
    assert np.array_equal(pred, expected)


def test_run_feature_window_backtest_unknown_field_raises() -> None:
    bars = _synthetic_bars(1000)
    model = _fitted_direction_model()
    with pytest.raises(UnknownFeatureWindowFieldError):
        run_feature_window_backtest(
            bars,
            field_name="not_a_real_field",
            champion_window=14,
            challenger_window=17,
            direction_model=model,
            features_cfg=_FEATURE_WINDOW_CFG,
        )


def test_run_feature_window_backtest_defaults_features_cfg() -> None:
    """features_cfg=None should load a valid default FeatureSettings, same
    as run_entropy_threshold_backtest / run_slippage_coeff_backtest."""
    bars = _synthetic_bars(1500)
    model = _fitted_direction_model()
    comparisons = run_feature_window_backtest(
        bars,
        field_name="atr_window",
        champion_window=14,
        challenger_window=17,
        direction_model=model,
    )
    assert {c.metric_name for c in comparisons} == {"oos_sharpe", "win_rate"}


def test_run_feature_window_backtest_produces_two_comparisons() -> None:
    bars = _synthetic_bars(1000)
    model = _fitted_direction_model()
    comparisons = run_feature_window_backtest(
        bars,
        field_name="atr_window",
        champion_window=14,
        challenger_window=17,
        direction_model=model,
        features_cfg=_FEATURE_WINDOW_CFG,
    )
    names = {c.metric_name for c in comparisons}
    assert names == {"oos_sharpe", "win_rate"}


@pytest.mark.parametrize(
    "field_name",
    ["vwap_window", "ofi_window", "atr_window", "sharpe_window", "volume_zscore_window"],
)
def test_run_feature_window_backtest_runs_for_every_field(field_name: str) -> None:
    bars = _synthetic_bars(1000)
    model = _fitted_direction_model()
    comparisons = run_feature_window_backtest(
        bars,
        field_name=field_name,
        champion_window=20,
        challenger_window=24,
        direction_model=model,
        features_cfg=_FEATURE_WINDOW_CFG,
    )
    assert len(comparisons) == 2


def test_identical_feature_window_never_significant() -> None:
    bars = _synthetic_bars(1000)
    model = _fitted_direction_model()
    comparisons = run_feature_window_backtest(
        bars,
        field_name="atr_window",
        champion_window=14,
        challenger_window=14,
        direction_model=model,
        features_cfg=_FEATURE_WINDOW_CFG,
    )
    for c in comparisons:
        assert not c.significant_improvement
        assert not c.significant_regression
