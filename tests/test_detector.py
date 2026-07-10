"""
Tests for src/regime/detector.py — GaussianHMM 3-state regime detector
(Hamilton 1989) and the GAP-002 posterior entropy gate.

Coverage priorities:
  1. RegimePrediction entropy/confidence/position_scalar math (pure,
     no fitted model needed) — the GAP-002 deliverable.
  2. fit() / predict_sequence() / predict_proba_sequence() / predict_current()
     end-to-end on synthetic 3-regime data with a real GaussianHMM.
  3. save()/load() round-trip.
  4. Error paths: unfitted access, malformed input, non-convergence fallback.
"""

import math
import tempfile

import numpy as np
import pandas as pd
import pytest

from src.config import HMMSettings, invalidate_settings_cache
from src.regime.detector import (
    HMM_FEATURE_COLS,
    REGIME_RANGING,
    REGIME_TRENDING,
    REGIME_VOLATILE,
    RegimeDetector,
    RegimePrediction,
)


@pytest.fixture(autouse=True)
def reset_settings():
    invalidate_settings_cache()
    yield
    invalidate_settings_cache()


def make_synthetic_features(n_per_regime: int = 80, seed: int = 7) -> pd.DataFrame:
    """
    Three well-separated synthetic regimes concatenated in sequence, so a
    GaussianHMM with n_components=3 has a realistic chance to converge and
    separate states cleanly (this is a unit test, not a calibration study —
    separation just needs to be good enough for fit() to converge).
    """
    rng = np.random.default_rng(seed)

    def block(vol_ratio_mu, atr_mom_mu, sharpe_mu, n):
        return pd.DataFrame(
            {
                "frac_diff": rng.normal(0.0, 0.01, n),
                "realized_vol_ratio": rng.normal(vol_ratio_mu, 0.05, n),
                "atr_momentum": rng.normal(atr_mom_mu, 0.05, n),
                "rolling_sharpe": rng.normal(sharpe_mu, 0.1, n),
                "volume_zscore": rng.normal(0.0, 0.3, n),
            }
        )

    ranging = block(0.8, 0.0, 0.1, n_per_regime)
    trending = block(1.0, 0.6, 0.9, n_per_regime)
    volatile = block(2.5, -0.3, -0.5, n_per_regime)

    df = pd.concat([ranging, trending, volatile], ignore_index=True)
    df.index = pd.RangeIndex(len(df))
    return df


@pytest.fixture(scope="module")
def fitted_detector() -> RegimeDetector:
    features = make_synthetic_features()
    cfg = HMMSettings(n_iter=200, random_state=42)
    detector = RegimeDetector(symbol="BTC/USDT", timeframe="1h", cfg=cfg)
    detector.fit(features)
    return detector


@pytest.fixture(scope="module")
def synthetic_features() -> pd.DataFrame:
    return make_synthetic_features()


# ─── RegimePrediction — entropy / confidence / position_scalar (GAP-002) ──────


class TestRegimePredictionEntropyMath:
    def test_entropy_zero_for_certain_prediction(self):
        pred = RegimePrediction(
            state=REGIME_TRENDING,
            prob_ranging=0.0,
            prob_trending=1.0,
            prob_volatile=0.0,
            entropy=0.0,
        )
        assert pred.entropy == 0.0
        assert pred.confidence == 1.0

    def test_confidence_is_one_minus_entropy(self):
        pred = RegimePrediction(
            state=REGIME_RANGING,
            prob_ranging=0.5,
            prob_trending=0.3,
            prob_volatile=0.2,
            entropy=0.85,
        )
        assert abs(pred.confidence - 0.15) < 1e-9

    def test_dominant_prob_picks_max(self):
        pred = RegimePrediction(
            state=REGIME_TRENDING,
            prob_ranging=0.15,
            prob_trending=0.72,
            prob_volatile=0.13,
        )
        assert abs(pred.dominant_prob - 0.72) < 1e-9

    def test_is_volatile_property(self):
        pred = RegimePrediction(
            state=REGIME_VOLATILE, prob_ranging=0.1, prob_trending=0.1, prob_volatile=0.8
        )
        assert pred.is_volatile is True
        pred2 = RegimePrediction(
            state=REGIME_TRENDING, prob_ranging=0.1, prob_trending=0.8, prob_volatile=0.1
        )
        assert pred2.is_volatile is False

    def test_as_dict_includes_entropy_and_confidence(self):
        pred = RegimePrediction(
            state=REGIME_TRENDING,
            prob_ranging=0.15,
            prob_trending=0.72,
            prob_volatile=0.13,
            entropy=0.7157,
        )
        d = pred.as_dict()
        assert d["entropy"] == round(0.7157, 6)
        assert d["confidence"] == round(1.0 - 0.7157, 6)
        assert "is_volatile" in d


class TestPositionScalar:
    def setup_method(self):
        self.cfg = HMMSettings(entropy_threshold=0.5, entropy_scalar_floor=0.5)

    def test_full_scalar_below_threshold(self):
        pred = RegimePrediction(
            state=REGIME_TRENDING,
            prob_ranging=0.05,
            prob_trending=0.9,
            prob_volatile=0.05,
            entropy=0.3,
        )
        assert pred.position_scalar(self.cfg) == 1.0

    def test_scalar_at_exact_threshold_is_one(self):
        pred = RegimePrediction(
            state=REGIME_TRENDING,
            prob_ranging=0.1,
            prob_trending=0.8,
            prob_volatile=0.1,
            entropy=0.5,
        )
        assert pred.position_scalar(self.cfg) == 1.0

    def test_scalar_at_max_entropy_equals_floor(self):
        pred = RegimePrediction(
            state=REGIME_TRENDING,
            prob_ranging=0.33,
            prob_trending=0.34,
            prob_volatile=0.33,
            entropy=1.0,
        )
        assert abs(pred.position_scalar(self.cfg) - 0.5) < 1e-9

    def test_scalar_continuous_no_discontinuity_at_threshold(self):
        # Linear ramp: an infinitesimal entropy change near the threshold
        # must NOT cause a large jump in the scalar (this was the whole
        # point of choosing continuous over a hard step function).
        just_below = RegimePrediction(
            state=1, prob_ranging=0.1, prob_trending=0.8, prob_volatile=0.1, entropy=0.4999
        )
        just_above = RegimePrediction(
            state=1, prob_ranging=0.1, prob_trending=0.8, prob_volatile=0.1, entropy=0.5001
        )
        scalar_below = just_below.position_scalar(self.cfg)
        scalar_above = just_above.position_scalar(self.cfg)
        assert abs(scalar_below - scalar_above) < 0.001

    def test_scalar_halfway_between_threshold_and_max(self):
        # entropy = 0.75 is halfway between threshold(0.5) and max(1.0)
        pred = RegimePrediction(
            state=1, prob_ranging=0.2, prob_trending=0.6, prob_volatile=0.2, entropy=0.75
        )
        scalar = pred.position_scalar(self.cfg)
        assert abs(scalar - 0.75) < 1e-9  # 1.0 - 0.5*(1.0-0.5) = 0.75

    def test_scalar_monotonically_decreasing_with_entropy(self):
        entropies = [0.5, 0.6, 0.7, 0.8, 0.9, 1.0]
        scalars = [
            RegimePrediction(
                state=1, prob_ranging=0.3, prob_trending=0.4, prob_volatile=0.3, entropy=e
            ).position_scalar(self.cfg)
            for e in entropies
        ]
        for i in range(len(scalars) - 1):
            assert scalars[i] >= scalars[i + 1] - 1e-9

    def test_scalar_never_exceeds_one_or_drops_below_floor(self):
        for e in [0.0, 0.25, 0.5, 0.75, 1.0]:
            pred = RegimePrediction(
                state=1, prob_ranging=0.3, prob_trending=0.4, prob_volatile=0.3, entropy=e
            )
            scalar = pred.position_scalar(self.cfg)
            assert 0.5 - 1e-9 <= scalar <= 1.0 + 1e-9

    def test_uses_global_settings_when_cfg_not_passed(self):
        # No cfg arg -> falls back to get_settings().hmm; just confirm no crash
        # and a sane bounded result using defaults (threshold=0.5, floor=0.5).
        pred = RegimePrediction(
            state=1, prob_ranging=0.3, prob_trending=0.4, prob_volatile=0.3, entropy=0.9
        )
        scalar = pred.position_scalar()
        assert 0.0 <= scalar <= 1.0

    def test_degenerate_threshold_equals_one_returns_floor_above_threshold(self):
        cfg = HMMSettings(entropy_threshold=1.0, entropy_scalar_floor=0.4)
        pred = RegimePrediction(
            state=1, prob_ranging=0.3, prob_trending=0.4, prob_volatile=0.3, entropy=1.0
        )
        # entropy <= threshold (1.0 <= 1.0) -> full scalar, span-zero branch not hit
        assert pred.position_scalar(cfg) == 1.0


# ─── RegimeDetector — fit / predict / persistence (integration-style) ─────────


class TestRegimeDetectorFit:
    def test_fit_returns_self_and_sets_fitted(self, synthetic_features):
        detector = RegimeDetector(symbol="ETH/USDT", timeframe="1h")
        result = detector.fit(synthetic_features)
        assert result is detector
        assert detector.is_fitted() is True

    def test_fit_rejects_missing_columns(self):
        detector = RegimeDetector(symbol="BTC/USDT", timeframe="1h")
        bad_df = pd.DataFrame({"frac_diff": [0.1] * 100})
        with pytest.raises(ValueError, match="missing observation columns"):
            detector.fit(bad_df)

    def test_fit_rejects_nan_values(self, synthetic_features):
        detector = RegimeDetector(symbol="BTC/USDT", timeframe="1h")
        df = synthetic_features.copy()
        df.loc[0, "frac_diff"] = float("nan")
        with pytest.raises(ValueError, match="NaN"):
            detector.fit(df)

    def test_fit_rejects_insufficient_rows(self):
        detector = RegimeDetector(symbol="BTC/USDT", timeframe="1h")
        tiny_df = pd.DataFrame({col: [0.1, 0.2, 0.3] for col in HMM_FEATURE_COLS})
        with pytest.raises(ValueError, match="need at least"):
            detector.fit(tiny_df)

    def test_fit_rejects_non_three_components(self, synthetic_features):
        cfg = HMMSettings(n_components=4)
        detector = RegimeDetector(symbol="BTC/USDT", timeframe="1h", cfg=cfg)
        with pytest.raises(ValueError, match="n_components=3"):
            detector.fit(synthetic_features)

    def test_refit_on_already_fitted_instance_raises(self, fitted_detector, synthetic_features):
        with pytest.raises(RuntimeError, match="already-fitted"):
            fitted_detector.fit(synthetic_features)

    def test_n_init_zero_raises_value_error(self, synthetic_features):
        cfg = HMMSettings(n_iter=50)
        detector = RegimeDetector(symbol="BTC/USDT", timeframe="1h", cfg=cfg)
        object.__setattr__(cfg, "n_init", 0) if hasattr(cfg, "__dict__") else None
        # n_init isn't a declared HMMSettings field; simulate via getattr override
        # by monkeypatching the cfg instance directly.
        try:
            cfg.n_init = 0  # type: ignore[attr-defined]
        except Exception:
            pytest.skip("HMMSettings does not allow dynamic attribute assignment")
        with pytest.raises(ValueError, match="HMM_N_INIT"):
            detector.fit(synthetic_features)


class TestRegimeDetectorPredict:
    def test_predict_sequence_returns_canonical_labels(self, fitted_detector, synthetic_features):
        states = fitted_detector.predict_sequence(synthetic_features)
        assert len(states) == len(synthetic_features)
        assert set(states.unique()).issubset({REGIME_RANGING, REGIME_TRENDING, REGIME_VOLATILE})

    def test_predict_proba_sequence_rows_sum_to_one(self, fitted_detector, synthetic_features):
        probs = fitted_detector.predict_proba_sequence(synthetic_features)
        assert list(probs.columns) == ["prob_ranging", "prob_trending", "prob_volatile"]
        row_sums = probs.sum(axis=1)
        assert np.allclose(row_sums, 1.0, atol=1e-6)

    def test_predict_current_returns_regime_prediction_with_entropy(
        self, fitted_detector, synthetic_features
    ):
        pred = fitted_detector.predict_current(synthetic_features, lookback=100)
        assert isinstance(pred, RegimePrediction)
        assert 0.0 <= pred.entropy <= 1.0 + 1e-9
        assert math.isclose(
            pred.prob_ranging + pred.prob_trending + pred.prob_volatile, 1.0, abs_tol=1e-6
        )

    def test_predict_current_entropy_matches_manual_shannon_calc(
        self, fitted_detector, synthetic_features
    ):
        pred = fitted_detector.predict_current(synthetic_features, lookback=100)
        probs = [pred.prob_ranging, pred.prob_trending, pred.prob_volatile]
        eps = 1e-12
        clipped = [max(p, eps) for p in probs]
        raw_entropy = -sum(p * math.log(p) for p in clipped)
        expected = raw_entropy / math.log(3)
        assert abs(pred.entropy - max(0.0, min(1.0, expected))) < 1e-4

    def test_predict_on_unfitted_detector_raises(self, synthetic_features):
        detector = RegimeDetector(symbol="BTC/USDT", timeframe="1h")
        with pytest.raises(RuntimeError, match="not fitted"):
            detector.predict_sequence(synthetic_features)
        with pytest.raises(RuntimeError, match="not fitted"):
            detector.predict_current(synthetic_features)

    def test_predict_rejects_nan_in_observation(self, fitted_detector, synthetic_features):
        df = synthetic_features.copy()
        df.loc[0, "frac_diff"] = float("nan")
        with pytest.raises(ValueError, match="NaN"):
            fitted_detector.predict_sequence(df)

    def test_non_convergent_model_defaults_to_volatile_with_zero_entropy(self, synthetic_features):
        detector = RegimeDetector(symbol="BTC/USDT", timeframe="1h")
        detector.fit(synthetic_features)
        # Force the non-convergence fail-safe path directly rather than
        # trying to engineer real non-convergence (flaky / slow).
        detector._convergence_failed = True
        pred = detector.predict_current(synthetic_features)
        assert pred.state == REGIME_VOLATILE
        assert pred.prob_volatile == 1.0
        assert pred.entropy == 0.0
        assert pred.is_volatile is True


class TestRegimeDetectorPersistence:
    def test_save_and_load_round_trip(self, fitted_detector, synthetic_features):
        with tempfile.TemporaryDirectory() as tmpdir:
            saved_path = fitted_detector.save(tmpdir)
            assert saved_path.exists()

            loaded = RegimeDetector.load(tmpdir, symbol="BTC/USDT", timeframe="1h")
            assert loaded.is_fitted() is True
            assert loaded.state_map() == fitted_detector.state_map()

            original_states = fitted_detector.predict_sequence(synthetic_features)
            loaded_states = loaded.predict_sequence(synthetic_features)
            assert (original_states == loaded_states).all()

    def test_load_missing_file_raises_file_not_found(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with pytest.raises(FileNotFoundError, match="No saved HMM model"):
                RegimeDetector.load(tmpdir, symbol="DOES/NOTEXIST", timeframe="1h")

    def test_save_on_unfitted_detector_raises(self):
        detector = RegimeDetector(symbol="BTC/USDT", timeframe="1h")
        with tempfile.TemporaryDirectory() as tmpdir:
            with pytest.raises(RuntimeError, match="not fitted"):
                detector.save(tmpdir)


class TestRegimeStatistics:
    def test_regime_statistics_returns_expected_shape(self, fitted_detector, synthetic_features):
        stats = fitted_detector.regime_statistics(synthetic_features)
        assert set(stats.index) == {"ranging", "trending", "volatile"}
        assert set(stats.columns) == {
            "count",
            "pct",
            "mean_vol_ratio",
            "mean_atr_momentum",
            "mean_rolling_sharpe",
        }
        assert stats["count"].sum() == len(synthetic_features)
