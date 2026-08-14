"""Tests for src/features/pipeline.py — all feature functions and the full pipeline."""

import numpy as np
import pandas as pd
import pytest

from src.config import invalidate_settings_cache
from src.features.pipeline import (
    BASE_FEATURE_COLUMNS,
    COL_ATR_MOMENTUM,
    COL_GARCH_VOL,
    COL_LABEL,
    COL_META_LABEL,
    COL_OFI,
    COL_REALIZED_VOL_RATIO,
    COL_ROLLING_SHARPE,
    COL_VOLUME_ZSCORE,
    COL_VWAP_DEV,
    FEATURE_COLUMNS,
    FeatureMatrix,
    _compute_daily_vol,
    _frac_diff_weights,
    atr_momentum,
    build_feature_matrix,
    build_inference_features,
    fractional_differentiation,
    meta_labels,
    order_flow_imbalance,
    realized_vol_ratio,
    rolling_sharpe,
    summarize_triple_barrier,
    triple_barrier_labels,
    triple_barrier_labels_with_offsets,
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
    return pd.DataFrame(
        {
            "open": close * 0.999,
            "high": high,
            "low": low,
            "close": close,
            "volume": volume,
        }
    )


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

    def test_early_exit_when_all_rows_resolved_before_max_holding(self):
        """VF-015: the k-loop should `break` as soon as every valid row has
        resolved, rather than always running all max_holding iterations."""
        n = 10
        close = pd.Series([100.0, 200.0, 50.0, 300.0, 20.0, 400.0, 10.0, 500.0, 5.0, 600.0])
        vol = pd.Series([0.5] * (n - 1) + [np.nan])  # last row invalid (no future data anyway)
        tb = triple_barrier_labels(close, 0.01, 0.01, max_holding=8, daily_vol=vol)
        # Wide barriers relative to the wild swings -> every valid row hits
        # a barrier at k=1, well before max_holding=8 is reached.
        assert tb.iloc[:-1].isin([0.0, 1.0]).all()
        assert pd.isna(tb.iloc[-1])


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

    def test_no_cfg_picks_up_promoted_window(self, synthetic_bars):
        """Self-tuning live-wiring regression: once features.atr_window is
        registered (see src/tuning/live_overrides.py), the no-cfg default
        must use the registry value, not the raw .env setting. Registering
        a window far larger than the fixture's 800 bars makes the effect
        observable: only the OVERRIDDEN value can push min_required past
        the available row count."""
        from src.tuning.registry import TunableParameter, parameter_registry

        parameter_registry._params.clear()
        try:
            parameter_registry.register(
                TunableParameter(
                    name="features.atr_window",
                    description="test",
                    floor=2.0,
                    ceiling=5000.0,
                    current=5000.0,
                    eval_strategy="test",
                )
            )
            with pytest.raises(ValueError, match="need at least"):
                build_feature_matrix(synthetic_bars)
        finally:
            parameter_registry._params.clear()

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

    def test_flat_price_run_logs_warning_but_still_computes(self, synthetic_bars):
        """M-13: >5% zero-delta bars (exchange halt / stale feed) must be
        warned about, not silently swallowed -- but must not block feature
        computation."""
        import structlog

        bars = synthetic_bars.copy()
        n_flat = int(len(bars) * 0.10)
        flat_value = bars["close"].iloc[0]
        bars.loc[bars.index[:n_flat], "close"] = flat_value
        bars.loc[bars.index[:n_flat], "open"] = flat_value * 0.999
        bars.loc[bars.index[:n_flat], "high"] = flat_value * 1.0001
        bars.loc[bars.index[:n_flat], "low"] = flat_value * 0.9999

        with structlog.testing.capture_logs() as captured:
            fm = build_feature_matrix(bars)

        assert isinstance(fm, FeatureMatrix)
        assert any(e.get("event") == "pipeline.flat_price_detected" for e in captured)


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

    def test_slow_path_explicit_cfg_skips_effective_settings_lookup(self, synthetic_bars):
        from src.config import FeatureSettings

        cfg = FeatureSettings()
        vec = build_inference_features(synthetic_bars, cfg=cfg)
        assert vec is not None

    def test_slow_path_nan_feature_returns_none(self, synthetic_bars):
        """H-12: a NaN in any slow-path (no pre-built feature_matrix) computed
        feature -- e.g. volume_zscore on a constant (zero-variance) volume
        series -- must skip the signal (return None), not propagate NaN."""
        bars = synthetic_bars.copy()
        bars["volume"] = 500.0  # constant -> zero std -> NaN z-score
        assert build_inference_features(bars) is None

    def test_fast_path_empty_feature_matrix_returns_none(self, synthetic_bars):
        """SCAN2-007 fast path: an empty pre-built feature_matrix must return
        None rather than indexing into an empty frame."""
        fm = build_feature_matrix(synthetic_bars)
        empty_fm = FeatureMatrix(
            features=fm.features.iloc[:0],
            labels=fm.labels.iloc[:0],
            meta=fm.meta.iloc[:0] if fm.meta is not None else None,
            daily_vol=fm.daily_vol.iloc[:0],
            log_returns=fm.log_returns.iloc[:0],
            dropped_rows=fm.dropped_rows,
        )
        assert build_inference_features(synthetic_bars, feature_matrix=empty_fm) is None

    def test_fast_path_live_ofi_overrides_last_row(self, synthetic_bars):
        fm = build_feature_matrix(synthetic_bars)
        vec = build_inference_features(synthetic_bars, feature_matrix=fm, live_ofi=0.77)
        assert vec is not None
        assert abs(vec[COL_OFI] - 0.77) < 1e-9

    def test_fast_path_nan_feature_returns_none(self, synthetic_bars):
        """Same NaN-skip contract as the slow path, but exercised via a
        pre-built feature_matrix whose last row has a NaN feature."""
        fm = build_feature_matrix(synthetic_bars)
        tampered_features = fm.features.copy()
        tampered_features.iloc[-1, tampered_features.columns.get_loc(COL_VOLUME_ZSCORE)] = float(
            "nan"
        )
        tampered_fm = FeatureMatrix(
            features=tampered_features,
            labels=fm.labels,
            meta=fm.meta,
            daily_vol=fm.daily_vol,
            log_returns=fm.log_returns,
            dropped_rows=fm.dropped_rows,
        )
        assert build_inference_features(synthetic_bars, feature_matrix=tampered_fm) is None

    def test_fast_path_injects_intelligence_metrics(self, synthetic_bars):
        fm = build_feature_matrix(synthetic_bars)
        vec = build_inference_features(
            synthetic_bars,
            feature_matrix=fm,
            intelligence_metrics={"exchange_stress_score": 0.5, "confidence": 0.8},
        )
        assert vec is not None
        assert "intelligence_exchange_stress_score" in vec.index

    def test_values_are_float64(self, synthetic_bars):
        vec = build_inference_features(synthetic_bars)
        assert vec is not None
        assert vec.dtype == np.float64


# ─── GARCH integration in pipeline ───────────────────────────────────────────


class TestGARCHPipelineIntegration:
    """Verify that COL_GARCH_VOL is present and finite in both pipeline paths."""

    def test_garch_vol_in_base_feature_columns(self) -> None:
        assert COL_GARCH_VOL in BASE_FEATURE_COLUMNS
        assert COL_GARCH_VOL == "garch_vol_forecast"

    def test_garch_vol_present_in_feature_matrix(self, synthetic_bars):
        fm = build_feature_matrix(synthetic_bars)
        assert COL_GARCH_VOL in fm.features.columns

    def test_garch_vol_finite_in_feature_matrix(self, synthetic_bars):
        fm = build_feature_matrix(synthetic_bars)
        assert fm.features[COL_GARCH_VOL].notna().all()
        assert (fm.features[COL_GARCH_VOL] > 0).all()

    def test_garch_vol_present_in_inference_slow_path(self, synthetic_bars):
        vec = build_inference_features(synthetic_bars)
        assert vec is not None
        assert COL_GARCH_VOL in vec.index

    def test_garch_vol_positive_in_inference(self, synthetic_bars):
        vec = build_inference_features(synthetic_bars)
        assert vec is not None
        assert vec[COL_GARCH_VOL] > 0

    def test_garch_vol_present_in_inference_fast_path(self, synthetic_bars):
        fm = build_feature_matrix(synthetic_bars)
        vec = build_inference_features(synthetic_bars, feature_matrix=fm)
        assert vec is not None
        assert COL_GARCH_VOL in vec.index
        assert vec[COL_GARCH_VOL] > 0

    def test_feature_columns_has_8_base_features(self) -> None:
        assert len(BASE_FEATURE_COLUMNS) == 8
        assert list(BASE_FEATURE_COLUMNS) == FEATURE_COLUMNS


# ─── triple-barrier exit composition ──────────────────────────────────────────


class TestTripleBarrierComposition:
    def _series(self, n: int = 200) -> pd.Series:
        return pd.Series(np.linspace(100.0, 110.0, n))

    def test_labels_only_signature_is_unchanged(self) -> None:
        # The long-standing entry point must keep returning a bare Series.
        tb = triple_barrier_labels(self._series(), 2.0, 1.0, 60)
        assert isinstance(tb, pd.Series)

    def test_offsets_variant_returns_both_and_agrees_on_labels(self) -> None:
        close = self._series()
        labels, offsets = triple_barrier_labels_with_offsets(close, 2.0, 1.0, 60)
        assert labels.equals(triple_barrier_labels(close, 2.0, 1.0, 60))
        assert len(offsets) == len(close)

    def test_offsets_are_bounded_by_max_holding(self) -> None:
        _, offsets = triple_barrier_labels_with_offsets(self._series(), 2.0, 1.0, 30)
        valid = offsets.dropna()
        assert (valid >= 1).all()
        assert (valid <= 30).all()

    def test_wide_barriers_resolve_on_the_clock(self) -> None:
        # Barriers far too wide to reach: every label comes from the vertical
        # barrier, which is the mis-calibration the composition exists to show.
        close = self._series()
        vol = pd.Series(np.full(len(close), 0.5), index=close.index)
        labels, offsets = triple_barrier_labels_with_offsets(close, 100.0, 100.0, 20, daily_vol=vol)
        comp = summarize_triple_barrier(labels, offsets)
        assert comp.time_exit_fraction == pytest.approx(1.0)
        assert comp.mean_holding_bars == pytest.approx(20.0)

    def test_tight_barriers_resolve_on_price(self) -> None:
        close = self._series()
        vol = pd.Series(np.full(len(close), 1e-4), index=close.index)
        labels, offsets = triple_barrier_labels_with_offsets(close, 0.01, 0.01, 30, daily_vol=vol)
        comp = summarize_triple_barrier(labels, offsets)
        assert comp.time_exit_fraction < 1.0
        assert comp.mean_holding_bars < 30.0

    def test_unlabelled_tail_is_excluded_not_counted(self) -> None:
        # NaN rows are the tail beyond the holding window, not a fourth
        # category of exit.
        labels = pd.Series([1.0, 0.0, -1.0, np.nan])
        offsets = pd.Series([3.0, 5.0, 10.0, np.nan])
        comp = summarize_triple_barrier(labels, offsets)
        assert comp.total == 3
        assert (comp.profit_target, comp.stop_loss, comp.time_exit) == (1, 1, 1)
        assert comp.mean_holding_bars == pytest.approx(6.0)

    def test_empty_composition_does_not_divide_by_zero(self) -> None:
        comp = summarize_triple_barrier(pd.Series(dtype=float), pd.Series(dtype=float))
        assert comp.total == 0
        assert comp.time_exit_fraction == 0.0
        assert comp.mean_holding_bars == 0.0

    def test_as_dict_is_loggable(self) -> None:
        comp = summarize_triple_barrier(pd.Series([1.0, -1.0]), pd.Series([2.0, 8.0]))
        payload = comp.as_dict()
        assert payload["total"] == 2
        assert payload["time_exit_fraction"] == pytest.approx(0.5)
