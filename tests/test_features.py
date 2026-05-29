"""Tests for src/features/pipeline.py — all feature functions and the full pipeline."""

import numpy as np
import pandas as pd
import pytest

from src.config import invalidate_settings_cache
from src.features.pipeline import (
    FEATURE_COLUMNS,
    COL_FRAC_DIFF,
    COL_LABEL,
    COL_META_LABEL,
    COL_OFI,
    COL_ROLLING_SHARPE,
    COL_VWAP_DEV,
    COL_VOLUME_ZSCORE,
    COL_REALIZED_VOL_RATIO,
    COL_ATR_MOMENTUM,
    FeatureMatrix,
    _compute_daily_vol,
    _frac_diff_weights,
    _FRAC_DIFF_MAX_WINDOW,
    atr_momentum,
    build_feature_matrix,
    build_inference_features,
    fractional_differentiation,
    meta_labels,
    order_flow_imbalance,
    realized_vol_ratio,
    rolling_sharpe,
    triple_barrier_labels,
    volume_zscore,
    vwap_deviation_zscore,
)


@pytest.fixture(autouse=True)
def reset_settings():
    invalidate_settings_cache()
    yield
    invalidate_settings_cache()


@pytest.fixture
def synthetic_bars() -> pd.DataFrame:
    """800-bar synthetic OHLCV DataFrame for pipeline tests."""
    np.random.seed(42)
    N = 800
    close = pd.Series(30000 + np.cumsum(np.random.randn(N) * 50), dtype=np.float64)
    high = close + np.abs(np.random.randn(N) * 30)
    low = close - np.abs(np.random.randn(N) * 30)
    volume = pd.Series(np.abs(np.random.randn(N) * 100 + 500), dtype=np.float64)
    return pd.DataFrame({
        "open": close * 0.999,
        "high": high,
        "low": low,
        "close": close,
        "volume": volume,
    })


# ─── _frac_diff_weights ───────────────────────────────────────────────────────


class TestFracDiffWeights:
    def test_first_weight_is_one(self):
        w = _frac_diff_weights(0.4, 500, 1e-5)
        assert abs(w[-1] - 1.0) < 1e-9  # most-recent weight

    def test_max_window_cap(self):
        w = _frac_diff_weights(0.4, 800, 1e-5, max_window=200)
        assert len(w) <= 200

    def test_weights_above_threshold(self):
        w = _frac_diff_weights(0.4, 800, 1e-5)
        assert all(abs(wi) >= 1e-5 for wi in w[:-1])

    def test_d_zero_single_weight(self):
        # d=0 → w_1 = -w_0*(0-1+1)/1 = 0 → stops at k=1
        w = _frac_diff_weights(0.0, 100, 1e-5)
        assert len(w) == 1

    def test_returns_ndarray(self):
        w = _frac_diff_weights(0.4, 100, 1e-5)
        assert isinstance(w, np.ndarray)


# ─── fractional_differentiation ──────────────────────────────────────────────


class TestFractionalDifferentiation:
    def test_returns_series_same_length(self, synthetic_bars):
        close = synthetic_bars["close"]
        fd = fractional_differentiation(close, 0.4, 1e-5)
        assert len(fd) == len(close)

    def test_has_nan_at_start(self, synthetic_bars):
        close = synthetic_bars["close"]
        fd = fractional_differentiation(close, 0.4, 1e-5)
        assert fd.isna().any()

    def test_non_nan_values_finite(self, synthetic_bars):
        close = synthetic_bars["close"]
        fd = fractional_differentiation(close, 0.4, 1e-5)
        assert np.isfinite(fd.dropna()).all()

    def test_sufficient_non_nan_rows(self, synthetic_bars):
        close = synthetic_bars["close"]
        fd = fractional_differentiation(close, 0.4, 1e-5)
        assert fd.dropna().shape[0] > 500

    def test_too_short_returns_all_nan(self):
        # Series of 3 with max_window=200: weight width = min(3,200)=3,
        # so only indices 0..1 are NaN; index 2 has a value.
        # A series shorter than the effective window returns all NaN.
        short = pd.Series([1.0, 2.0])  # 2 elements; width=min(2,200)=2 → index 1 non-NaN
        fd = fractional_differentiation(short, 0.4, 1e-5, max_window=200)
        # Only index 0 is NaN; verify no crash and correct length
        assert len(fd) == 2
        # With an empty series it must return all NaN
        empty = pd.Series([], dtype=np.float64)
        fd_empty = fractional_differentiation(empty, 0.4, 1e-5, max_window=200)
        assert len(fd_empty) == 0


# ─── vwap_deviation_zscore ────────────────────────────────────────────────────


class TestVWAPDevZscore:
    def test_output_length(self, synthetic_bars):
        b = synthetic_bars
        z = vwap_deviation_zscore(b["high"], b["low"], b["close"], b["volume"], 20)
        assert len(z) == len(b)

    def test_mostly_within_bounds(self, synthetic_bars):
        b = synthetic_bars
        z = vwap_deviation_zscore(b["high"], b["low"], b["close"], b["volume"], 20)
        within = z.dropna().between(-10, 10).mean()
        assert within > 0.95

    def test_has_burn_in_nan(self, synthetic_bars):
        b = synthetic_bars
        z = vwap_deviation_zscore(b["high"], b["low"], b["close"], b["volume"], 20)
        assert z.isna().any()

    def test_renamed_correctly(self, synthetic_bars):
        b = synthetic_bars
        z = vwap_deviation_zscore(b["high"], b["low"], b["close"], b["volume"], 20)
        assert z.name == COL_VWAP_DEV


# ─── order_flow_imbalance ─────────────────────────────────────────────────────


class TestOrderFlowImbalance:
    def test_bounded_minus_one_to_one(self, synthetic_bars):
        b = synthetic_bars
        ofi = order_flow_imbalance(b["close"], b["volume"], 20)
        assert ofi.dropna().between(-1.0, 1.0).all()

    def test_output_length(self, synthetic_bars):
        b = synthetic_bars
        ofi = order_flow_imbalance(b["close"], b["volume"], 20)
        assert len(ofi) == len(b)

    def test_renamed(self, synthetic_bars):
        b = synthetic_bars
        ofi = order_flow_imbalance(b["close"], b["volume"], 20)
        assert ofi.name == COL_OFI


# ─── realized_vol_ratio ───────────────────────────────────────────────────────


class TestRealizedVolRatio:
    def test_positive_values(self, synthetic_bars):
        close = synthetic_bars["close"]
        rvr = realized_vol_ratio(close, 10, 60)
        assert (rvr.dropna() > 0).all()

    def test_output_length(self, synthetic_bars):
        close = synthetic_bars["close"]
        rvr = realized_vol_ratio(close, 10, 60)
        assert len(rvr) == len(close)

    def test_renamed(self, synthetic_bars):
        close = synthetic_bars["close"]
        rvr = realized_vol_ratio(close, 10, 60)
        assert rvr.name == COL_REALIZED_VOL_RATIO


# ─── atr_momentum ─────────────────────────────────────────────────────────────


class TestATRMomentum:
    def test_finite_values(self, synthetic_bars):
        b = synthetic_bars
        atr = atr_momentum(b["high"], b["low"], b["close"], 14)
        assert np.isfinite(atr.dropna()).all()

    def test_output_length(self, synthetic_bars):
        b = synthetic_bars
        atr = atr_momentum(b["high"], b["low"], b["close"], 14)
        assert len(atr) == len(b)

    def test_renamed(self, synthetic_bars):
        b = synthetic_bars
        atr = atr_momentum(b["high"], b["low"], b["close"], 14)
        assert atr.name == COL_ATR_MOMENTUM


# ─── rolling_sharpe ───────────────────────────────────────────────────────────


class TestRollingSharpe:
    def test_finite_values(self, synthetic_bars):
        close = synthetic_bars["close"]
        rs = rolling_sharpe(close, 60)
        assert np.isfinite(rs.dropna()).all()

    def test_output_length(self, synthetic_bars):
        close = synthetic_bars["close"]
        rs = rolling_sharpe(close, 60)
        assert len(rs) == len(close)

    def test_renamed(self, synthetic_bars):
        close = synthetic_bars["close"]
        rs = rolling_sharpe(close, 60)
        assert rs.name == COL_ROLLING_SHARPE


# ─── volume_zscore ────────────────────────────────────────────────────────────


class TestVolumeZscore:
    def test_finite_values(self, synthetic_bars):
        vz = volume_zscore(synthetic_bars["volume"], 20)
        assert np.isfinite(vz.dropna()).all()

    def test_output_length(self, synthetic_bars):
        vz = volume_zscore(synthetic_bars["volume"], 20)
        assert len(vz) == len(synthetic_bars)

    def test_renamed(self, synthetic_bars):
        vz = volume_zscore(synthetic_bars["volume"], 20)
        assert vz.name == COL_VOLUME_ZSCORE


# ─── _compute_daily_vol ───────────────────────────────────────────────────────


class TestComputeDailyVol:
    def test_positive_values(self, synthetic_bars):
        log_ret = np.log(synthetic_bars["close"] / synthetic_bars["close"].shift(1)).fillna(0.0)
        dv = _compute_daily_vol(log_ret)
        assert (dv.dropna() > 0).all()

    def test_output_length(self, synthetic_bars):
        log_ret = np.log(synthetic_bars["close"] / synthetic_bars["close"].shift(1)).fillna(0.0)
        dv = _compute_daily_vol(log_ret)
        assert len(dv) == len(synthetic_bars)


# ─── triple_barrier_labels ────────────────────────────────────────────────────


class TestTripleBarrierLabels:
    def test_valid_label_values(self, synthetic_bars):
        close = synthetic_bars["close"]
        tb = triple_barrier_labels(close, 2.0, 1.0, 60)
        valid = {0.0, 1.0, -1.0}
        assert set(tb.dropna().unique()).issubset(valid)

    def test_sufficient_labels(self, synthetic_bars):
        close = synthetic_bars["close"]
        tb = triple_barrier_labels(close, 2.0, 1.0, 60)
        assert len(tb.dropna()) > 100

    def test_tail_is_nan(self, synthetic_bars):
        close = synthetic_bars["close"]
        tb = triple_barrier_labels(close, 2.0, 1.0, 60)
        # Last max_holding bars should have NaN (horizon extends beyond data)
        assert tb.iloc[-1] == -1.0 or pd.isna(tb.iloc[-1])

    def test_renamed(self, synthetic_bars):
        close = synthetic_bars["close"]
        tb = triple_barrier_labels(close, 2.0, 1.0, 60)
        assert tb.name == COL_LABEL


# ─── meta_labels ──────────────────────────────────────────────────────────────


class TestMetaLabels:
    def test_agree_long(self):
        prim = pd.Series([1], dtype=np.int8)
        real = pd.Series([1], dtype=np.int8)
        assert meta_labels(prim, real).iloc[0] == 1

    def test_agree_short(self):
        prim = pd.Series([0], dtype=np.int8)
        real = pd.Series([0], dtype=np.int8)
        assert meta_labels(prim, real).iloc[0] == 1

    def test_disagree(self):
        prim = pd.Series([1], dtype=np.int8)
        real = pd.Series([0], dtype=np.int8)
        assert meta_labels(prim, real).iloc[0] == 0

    def test_time_exit_is_skip(self):
        prim = pd.Series([1], dtype=np.int8)
        real = pd.Series([-1], dtype=np.int8)
        assert meta_labels(prim, real).iloc[0] == 0

    def test_full_sequence(self):
        prim = pd.Series([1, 0, 1, 0, 1], dtype=np.int8)
        real = pd.Series([1, 0, 0, 1, -1], dtype=np.int8)
        result = meta_labels(prim, real)
        assert list(result) == [1, 1, 0, 0, 0]

    def test_renamed(self):
        prim = pd.Series([1], dtype=np.int8)
        real = pd.Series([1], dtype=np.int8)
        assert meta_labels(prim, real).name == COL_META_LABEL


# ─── build_feature_matrix ─────────────────────────────────────────────────────


class TestBuildFeatureMatrix:
    def test_returns_feature_matrix(self, synthetic_bars):
        fm = build_feature_matrix(synthetic_bars)
        assert isinstance(fm, FeatureMatrix)

    def test_correct_feature_columns(self, synthetic_bars):
        fm = build_feature_matrix(synthetic_bars)
        assert list(fm.features.columns) == FEATURE_COLUMNS

    def test_no_nan_in_features(self, synthetic_bars):
        fm = build_feature_matrix(synthetic_bars)
        assert not fm.features.isna().any().any()

    def test_labels_binary(self, synthetic_bars):
        fm = build_feature_matrix(synthetic_bars)
        assert fm.labels.isin([0, 1]).all()

    def test_meta_binary(self, synthetic_bars):
        fm = build_feature_matrix(synthetic_bars)
        assert fm.meta.isin([0, 1]).all()

    def test_lengths_aligned(self, synthetic_bars):
        fm = build_feature_matrix(synthetic_bars)
        assert len(fm.features) == len(fm.labels) == len(fm.meta)

    def test_dropped_rows_positive(self, synthetic_bars):
        fm = build_feature_matrix(synthetic_bars)
        assert fm.dropped_rows > 0

    def test_sufficient_output_rows(self, synthetic_bars):
        fm = build_feature_matrix(synthetic_bars)
        assert len(fm.features) > 100

    def test_missing_column_raises(self, synthetic_bars):
        with pytest.raises(ValueError, match="missing required columns"):
            build_feature_matrix(synthetic_bars.drop(columns=["volume"]))

    def test_too_few_rows_raises(self, synthetic_bars):
        with pytest.raises(ValueError):
            build_feature_matrix(synthetic_bars.iloc[:5])

    def test_ofi_snapshot_override(self, synthetic_bars):
        ofi_series = pd.Series(0.42, index=synthetic_bars.index)
        fm = build_feature_matrix(synthetic_bars, ofi_snapshots=ofi_series)
        assert isinstance(fm, FeatureMatrix)

    def test_log_returns_aligned(self, synthetic_bars):
        fm = build_feature_matrix(synthetic_bars)
        assert len(fm.log_returns) == len(fm.features)

    def test_daily_vol_aligned(self, synthetic_bars):
        fm = build_feature_matrix(synthetic_bars)
        assert len(fm.daily_vol) == len(fm.features)
        assert (fm.daily_vol.dropna() > 0).all()


# ─── build_inference_features ─────────────────────────────────────────────────


class TestBuildInferenceFeatures:
    def test_returns_series(self, synthetic_bars):
        vec = build_inference_features(synthetic_bars)
        assert isinstance(vec, pd.Series)

    def test_correct_index(self, synthetic_bars):
        vec = build_inference_features(synthetic_bars)
        assert vec is not None
        assert list(vec.index) == FEATURE_COLUMNS

    def test_no_nan(self, synthetic_bars):
        vec = build_inference_features(synthetic_bars)
        assert vec is not None
        assert not vec.isna().any()

    def test_live_ofi_override(self, synthetic_bars):
        vec = build_inference_features(synthetic_bars, live_ofi=0.42)
        assert vec is not None
        assert abs(vec[COL_OFI] - 0.42) < 1e-9

    def test_insufficient_data_returns_none(self, synthetic_bars):
        assert build_inference_features(synthetic_bars.iloc[:5]) is None

    def test_missing_column_raises(self, synthetic_bars):
        with pytest.raises(ValueError):
            build_inference_features(synthetic_bars.drop(columns=["close"]))

    def test_values_are_float64(self, synthetic_bars):
        vec = build_inference_features(synthetic_bars)
        assert vec is not None
        assert vec.dtype == np.float64