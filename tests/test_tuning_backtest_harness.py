import random

import numpy as np
import pandas as pd
import pytest
from xgboost import XGBClassifier

from src.config import FeatureSettings, XGBoostSettings
from src.data.storage import TradeRecord
from src.features.pipeline import FEATURE_COLUMNS, build_feature_matrix
from src.tuning.backtest_harness import (
    EnsembleBlendSample,
    InsufficientDataError,
    SlippageFillSample,
    TradeSample,
    UnknownFeatureWindowFieldError,
    UnknownXGBHyperparamFieldError,
    _blended_p_long,
    _fold_sharpe,
    _make_folds,
    _max_drawdown_inverted,
    _position_scalar,
    _predict_direction_batch,
    _predicted_slippage_bps,
    _realized_slippage_bps,
    ensemble_blend_samples_from_trades,
    run_ensemble_blend_backtest,
    run_entropy_threshold_backtest,
    run_feature_window_backtest,
    run_slippage_coeff_backtest,
    run_xgboost_hyperparam_backtest,
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
# risk.ensemble_blend_weight -- EnsemblePredictor blend recalibration
# ---------------------------------------------------------------------------


def _make_ensemble_trade(
    raw_p_long: float,
    ensemble_point_estimate: float,
    blend_weight: float,
    direction: int,
    entry_price: float = 100.0,
    exit_price: float | None = 101.0,
) -> TradeRecord:
    """Build a closed TradeRecord as if signal_engine.py's blend had already
    been applied -- raw_signal carries the POST-blend p_long, matching what
    orchestrator.py/storage.py actually persist."""
    blended_p_long = (1.0 - blend_weight) * raw_p_long + blend_weight * ensemble_point_estimate
    return TradeRecord(
        id="t1",
        symbol="BTC/USDT",
        timeframe="15m",
        trading_mode="paper",
        execution_mode="paper",
        direction=direction,
        entry_price=entry_price,
        exit_price=exit_price,
        quantity=1.0,
        notional_usd=entry_price,
        entry_ts=1000,
        exit_ts=2000,
        pnl_usd=None,
        pnl_pct=None,
        fee_usd=0.1,
        kelly_fraction=0.1,
        regime_at_entry=1,
        meta_label_prob=0.6,
        exit_reason="profit_target",
        approved_by="auto",
        raw_signal=blended_p_long,
        ensemble_point_estimate=ensemble_point_estimate,
        ensemble_blend_weight=blend_weight,
    )


def test_blended_p_long_matches_signal_engine_formula() -> None:
    sample = EnsembleBlendSample(
        raw_p_long=0.6, ensemble_point_estimate=0.8, direction=1, raw_return=0.01
    )
    assert _blended_p_long(sample, 0.0) == pytest.approx(0.6)
    assert _blended_p_long(sample, 1.0) == pytest.approx(0.8)
    assert _blended_p_long(sample, 0.5) == pytest.approx(0.7)


def test_ensemble_blend_samples_from_trades_reconstructs_raw_p_long() -> None:
    trade = _make_ensemble_trade(
        raw_p_long=0.65, ensemble_point_estimate=0.55, blend_weight=0.4, direction=1
    )
    samples = ensemble_blend_samples_from_trades([trade])
    assert len(samples) == 1
    assert samples[0].raw_p_long == pytest.approx(0.65)
    assert samples[0].ensemble_point_estimate == pytest.approx(0.55)
    assert samples[0].direction == 1
    assert samples[0].raw_return == pytest.approx(0.01)


def test_ensemble_blend_samples_from_trades_skips_open_trades() -> None:
    trade = _make_ensemble_trade(
        raw_p_long=0.65, ensemble_point_estimate=0.55, blend_weight=0.4, direction=1,
        exit_price=None,
    )
    assert ensemble_blend_samples_from_trades([trade]) == []


def test_ensemble_blend_samples_from_trades_skips_unblended_trades() -> None:
    trade = TradeRecord(
        id="t1",
        symbol="BTC/USDT",
        timeframe="15m",
        trading_mode="paper",
        execution_mode="paper",
        direction=1,
        entry_price=100.0,
        exit_price=101.0,
        quantity=1.0,
        notional_usd=100.0,
        entry_ts=1000,
        exit_ts=2000,
        pnl_usd=None,
        pnl_pct=None,
        fee_usd=0.1,
        kelly_fraction=0.1,
        regime_at_entry=1,
        meta_label_prob=0.6,
        exit_reason="profit_target",
        approved_by="auto",
        raw_signal=0.6,
        ensemble_point_estimate=None,
        ensemble_blend_weight=None,
    )
    assert ensemble_blend_samples_from_trades([trade]) == []


@pytest.mark.parametrize("degenerate_weight", [0.0, 1.0])
def test_ensemble_blend_samples_from_trades_skips_degenerate_weights(
    degenerate_weight: float,
) -> None:
    trade = _make_ensemble_trade(
        raw_p_long=0.65,
        ensemble_point_estimate=0.55,
        blend_weight=degenerate_weight,
        direction=1,
    )
    assert ensemble_blend_samples_from_trades([trade]) == []


def _synthetic_ensemble_samples(
    n: int, true_weight: float, seed: int = 0
) -> list[EnsembleBlendSample]:
    """Trades whose realized win/loss is generated from a TRUE blend weight
    -- lets tests assert a challenger closer to true_weight scores higher
    prediction accuracy, mirroring the slippage-coeff harness's tests."""
    rng = random.Random(seed)
    samples = []
    for _ in range(n):
        raw_p_long = rng.uniform(0.2, 0.8)
        ensemble_point_estimate = rng.uniform(0.2, 0.8)
        direction = rng.choice([0, 1])
        true_p_long = (1.0 - true_weight) * raw_p_long + true_weight * ensemble_point_estimate
        true_p_win = true_p_long if direction == 1 else (1.0 - true_p_long)
        won = rng.random() < true_p_win
        raw_return = abs(rng.gauss(0.01, 0.005)) * (1 if won else -1)
        samples.append(
            EnsembleBlendSample(
                raw_p_long=raw_p_long,
                ensemble_point_estimate=ensemble_point_estimate,
                direction=direction,
                raw_return=raw_return,
            )
        )
    return samples


def test_run_ensemble_blend_backtest_produces_two_comparisons() -> None:
    samples = _synthetic_ensemble_samples(300, true_weight=0.5, seed=1)
    cfg = FeatureSettings(cpcv_n_splits=10, purge_gap_bars=1, triple_barrier_max_holding_bars=1)
    comparisons = run_ensemble_blend_backtest(
        samples, champion_weight=0.2, challenger_weight=0.8, features_cfg=cfg
    )
    names = {c.metric_name for c in comparisons}
    assert names == {"ensemble_prediction_accuracy", "oos_sharpe"}


def test_ensemble_blend_weight_closer_to_truth_scores_higher_accuracy() -> None:
    samples = _synthetic_ensemble_samples(400, true_weight=0.9, seed=2)
    cfg = FeatureSettings(cpcv_n_splits=10, purge_gap_bars=1, triple_barrier_max_holding_bars=1)
    comparisons = run_ensemble_blend_backtest(
        samples, champion_weight=0.1, challenger_weight=0.9, features_cfg=cfg
    )
    accuracy = next(c for c in comparisons if c.metric_name == "ensemble_prediction_accuracy")
    assert accuracy.challenger_mean > accuracy.champion_mean
    assert accuracy.significant_improvement


def test_identical_ensemble_blend_weight_never_significant() -> None:
    samples = _synthetic_ensemble_samples(300, true_weight=0.5, seed=3)
    cfg = FeatureSettings(cpcv_n_splits=10, purge_gap_bars=1, triple_barrier_max_holding_bars=1)
    comparisons = run_ensemble_blend_backtest(
        samples, champion_weight=0.5, challenger_weight=0.5, features_cfg=cfg
    )
    for c in comparisons:
        assert not c.significant_improvement
        assert not c.significant_regression


def test_run_ensemble_blend_backtest_defaults_features_cfg() -> None:
    """features_cfg=None should load a valid default FeatureSettings, same
    as run_entropy_threshold_backtest / run_slippage_coeff_backtest."""
    samples = _synthetic_ensemble_samples(1000, true_weight=0.5, seed=4)
    comparisons = run_ensemble_blend_backtest(samples, champion_weight=0.3, challenger_weight=0.6)
    assert len(comparisons) == 2


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


# ---------------------------------------------------------------------------
# Phase 8 item 4 -- XGBoost hyperparameters
# ---------------------------------------------------------------------------

_XGB_CPCV_CFG = FeatureSettings(
    cpcv_n_splits=10, cpcv_n_test_splits=1, purge_gap_bars=1, triple_barrier_max_holding_bars=1
)
_FAST_XGB = XGBoostSettings(n_estimators=10, max_depth=2, early_stopping_rounds=5)


@pytest.fixture(scope="module")
def xgb_feature_matrix():
    """Built once per module -- build_feature_matrix itself is cheap; this
    just avoids repeating pipeline logging/work across every test below."""
    bars = _synthetic_bars(3000, seed=7)
    return build_feature_matrix(bars, cfg=_XGB_CPCV_CFG)


def test_run_xgboost_hyperparam_backtest_unknown_field_raises(xgb_feature_matrix) -> None:
    with pytest.raises(UnknownXGBHyperparamFieldError):
        run_xgboost_hyperparam_backtest(
            xgb_feature_matrix,
            field_name="not_a_real_field",
            champion_value=2,
            challenger_value=3,
            base_xgb_cfg=_FAST_XGB,
            symbol="BTC/USDT",
            timeframe="15m",
            feature_cfg=_XGB_CPCV_CFG,
        )


def test_run_xgboost_hyperparam_backtest_produces_two_comparisons(xgb_feature_matrix) -> None:
    comparisons = run_xgboost_hyperparam_backtest(
        xgb_feature_matrix,
        field_name="max_depth",
        champion_value=2,
        challenger_value=3,
        base_xgb_cfg=_FAST_XGB,
        symbol="BTC/USDT",
        timeframe="15m",
        feature_cfg=_XGB_CPCV_CFG,
    )
    names = {c.metric_name for c in comparisons}
    assert names == {"oos_sharpe", "accuracy"}


def test_identical_xgboost_hyperparam_never_significant(xgb_feature_matrix) -> None:
    comparisons = run_xgboost_hyperparam_backtest(
        xgb_feature_matrix,
        field_name="max_depth",
        champion_value=2,
        challenger_value=2,
        base_xgb_cfg=_FAST_XGB,
        symbol="BTC/USDT",
        timeframe="15m",
        feature_cfg=_XGB_CPCV_CFG,
    )
    for c in comparisons:
        assert not c.significant_improvement
        assert not c.significant_regression


def test_run_xgboost_hyperparam_backtest_runs_for_reg_alpha(xgb_feature_matrix) -> None:
    comparisons = run_xgboost_hyperparam_backtest(
        xgb_feature_matrix,
        field_name="reg_alpha",
        champion_value=0.1,
        challenger_value=0.12,
        base_xgb_cfg=_FAST_XGB,
        symbol="BTC/USDT",
        timeframe="15m",
        feature_cfg=_XGB_CPCV_CFG,
    )
    assert len(comparisons) == 2
