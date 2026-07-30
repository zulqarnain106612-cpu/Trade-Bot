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
        # garch_vol_forecast: conditional vol proxy; volatile regime has higher value
        garch_mu = vol_ratio_mu * 0.02  # proxy: scales with realized_vol_ratio
        return pd.DataFrame(
            {
                "frac_diff": rng.normal(0.0, 0.01, n),
                "realized_vol_ratio": rng.normal(vol_ratio_mu, 0.05, n),
                "atr_momentum": rng.normal(atr_mom_mu, 0.05, n),
                "rolling_sharpe": rng.normal(sharpe_mu, 0.1, n),
                "volume_zscore": rng.normal(0.0, 0.3, n),
                "garch_vol_forecast": rng.normal(garch_mu, 0.005, n).clip(0.001),
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

    def test_zero_span_degenerate_threshold_returns_floor(self):
        """threshold == 1.0 -> span = 1.0 - threshold = 0.0; the entropy > 1.0
        edge case (not reachable via a normal [0,1]-normalized entropy but
        not runtime-validated on the dataclass either) must return floor
        rather than divide by zero."""
        cfg = HMMSettings(entropy_threshold=1.0, entropy_scalar_floor=0.5)
        pred = RegimePrediction(
            state=1, prob_ranging=0.3, prob_trending=0.4, prob_volatile=0.3, entropy=1.01
        )
        assert pred.position_scalar(cfg) == 0.5

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

    def test_no_cfg_picks_up_self_tuning_promoted_threshold(self):
        """Self-tuning live-wiring regression: once hmm.entropy_threshold is
        registered (see src/tuning/live_overrides.py), the no-cfg default
        path must reflect its registry value, not the raw .env setting --
        otherwise a live promotion has zero effect on position sizing."""
        from src.tuning.registry import TunableParameter, parameter_registry

        parameter_registry._params.clear()
        try:
            parameter_registry.register(
                TunableParameter(
                    name="hmm.entropy_threshold",
                    description="test",
                    floor=0.0,
                    ceiling=1.0,
                    current=0.2,  # far below the entropy=0.9 sample below
                    eval_strategy="test",
                )
            )
            pred = RegimePrediction(
                state=1, prob_ranging=0.3, prob_trending=0.4, prob_volatile=0.3, entropy=0.9
            )
            scalar = pred.position_scalar()  # no cfg -> must pick up the override
            # entropy (0.9) is well above the overridden threshold (0.2), so the
            # scalar must have decayed below 1.0 -- the raw .env default
            # (threshold=0.5) would give a different (higher) result.
            assert scalar < 1.0
        finally:
            parameter_registry._params.clear()


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
        """n_init isn't a declared HMMSettings field (pydantic forbids
        assigning undeclared attributes to a real HMMSettings instance) --
        detector.fit() reads it via getattr(cfg, "n_init", 5), so a plain
        duck-typed stand-in exposing the same attributes fit() actually
        touches is the correct way to exercise this branch."""
        from types import SimpleNamespace

        cfg = SimpleNamespace(
            n_components=3,
            covariance_type="full",
            n_iter=50,
            tol=1e-4,
            random_state=42,
            entropy_threshold=0.5,
            entropy_scalar_floor=0.5,
            n_init=0,
        )
        detector = RegimeDetector(symbol="BTC/USDT", timeframe="1h", cfg=cfg)  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="HMM_N_INIT"):
            detector.fit(synthetic_features)

    def test_score_exception_skips_candidate(self, synthetic_features, monkeypatch):
        """A candidate whose .score() raises must be skipped (not crash the
        whole multi-init loop) -- the remaining candidates still compete."""
        from hmmlearn.hmm import GaussianHMM

        cfg = HMMSettings(n_iter=20, n_components=3)
        detector = RegimeDetector(symbol="BTC/USDT", timeframe="1h", cfg=cfg)

        call_count = 0
        real_score = GaussianHMM.score

        def _flaky_score(self, X, lengths=None):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("scoring blew up")
            return real_score(self, X, lengths=lengths)

        monkeypatch.setattr(GaussianHMM, "score", _flaky_score)
        detector.fit(synthetic_features)  # must not raise despite the first candidate failing
        assert detector.is_fitted()
        assert call_count >= 2

    def test_all_candidates_fail_scoring_raises(self, synthetic_features, monkeypatch):
        from hmmlearn.hmm import GaussianHMM

        cfg = HMMSettings(n_iter=20, n_components=3)
        detector = RegimeDetector(symbol="BTC/USDT", timeframe="1h", cfg=cfg)

        def _always_fails(self, X, lengths=None):
            raise RuntimeError("scoring blew up")

        monkeypatch.setattr(GaussianHMM, "score", _always_fails)
        with pytest.raises(RuntimeError, match="all candidate fits failed"):
            detector.fit(synthetic_features)

    def test_non_convergent_fit_sets_convergence_failed_flag(self, synthetic_features, monkeypatch):
        """VUL-025: a best model that did not converge must set
        _convergence_failed=True so predict_current() defaults to VOLATILE."""
        from hmmlearn.hmm import GaussianHMM

        cfg = HMMSettings(n_iter=10, n_components=3)  # minimum allowed n_iter
        detector = RegimeDetector(symbol="BTC/USDT", timeframe="1h", cfg=cfg)

        from types import SimpleNamespace

        real_fit = GaussianHMM.fit

        def _fit_but_force_non_convergent(self, X, lengths=None):
            result = real_fit(self, X, lengths=lengths)
            # ConvergenceMonitor.converged is a read-only computed property;
            # replace the whole monitor_ with a stand-in exposing converged=False,
            # since detector.py only ever reads best_model.monitor_.converged.
            self.monitor_ = SimpleNamespace(converged=False, iter=self.monitor_.iter)
            return result

        monkeypatch.setattr(GaussianHMM, "fit", _fit_but_force_non_convergent)
        detector.fit(synthetic_features)
        assert detector.is_fitted()
        assert detector._convergence_failed is True


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

    def test_predict_current_missing_columns_raises(self, fitted_detector, synthetic_features):
        df = synthetic_features.drop(columns=["frac_diff"])
        with pytest.raises(ValueError, match="missing columns"):
            fitted_detector.predict_current(df)

    def test_predict_current_insufficient_rows_raises(self, fitted_detector, synthetic_features):
        tiny = synthetic_features.iloc[:2]  # fewer than n_components * 5
        with pytest.raises(ValueError, match="need at least"):
            fitted_detector.predict_current(tiny)

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

    def test_load_missing_manifest_raises(self, fitted_detector):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = fitted_detector.save(tmpdir)
            path.with_suffix(".sha256").unlink()
            with pytest.raises(RuntimeError, match="manifest missing"):
                RegimeDetector.load(tmpdir, symbol="BTC/USDT", timeframe="1h")

    def test_load_tampered_file_raises(self, fitted_detector):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = fitted_detector.save(tmpdir)
            with path.open("ab") as f:
                f.write(b"tampered-bytes")
            with pytest.raises(RuntimeError, match="integrity check FAILED"):
                RegimeDetector.load(tmpdir, symbol="BTC/USDT", timeframe="1h")


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

    def test_regime_statistics_empty_regime_reports_nan_means(
        self, fitted_detector, synthetic_features, monkeypatch
    ):
        """A regime state that never appears in predict_sequence()'s output
        must still get a row (count=0, pct=0.0, NaN means) rather than being
        silently omitted."""
        n = len(synthetic_features)
        # Every bar classified as trending -> ranging/volatile rows are empty.
        monkeypatch.setattr(
            fitted_detector, "predict_sequence", lambda features: np.full(n, REGIME_TRENDING)
        )
        stats = fitted_detector.regime_statistics(synthetic_features)
        assert stats.loc["ranging", "count"] == 0
        assert stats.loc["ranging", "pct"] == 0.0
        assert math.isnan(stats.loc["ranging", "mean_vol_ratio"])
        assert stats.loc["trending", "count"] == n
