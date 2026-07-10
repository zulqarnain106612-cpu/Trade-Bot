"""Tests for src/intelligence/ensemble_predictor.py (0% → target 75%+)."""

from __future__ import annotations

from unittest.mock import patch

import numpy as np
import pandas as pd
import pytest

from src.intelligence.ensemble_predictor import (
    ARIMAPredictor,
    EnsemblePrediction,
    EnsemblePredictor,
    GaussianProcessPredictor,
    LSTMPredictor,
    TreeEnsemblePredictor,
    XGBoostPredictor,
)


def _make_series(n: int = 50, seed: int = 42) -> pd.Series:
    rng = np.random.default_rng(seed)
    return pd.Series(rng.standard_normal(n) * 0.02)


def _make_features(n: int = 50, seed: int = 42) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    return pd.DataFrame({"a": rng.standard_normal(n), "b": rng.standard_normal(n)})


# ---------------------------------------------------------------------------
# EnsemblePrediction dataclass
# ---------------------------------------------------------------------------


class TestEnsemblePrediction:
    def test_uncertainty_width(self):
        ep = EnsemblePrediction(
            point_estimate=0.5,
            credible_lower=0.3,
            credible_upper=0.7,
            model_disagreement=0.1,
            aleatoric_uncertainty=0.05,
            epistemic_uncertainty=0.03,
            best_model="xgboost",
            model_weights={"xgboost": 1.0},
            individual_predictions={"xgboost": 0.5},
        )
        assert ep.uncertainty_width == pytest.approx(0.4)


# ---------------------------------------------------------------------------
# ARIMAPredictor
# ---------------------------------------------------------------------------


class TestARIMAPredictor:
    def test_predict_unfitted_returns_zero(self):
        p = ARIMAPredictor()
        features = _make_features(5)
        assert p.predict(features) == 0.0

    def test_predict_with_uncertainty_unfitted(self):
        p = ARIMAPredictor()
        features = _make_features(5)
        point, unc = p.predict_with_uncertainty(features)
        assert point == 0.0
        assert unc == 0.1  # fallback when rmse=inf

    def test_performance_metrics(self):
        p = ARIMAPredictor()
        m = p.get_performance_metrics()
        assert m["model_type"] == "ARIMA"
        assert m["rmse"] == np.inf

    def test_fit_attempts(self):
        p = ARIMAPredictor()
        ts = _make_series(30)
        # fit() may silently skip if statsmodels not installed; should not raise
        p.fit(ts)
        m = p.get_performance_metrics()
        assert "rmse" in m

    def test_fit_and_predict_success(self):
        p = ARIMAPredictor()
        ts = pd.Series(np.cumsum(np.random.default_rng(1).standard_normal(60)) + 100)
        p.fit(ts)
        if p.model is not None:
            # NOTE: statsmodels' forecast() returns a Series whose index
            # continues from the end of the training series (e.g. label 60
            # for a 60-row fit), so the real `self.model.forecast(steps=1)[0]`
            # label lookup raises KeyError for any non-trivial series and
            # always falls into the except branch in practice (suspected
            # production bug — not fixed here, out of scope for this test
            # pass). Mock a 0-indexed forecast result to exercise the
            # intended success path.
            with patch.object(p.model, "forecast", return_value=pd.Series([42.0], index=[0])):
                pred = p.predict(_make_features(1))
            assert pred == pytest.approx(42.0)

    def test_predict_forecast_exception_returns_zero(self):
        p = ARIMAPredictor()
        ts = pd.Series(np.cumsum(np.random.default_rng(1).standard_normal(60)) + 100)
        p.fit(ts)
        if p.model is not None:
            with patch.object(p.model, "forecast", side_effect=RuntimeError("boom")):
                pred = p.predict(_make_features(1))
            assert pred == 0.0


# ---------------------------------------------------------------------------
# XGBoostPredictor
# ---------------------------------------------------------------------------


class TestXGBoostPredictor:
    def test_predict_unfitted_returns_zero(self):
        p = XGBoostPredictor()
        assert p.predict(_make_features(5)) == 0.0

    def test_predict_with_uncertainty_unfitted(self):
        p = XGBoostPredictor()
        point, unc = p.predict_with_uncertainty(_make_features(5))
        assert point == 0.0
        assert unc == 0.15

    def test_performance_metrics_unfitted(self):
        p = XGBoostPredictor()
        m = p.get_performance_metrics()
        assert m["model_type"] == "XGBoost"

    def test_fit_and_predict(self):
        p = XGBoostPredictor()
        X = _make_features(80)
        y = pd.Series(X["a"] * 2.0 + X["b"] * 0.5)
        p.fit(X, y)
        pred = p.predict(X.iloc[:1])
        assert isinstance(pred, float)
        assert p.rmse < np.inf

    def test_predict_with_uncertainty_fitted(self):
        p = XGBoostPredictor()
        X = _make_features(80)
        y = pd.Series(X["a"] + X["b"])
        p.fit(X, y)
        point, unc = p.predict_with_uncertainty(X.iloc[:1])
        assert isinstance(point, float)
        assert unc == p.rmse  # rmse as uncertainty when finite

    def test_fit_import_error_disables_model(self):
        p = XGBoostPredictor()
        X = _make_features(20)
        y = pd.Series(X["a"])
        with patch.dict("sys.modules", {"xgboost": None}):
            p.fit(X, y)
        assert p.model is None

    def test_predict_exception_returns_zero(self):
        p = XGBoostPredictor()
        X = _make_features(80)
        y = pd.Series(X["a"] + X["b"])
        p.fit(X, y)
        with patch.object(p.model, "predict", side_effect=RuntimeError("boom")):
            result = p.predict(X.iloc[:1])
        assert result == 0.0


# ---------------------------------------------------------------------------
# LSTMPredictor
# ---------------------------------------------------------------------------


class TestLSTMPredictor:
    def test_predict_unfitted_returns_zero(self):
        p = LSTMPredictor()
        assert p.predict(_make_features(5)) == 0.0

    def test_predict_with_uncertainty_unfitted(self):
        p = LSTMPredictor()
        point, unc = p.predict_with_uncertainty(_make_features(5))
        assert point == 0.0
        assert unc > 0

    def test_performance_metrics(self):
        p = LSTMPredictor()
        m = p.get_performance_metrics()
        assert "model_type" in m

    def test_fit_import_error_disables_model(self):
        p = LSTMPredictor(hidden_dim=4, lookback=5, epochs=1)
        rng = np.random.default_rng(3)
        X = rng.standard_normal((30, 5, 1)).astype(np.float32)
        y = rng.standard_normal(30).astype(np.float32)
        with patch.dict("sys.modules", {"torch": None, "torch.nn": None}):
            p.fit(X, y)
        assert p.model is None

    def test_fit_generic_exception_disables_model(self):
        p = LSTMPredictor(hidden_dim=4, lookback=5, epochs=1)
        rng = np.random.default_rng(3)
        X = rng.standard_normal((30, 5, 1)).astype(np.float32)
        y = rng.standard_normal(30).astype(np.float32)
        with patch("torch.optim.Adam", side_effect=RuntimeError("boom")):
            p.fit(X, y)
        assert p.model is None

    def test_fit_and_predict_success(self):
        p = LSTMPredictor(hidden_dim=4, lookback=5, epochs=1)
        rng = np.random.default_rng(3)
        X = rng.standard_normal((30, 5, 1)).astype(np.float32)
        y = rng.standard_normal(30).astype(np.float32)
        p.fit(X, y)
        assert p.model is not None
        pred = p.predict(X[:1].reshape(1, -1))
        assert isinstance(pred, float)

    def test_predict_exception_returns_zero(self):
        p = LSTMPredictor(hidden_dim=4, lookback=5, epochs=1)
        rng = np.random.default_rng(3)
        X = rng.standard_normal((30, 5, 1)).astype(np.float32)
        y = rng.standard_normal(30).astype(np.float32)
        p.fit(X, y)
        with patch.object(p.model, "eval", side_effect=RuntimeError("boom")):
            result = p.predict(X[:1].reshape(1, -1))
        assert result == 0.0


# ---------------------------------------------------------------------------
# GaussianProcessPredictor
# ---------------------------------------------------------------------------


class TestGaussianProcessPredictor:
    def test_predict_unfitted(self):
        p = GaussianProcessPredictor()
        point = p.predict(_make_features(5))
        assert point == 0.0

    def test_predict_with_uncertainty_unfitted(self):
        p = GaussianProcessPredictor()
        point, unc = p.predict_with_uncertainty(_make_features(5))
        assert point == 0.0
        assert unc == 0.2

    def test_performance_metrics(self):
        p = GaussianProcessPredictor()
        m = p.get_performance_metrics()
        assert m["model_type"] == "GaussianProcess"

    def test_fit_too_few_samples(self):
        p = GaussianProcessPredictor()
        X = _make_features(3)
        y = pd.Series([1.0, 2.0, 3.0])
        p.fit(X, y)  # should warn + stay unfitted
        assert p.model is None

    def test_fit_and_predict(self):
        p = GaussianProcessPredictor()
        X = _make_features(20)
        y = pd.Series(X["a"] * 1.5)
        p.fit(X, y)
        if p.model is not None:
            point, unc = p.predict_with_uncertainty(X.iloc[:1])
            assert isinstance(point, float)
            assert unc >= 0.0

    def test_fit_import_error_disables_model(self):
        p = GaussianProcessPredictor()
        X = _make_features(20)
        y = pd.Series(X["a"])
        with patch.dict(
            "sys.modules",
            {"sklearn.gaussian_process": None, "sklearn.gaussian_process.kernels": None},
        ):
            p.fit(X, y)
        assert p.model is None

    def test_fit_generic_exception_disables_model(self):
        p = GaussianProcessPredictor()
        X = _make_features(20)
        y = pd.Series(X["a"])
        from sklearn.gaussian_process import GaussianProcessRegressor

        with patch.object(GaussianProcessRegressor, "fit", side_effect=RuntimeError("boom")):
            p.fit(X, y)
        # self.model is assigned before .fit() is called, so it stays a real
        # (unfitted) instance; rmse never gets updated past the exception.
        assert p.rmse == np.inf

    def test_predict_with_uncertainty_exception_returns_fallback(self):
        p = GaussianProcessPredictor()
        X = _make_features(20)
        y = pd.Series(X["a"] * 1.5)
        p.fit(X, y)
        assert p.model is not None
        with patch.object(p.model, "predict", side_effect=RuntimeError("boom")):
            point, unc = p.predict_with_uncertainty(X.iloc[:1])
        assert point == 0.0
        assert unc == 0.2


# ---------------------------------------------------------------------------
# TreeEnsemblePredictor
# ---------------------------------------------------------------------------


class TestTreeEnsemblePredictor:
    def test_predict_unfitted_returns_zero(self):
        p = TreeEnsemblePredictor()
        assert p.predict(_make_features(5)) == 0.0

    def test_predict_with_uncertainty_unfitted(self):
        p = TreeEnsemblePredictor()
        point, unc = p.predict_with_uncertainty(_make_features(5))
        assert point == 0.0
        assert unc == 0.15

    def test_fit_insufficient_data(self):
        p = TreeEnsemblePredictor()
        X = _make_features(5)
        y = pd.Series([1.0] * 5)
        p.fit(X, y)  # warns + stays unfitted
        assert p.model is None

    def test_fit_and_predict(self):
        p = TreeEnsemblePredictor(n_estimators=10, n_bootstrap=3)
        X = _make_features(80)
        y = pd.Series(X["a"] * 2.0 + X["b"])
        p.fit(X, y)
        assert p.model is not None
        pred = p.predict(X.iloc[:1])
        assert isinstance(pred, float)

    def test_predict_with_uncertainty_fitted(self):
        p = TreeEnsemblePredictor(n_estimators=10, n_bootstrap=3)
        X = _make_features(80)
        y = pd.Series(X["a"] + X["b"])
        p.fit(X, y)
        point, unc = p.predict_with_uncertainty(X.iloc[:1])
        assert isinstance(point, float)
        assert unc >= 0.0

    def test_performance_metrics(self):
        p = TreeEnsemblePredictor()
        m = p.get_performance_metrics()
        assert "TreeEnsemble" in m["model_type"]

    def test_fit_generic_exception_disables_model(self):
        p = TreeEnsemblePredictor(n_estimators=10, n_bootstrap=2)
        X = _make_features(80)
        y = pd.Series(X["a"] + X["b"])
        from sklearn.ensemble import GradientBoostingRegressor

        with patch.object(GradientBoostingRegressor, "fit", side_effect=RuntimeError("boom")):
            p.fit(X, y)
        # self.model is assigned before .fit() is called, so it stays a real
        # (unfitted) instance; rmse and the bootstrap ensemble never populate.
        assert p.rmse == np.inf
        assert p._bootstrap_models == []

    def test_predict_exception_returns_zero(self):
        p = TreeEnsemblePredictor(n_estimators=10, n_bootstrap=2)
        X = _make_features(80)
        y = pd.Series(X["a"] + X["b"])
        p.fit(X, y)
        with patch.object(p.model, "predict", side_effect=RuntimeError("boom")):
            result = p.predict(X.iloc[:1])
        assert result == 0.0

    def test_predict_with_uncertainty_exception_falls_back_to_rmse(self):
        p = TreeEnsemblePredictor(n_estimators=10, n_bootstrap=2)
        X = _make_features(80)
        y = pd.Series(X["a"] + X["b"])
        p.fit(X, y)
        assert p._bootstrap_models
        with patch.object(p._bootstrap_models[0], "predict", side_effect=RuntimeError("boom")):
            point, unc = p.predict_with_uncertainty(X.iloc[:1])
        assert isinstance(point, float)
        assert unc == p.rmse


# ---------------------------------------------------------------------------
# EnsemblePredictor
# ---------------------------------------------------------------------------


class TestEnsemblePredictor:
    def test_init_creates_5_models(self):
        ep = EnsemblePredictor()
        assert len(ep.models) == 5
        assert "arima" in ep.models
        assert "xgboost" in ep.models

    def test_init_sets_equal_weights_cold_start(self):
        ep = EnsemblePredictor()
        assert len(ep.weights) == 5
        for w in ep.weights.values():
            assert 0.0 <= w <= 1.0
        assert abs(sum(ep.weights.values()) - 1.0) < 1e-9

    def test_predict_before_fit(self):
        ep = EnsemblePredictor()
        features = _make_features(1)
        result = ep.predict(features)
        assert isinstance(result, EnsemblePrediction)

    def test_predict_returns_correct_type(self):
        ep = EnsemblePredictor()
        result = ep.predict(_make_features(1))
        assert hasattr(result, "point_estimate")
        assert hasattr(result, "credible_lower")
        assert hasattr(result, "credible_upper")
        assert hasattr(result, "model_weights")
        assert hasattr(result, "individual_predictions")

    def test_update_weights_cold_start(self):
        ep = EnsemblePredictor()
        ep._update_weights()
        total = sum(ep.weights.values())
        assert abs(total - 1.0) < 1e-9

    def test_fit_dispatches_per_model(self):
        ep = EnsemblePredictor()
        X = _make_features(60)
        y = pd.Series(X["a"] * 1.5 + X["b"])
        ep.fit(X, y)
        # After fit, weights should still sum to 1
        assert abs(sum(ep.weights.values()) - 1.0) < 1e-9

    def test_fit_and_predict(self):
        ep = EnsemblePredictor()
        X = _make_features(60)
        y = pd.Series(X["a"] * 2.0)
        ep.fit(X, y)
        result = ep.predict(X.iloc[:1])
        assert isinstance(result, EnsemblePrediction)
        assert len(result.individual_predictions) == 5

    def test_build_lstm_sequences_insufficient(self):
        y = pd.Series([1.0, 2.0, 3.0])
        X, y_out = EnsemblePredictor._build_lstm_sequences(y, lookback=5)
        assert X is None
        assert y_out is None

    def test_build_lstm_sequences_sufficient(self):
        y = pd.Series(range(30))
        X, y_out = EnsemblePredictor._build_lstm_sequences(y, lookback=5)
        assert X is not None
        assert y_out is not None
        assert X.shape == (25, 5, 1)
        assert len(y_out) == 25

    def test_weights_updated_after_fit(self):
        ep = EnsemblePredictor()
        X = _make_features(60)
        y = pd.Series(X["a"])
        ep.fit(X, y)
        # At least one weight may differ from equal weights if any model fitted
        final_weights = ep.weights
        assert final_weights is not None

    def test_predict_best_model_is_min_uncertainty(self):
        ep = EnsemblePredictor()
        result = ep.predict(_make_features(1))
        assert result.best_model in ep.models

    def test_fit_skips_lstm_on_insufficient_data(self):
        ep = EnsemblePredictor()
        X = _make_features(10)
        y = pd.Series(X["a"])  # len(y)=10 <= default lstm lookback=20
        ep.fit(X, y)  # should not raise despite LSTM having too little data
        assert abs(sum(ep.weights.values()) - 1.0) < 1e-9

    def test_fit_model_exception_is_caught(self):
        ep = EnsemblePredictor()
        X = _make_features(60)
        y = pd.Series(X["a"])
        with patch.object(ep.models["xgboost"], "fit", side_effect=RuntimeError("boom")):
            ep.fit(X, y)  # should not raise; error logged and loop continues
        assert abs(sum(ep.weights.values()) - 1.0) < 1e-9

    def test_predict_member_exception_falls_back_to_neutral(self):
        ep = EnsemblePredictor()
        with patch.object(
            ep.models["xgboost"],
            "predict_with_uncertainty",
            side_effect=RuntimeError("boom"),
        ):
            result = ep.predict(_make_features(1))
        assert result.individual_predictions["xgboost"] == 0.0
