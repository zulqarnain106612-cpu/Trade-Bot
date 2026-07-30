"""
Ensemble prediction framework.

Wired into the live signal path (src/engine/signal_engine.py): trained
alongside the direction/meta-label models in the orchestrator's regular
retrain cycle (ModelTrainer.train_ensemble), persisted via save()/load(),
and blended into p_long at inference time. No external API keys are
required — every member fits on OHLCV-derived features and the same
log-return series already computed by src/features/pipeline.py, so this
carries no additional network/data dependency beyond what direction/meta
training already needs.

Reduce model risk by combining diverse prediction techniques.
Output: Point forecast + credible interval + uncertainty decomposition.

Models:
  1. ARIMA: Time-series momentum
  2. XGBoost: Non-linear patterns
  3. LSTM: Sequence learning (requires `torch`; falls back to weight-0 if
     not installed — see LSTMPredictor)
  4. Gaussian Process: principled, model-native uncertainty quantification
  5. TreeEnsemble: shrunk gradient-boosted trees + bootstrap uncertainty
     (sum-of-trees structure in the spirit of BART, without an unmaintained
     or oversized dependency — see TreeEnsemblePredictor docstring for the
     full rationale; this is NOT a literal BART/MCMC implementation and is
     never described as one elsewhere in this codebase)

Authority: Wolpert (1992) Stacked Generalization, Breiman (1996) Bagging,
Rasmussen & Williams (2006) Gaussian Processes for Machine Learning.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import joblib
import numpy as np
import pandas as pd
import structlog


log = structlog.get_logger(__name__)

try:
    import torch
    import torch.nn as nn

    class _LSTMNet(nn.Module):  # type: ignore[misc]
        """
        Module-level (not nested in LSTMPredictor.fit()) so instances are
        picklable — joblib/pickle cannot serialize a class defined inside a
        function, which previously made EnsemblePredictor.save() raise
        PicklingError whenever the LSTM member had been fit.
        """

        def __init__(self, hidden_dim: int) -> None:
            super().__init__()
            self.lstm = nn.LSTM(input_size=1, hidden_size=hidden_dim, batch_first=True)
            self.head = nn.Linear(hidden_dim, 1)

        def forward(self, x: torch.Tensor) -> torch.Tensor:
            out, _ = self.lstm(x)
            return self.head(out[:, -1, :])

    _TORCH_AVAILABLE = True
except ImportError:
    _TORCH_AVAILABLE = False

_MODEL_FILENAME: Final[str] = "ensemble_{symbol}_{timeframe}.joblib"
_MANIFEST_SUFFIX: Final[str] = ".sha256"


def _write_manifest(path: Path) -> None:
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    path.with_suffix(_MANIFEST_SUFFIX).write_text(json.dumps({"file": path.name, "sha256": digest}))


def _verify_manifest(path: Path) -> None:
    manifest_path = path.with_suffix(_MANIFEST_SUFFIX)
    if not manifest_path.exists():
        raise RuntimeError(f"Ensemble model manifest missing for {path}. Re-train to regenerate.")
    manifest = json.loads(manifest_path.read_text())
    expected = manifest.get("sha256", "")
    actual = hashlib.sha256(path.read_bytes()).hexdigest()
    if not hmac.compare_digest(actual.encode(), expected.encode()):
        raise RuntimeError(
            f"Ensemble model integrity check FAILED for {path}. "
            "File may be tampered. Re-train to replace."
        )


@dataclass
class EnsemblePrediction:
    """
    Final ensemble prediction with uncertainty.
    """

    point_estimate: float  # Weighted average
    credible_lower: float  # 2.5th percentile
    credible_upper: float  # 97.5th percentile
    model_disagreement: float  # Std dev across models
    aleatoric_uncertainty: float  # Individual model noise
    epistemic_uncertainty: float  # Model disagreement
    best_model: str  # Top-performing model
    model_weights: dict  # {"arima": 0.15, ...}
    individual_predictions: dict  # {"arima": 0.52, "xgboost": 0.48, ...}

    @property
    def uncertainty_width(self) -> float:
        return self.credible_upper - self.credible_lower


class PredictionModel(ABC):
    """
    Base class for ensemble members.
    """

    @abstractmethod
    def predict(self, features: pd.DataFrame) -> float:
        """Point prediction."""

    @abstractmethod
    def predict_with_uncertainty(
        self, features: pd.DataFrame
    ) -> tuple[float, float]:  # (point, uncertainty)
        """Prediction + uncertainty estimate."""

    @abstractmethod
    def get_performance_metrics(self) -> dict[str, Any]:
        """Model performance: MAE, RMSE, etc."""

    @abstractmethod
    def fit(self, *args: Any, **kwargs: Any) -> None:
        """Fit the model. Concrete subclasses override with specific signatures."""


class ARIMAPredictor(PredictionModel):
    """
    ARIMA: Autoregressive Integrated Moving Average.
    Good for: Time-series momentum, trend following.
    """

    def __init__(self, order: tuple = (1, 1, 1)):
        self.order = order
        self.model: Any = None
        self.rmse = np.inf

    def fit(self, timeseries: pd.Series) -> None:
        """Fit ARIMA on historical data."""
        try:
            from statsmodels.tsa.arima.model import ARIMA

            self.model = ARIMA(timeseries, order=self.order).fit()
            self.rmse = np.sqrt(np.mean(self.model.resid**2))
        except ImportError:
            log.warning("statsmodels not installed, ARIMA disabled")

    def predict(self, features: pd.DataFrame) -> float:
        if self.model is None:
            return 0.0
        try:
            # statsmodels forecast() returns a Series whose index continues from
            # the training series' length (e.g. label 60 for a 60-row fit), not
            # from 0 — positional .iloc[0] is required, [0] label-indexes and
            # raises KeyError on virtually every real call.
            forecast = self.model.forecast(steps=1).iloc[0]
            return float(forecast)
        except Exception as e:
            log.error("arima_prediction_failed", error=str(e), exc_info=True)
            return 0.0

    def predict_with_uncertainty(self, features: pd.DataFrame) -> tuple[float, float]:
        point = self.predict(features)
        uncertainty = self.rmse if self.rmse != np.inf else 0.1
        return point, uncertainty

    def get_performance_metrics(self) -> dict[str, Any]:
        return {"rmse": self.rmse, "model_type": "ARIMA"}


class XGBoostPredictor(PredictionModel):
    """
    XGBoost: Gradient boosting.
    Good for: Non-linear patterns, feature interactions.
    """

    def __init__(self, max_depth: int = 6, learning_rate: float = 0.1):
        self.max_depth = max_depth
        self.learning_rate = learning_rate
        self.model: Any = None
        self.rmse = np.inf

    def fit(self, X: pd.DataFrame, y: pd.Series) -> None:
        """Fit XGBoost."""
        try:
            import xgboost as xgb

            self.model = xgb.XGBRegressor(
                max_depth=self.max_depth,
                learning_rate=self.learning_rate,
                n_estimators=100,
                random_state=42,
            )
            self.model.fit(X, y)
            self.rmse = np.sqrt(np.mean((self.model.predict(X) - y) ** 2))
        except ImportError:
            log.warning("xgboost not installed")

    def predict(self, features: pd.DataFrame) -> float:
        if self.model is None:
            return 0.0
        try:
            return float(self.model.predict(features)[0])
        except Exception as e:
            log.error("xgboost_prediction_failed", error=str(e), exc_info=True)
            return 0.0

    def predict_with_uncertainty(self, features: pd.DataFrame) -> tuple[float, float]:
        point = self.predict(features)
        uncertainty = self.rmse if self.rmse != np.inf else 0.15
        return point, uncertainty

    def get_performance_metrics(self) -> dict[str, Any]:
        return {"rmse": self.rmse, "model_type": "XGBoost"}


class LSTMPredictor(PredictionModel):
    """
    LSTM: Long Short-Term Memory neural network.
    Good for: Sequence patterns, long-term dependencies.

    GAP-015: re-implemented on `torch` instead of TensorFlow/Keras.
    TensorFlow was never an installed/pinned dependency in this repo
    (requirements.in/.lock had no tensorflow entry), meaning the original
    `import tensorflow` always hit the ImportError branch and this model
    contributed 0.0 with weight 0 in every run — a silently-dead ensemble
    member. torch>=2.4 is now a pinned dependency (requirements.in) and is
    CPU-only here (no CUDA needed for a single-feature, 20-step LSTM at
    this data scale).
    """

    def __init__(self, hidden_dim: int = 64, lookback: int = 20, epochs: int = 30):
        self.hidden_dim = hidden_dim
        self.lookback = lookback
        self.epochs = epochs
        self.model: Any = None
        self.rmse = np.inf

    def fit(self, X: np.ndarray, y: np.ndarray) -> None:
        """Fit LSTM (requires torch). X shape: (n_samples, lookback, 1)."""
        if not _TORCH_AVAILABLE:
            log.warning("torch not installed, LSTM disabled")
            return
        try:
            torch.manual_seed(42)

            net = _LSTMNet(self.hidden_dim)
            X_t = torch.tensor(np.asarray(X), dtype=torch.float32)
            y_t = torch.tensor(np.asarray(y), dtype=torch.float32).reshape(-1, 1)

            optimizer = torch.optim.Adam(net.parameters(), lr=1e-3)
            loss_fn = nn.MSELoss()

            net.train()
            batch_size = min(32, len(X_t))
            n = len(X_t)
            for _epoch in range(self.epochs):
                perm = torch.randperm(n)
                for start in range(0, n, batch_size):
                    idx = perm[start : start + batch_size]
                    optimizer.zero_grad()
                    pred = net(X_t[idx])
                    loss = loss_fn(pred, y_t[idx])
                    loss.backward()
                    optimizer.step()

            net.eval()
            with torch.no_grad():
                fitted = net(X_t).numpy().flatten()
            self.rmse = float(np.sqrt(np.mean((fitted - np.asarray(y)) ** 2)))
            self.model = net
        except Exception as e:
            log.error("lstm_fit_failed", error=str(e), exc_info=True)

    def predict(self, features: pd.DataFrame) -> float:
        if self.model is None or not _TORCH_AVAILABLE:
            return 0.0
        try:
            # Reshape for LSTM (assumes timeseries input — same contract
            # as the original implementation: caller supplies `lookback`
            # raw sequential values).
            X_reshaped = np.array(features, dtype=np.float32).reshape(-1, self.lookback, 1)
            self.model.eval()
            with torch.no_grad():
                out = self.model(torch.tensor(X_reshaped, dtype=torch.float32))
            return float(out.numpy().flatten()[0])
        except Exception as e:
            log.error("lstm_prediction_failed", error=str(e), exc_info=True)
            return 0.0

    def predict_with_uncertainty(self, features: pd.DataFrame) -> tuple[float, float]:
        point = self.predict(features)
        uncertainty = self.rmse if self.rmse != np.inf else 0.2
        return point, uncertainty

    def get_performance_metrics(self) -> dict[str, Any]:
        return {"rmse": self.rmse, "model_type": "LSTM(torch)"}


class GaussianProcessPredictor(PredictionModel):
    """
    Gaussian Process regression.
    Good for: principled, model-native uncertainty quantification — the
    posterior predictive std is a real Bayesian credible interval, not an
    RMSE proxy like the other four members use.

    Uses sklearn.gaussian_process (already an installed dependency via
    scikit-learn — no new package added). A Matern kernel (nu=1.5) is used
    rather than the smoother RBF default: financial return series are not
    infinitely differentiable, and Matern is the standard choice for
    series with occasional sharp moves (Rasmussen & Williams 2006, ch.4).
    A WhiteKernel term is included so the GP can attribute some variance
    to observation noise rather than forcing all of it into the length
    scale — without it, GPs tend to overfit illiquid/noisy bars.
    """

    def __init__(self, n_restarts_optimizer: int = 3, max_train_samples: int = 500):
        self.n_restarts_optimizer = n_restarts_optimizer
        # Exact GP inference (sklearn.gaussian_process, no sparse/inducing-
        # point approximation) inverts an n x n covariance matrix — O(n^3)
        # time, O(n^2) memory. This trainer's live retrain cycle passes
        # ~1800 rows (orchestrator._HISTORY_BARS_FOR_TRAIN=2000 bars minus
        # feature burn-in): 1800^3 ops made a single fit take many minutes,
        # impractical for a periodic retrain job. Cap to the most recent
        # `max_train_samples` rows — recency is also more relevant than
        # older history for a non-stationary financial series.
        self.max_train_samples = max_train_samples
        self.model: Any = None
        self.rmse = np.inf
        self._feature_cols: list[str] | None = None

    def fit(self, X: pd.DataFrame, y: pd.Series) -> None:
        """Fit a GP regressor on tabular features."""
        try:
            from sklearn.gaussian_process import GaussianProcessRegressor
            from sklearn.gaussian_process.kernels import ConstantKernel, Matern, WhiteKernel

            if len(X) < 5:
                # GP covariance matrix inversion is ill-conditioned with
                # too few points; rather than let sklearn silently produce
                # a degenerate fit, refuse and stay at rmse=inf so
                # _update_weights() correctly zero-weights this model.
                log.warning("gp_insufficient_data", have=len(X), need_at_least=5)
                return

            if len(X) > self.max_train_samples:
                X = X.iloc[-self.max_train_samples :]
                y = y.iloc[-self.max_train_samples :]

            self._feature_cols = list(X.columns)
            kernel = ConstantKernel(1.0) * Matern(length_scale=1.0, nu=1.5) + WhiteKernel(
                noise_level=1e-3
            )
            self.model = GaussianProcessRegressor(
                kernel=kernel,
                n_restarts_optimizer=self.n_restarts_optimizer,
                normalize_y=True,
                random_state=42,
            )
            self.model.fit(X, y)
            preds = self.model.predict(X)
            self.rmse = float(np.sqrt(np.mean((preds - y.to_numpy()) ** 2)))
        except ImportError:
            log.warning("scikit-learn gaussian_process module not available, GP disabled")
        except Exception as e:
            log.error("gp_fit_failed", error=str(e), exc_info=True)

    def predict(self, features: pd.DataFrame) -> float:
        point, _ = self.predict_with_uncertainty(features)
        return point

    def predict_with_uncertainty(self, features: pd.DataFrame) -> tuple[float, float]:
        if self.model is None:
            return 0.0, 0.2
        try:
            X = features[self._feature_cols] if self._feature_cols else features
            mean, std = self.model.predict(X, return_std=True)
            return float(mean[0]), float(std[0])
        except Exception as e:
            log.error("gp_prediction_failed", error=str(e), exc_info=True)
            return 0.0, 0.2

    def get_performance_metrics(self) -> dict[str, Any]:
        return {"rmse": self.rmse, "model_type": "GaussianProcess"}


class TreeEnsemblePredictor(PredictionModel):
    """
    Shrunk-tree ensemble with bootstrap uncertainty.

    NOTE ON NAMING — this is deliberately NOT called "BART" anywhere in
    this codebase, logs, or docstrings, even though it fills BART's slot
    in the original 5-model design (see module docstring history). A real
    BART (Chipman, George & McCulloch 2010 — Bayesian sum-of-trees with
    MCMC posterior sampling) has no good dependency option here:
      - `bartpy` (the only pure-Python implementation) is unmaintained
        since 2019, last tested against Python 3.6 — installing an
        abandoned package into a live trading risk path would just
        relocate this same "silent useless / unverified" problem into
        third-party code we cannot fix or audit.
      - `pymc-bart` is actively maintained but requires the full PyMC +
        PyTensor probabilistic-programming stack for one of five ensemble
        voters — a disproportionate dependency/maintenance footprint, and
        MCMC sampling itself becomes a new failure surface.
    Per explicit instruction: if a faithful BART can't be added at near-
    zero risk, replace the slot with something authentic that actually
    serves prediction accuracy, named honestly rather than mislabeled.

    What this IS: a sum of many shallow, shrinkage-regularized regression
    trees (sklearn.ensemble.GradientBoostingRegressor — same "weak learner
    + shrinkage" mechanism that gives BART its sum-of-trees character),
    with uncertainty estimated via bootstrap resampling (Breiman 1996)
    rather than MCMC posterior sampling. It captures BART's core practical
    contribution to this ensemble — non-parametric, tree-based structure
    with a genuine spread-based uncertainty estimate distinct from the
    other four members' parametric assumptions — without an MCMC sampler
    and without an unmaintained or oversized dependency.
    """

    def __init__(self, n_estimators: int = 100, max_depth: int = 3, n_bootstrap: int = 30):
        self.n_estimators = n_estimators
        self.max_depth = max_depth
        self.n_bootstrap = n_bootstrap
        self.model: Any = None
        self._bootstrap_models: list = []
        self.rmse = np.inf

    def fit(self, X: pd.DataFrame, y: pd.Series) -> None:
        """Fit the primary model plus a bootstrap ensemble for uncertainty."""
        try:
            from sklearn.ensemble import GradientBoostingRegressor

            if len(X) < 10:
                log.warning("tree_ensemble_insufficient_data", have=len(X), need_at_least=10)
                return

            self.model = GradientBoostingRegressor(
                n_estimators=self.n_estimators,
                max_depth=self.max_depth,
                learning_rate=0.05,
                subsample=0.8,
                random_state=42,
            )
            self.model.fit(X, y)
            preds = self.model.predict(X)
            self.rmse = float(np.sqrt(np.mean((preds - y.to_numpy()) ** 2)))

            # Bootstrap resamples give a genuine spread-based uncertainty
            # estimate (Breiman 1996), playing the role BART's posterior
            # draws would play, without an MCMC sampler.
            rng = np.random.RandomState(42)
            n = len(X)
            self._bootstrap_models = []
            for _ in range(self.n_bootstrap):
                idx = rng.randint(0, n, size=n)
                X_boot = X.iloc[idx]
                y_boot = y.iloc[idx]
                m = GradientBoostingRegressor(
                    n_estimators=max(20, self.n_estimators // 4),
                    max_depth=self.max_depth,
                    learning_rate=0.05,
                    subsample=0.8,
                    random_state=int(rng.randint(0, 2**31 - 1)),
                )
                m.fit(X_boot, y_boot)
                self._bootstrap_models.append(m)
        except Exception as e:
            log.error("tree_ensemble_fit_failed", error=str(e), exc_info=True)

    def predict(self, features: pd.DataFrame) -> float:
        if self.model is None:
            return 0.0
        try:
            return float(self.model.predict(features)[0])
        except Exception as e:
            log.error("tree_ensemble_prediction_failed", error=str(e), exc_info=True)
            return 0.0

    def predict_with_uncertainty(self, features: pd.DataFrame) -> tuple[float, float]:
        point = self.predict(features)
        if not self._bootstrap_models:
            uncertainty = self.rmse if self.rmse != np.inf else 0.15
            return point, uncertainty
        try:
            boot_preds = np.array([float(m.predict(features)[0]) for m in self._bootstrap_models])
            uncertainty = float(np.std(boot_preds))
            return point, uncertainty
        except Exception as e:
            log.error("tree_ensemble_uncertainty_failed", error=str(e), exc_info=True)
            return point, self.rmse if self.rmse != np.inf else 0.15

    def get_performance_metrics(self) -> dict[str, Any]:
        return {"rmse": self.rmse, "model_type": "TreeEnsemble(GBM+bootstrap)"}


class EnsemblePredictor:
    """
    Combines 5 diverse models, weights by past performance.

    Output: Not just point forecast, but full uncertainty quantification.
    """

    def __init__(self) -> None:
        self.models = {
            "arima": ARIMAPredictor(),
            "xgboost": XGBoostPredictor(),
            "lstm": LSTMPredictor(),
            "gp": GaussianProcessPredictor(),
            "tree_ensemble": TreeEnsemblePredictor(),
        }
        # NOTE: self.weights is fully recomputed by _update_weights() below
        # immediately on every call (and after every fit()) from whatever
        # keys are present in self.models — there is no hardcoded initial
        # weighting to keep in sync here. See _update_weights() cold-start
        # fallback for the equal-weighting behavior before any model has
        # a finite RMSE.
        self.weights: dict[str, float] = {}
        # Tabular feature column order used at fit() time — inference must
        # supply features in this exact order (XGBoost/GP/TreeEnsemble are
        # column-order-sensitive; a mismatch silently mispredicts rather
        # than raising). Set by fit(); None until then.
        self._feature_cols: list[str] | None = None
        self._update_weights()

    def fit(self, X: pd.DataFrame, y: pd.Series) -> None:
        """
        Fit all ensemble members.

        BUG FIX: previously called `model.fit(X, y)` identically for every
        model, but ARIMA and XGBoost/LSTM are not interchangeable here:

        - ARIMAPredictor.fit() takes a single univariate `timeseries`
          argument (it models the target's own autocorrelation structure,
          not a feature matrix). Calling it with (X, y) raised a TypeError
          on every single fit, silently swallowed by the broad except --
          meaning ARIMA was never actually trained through this path.
        - LSTMPredictor.fit() expects pre-windowed 3D sequences
          (n_samples, lookback, 1), not a raw tabular DataFrame. Calling it
          with the raw (X, y) either raised inside Keras or silently
          produced a meaningless fit.

        Each model is now dispatched with the input shape it actually
        requires, and weights are refreshed immediately after fitting
        (previously weights stayed at their stale pre-fit values until the
        next predict() call, which could mislead a caller checking
        .weights right after .fit()).
        """
        log.info("ensemble_fitting", num_models=len(self.models))
        self._feature_cols = list(X.columns)

        for name, model in self.models.items():
            try:
                if name == "arima":
                    # Univariate: ARIMA models the target series' own
                    # autocorrelation, not the feature matrix.
                    model.fit(y)
                elif name == "lstm":
                    lookback: int = getattr(model, "lookback", 20)
                    X_seq, y_seq = self._build_lstm_sequences(y, lookback)
                    if X_seq is None:
                        log.warning(
                            f"{name}_insufficient_data",
                            need_at_least=lookback + 1,
                            have=len(y),
                        )
                        continue
                    model.fit(X_seq, y_seq)
                else:
                    # XGBoost and any other tabular (X, y) model.
                    model.fit(X, y)
                log.info(f"{name}_fitted", metrics=model.get_performance_metrics())
            except Exception as e:
                log.error(f"{name}_fit_failed", error=str(e), exc_info=True)

        # Refresh weights immediately so .weights reflects the just-fitted
        # models rather than staying at stale pre-fit values until the next
        # predict() call.
        self._update_weights()

    @staticmethod
    def _build_lstm_sequences(
        y: pd.Series, lookback: int
    ) -> tuple[np.ndarray | None, np.ndarray | None]:
        """
        Convert a univariate target series into sliding-window sequences
        suitable for LSTM training: each input is `lookback` consecutive
        past values, each target is the value immediately following that
        window. Returns (None, None) if there isn't enough data for even
        one full window.
        """
        values = np.asarray(y, dtype=float)
        n = len(values)
        if n <= lookback:
            return None, None

        X_seq = np.array([values[i : i + lookback] for i in range(n - lookback)])
        y_seq = values[lookback:]
        return X_seq.reshape(-1, lookback, 1), y_seq

    def predict_row(self, features: dict[str, float] | pd.Series) -> EnsemblePrediction:
        """
        Convenience wrapper for live single-row inference.

        Builds a 1-row DataFrame with columns in the exact order recorded
        at fit() time, so column-order-sensitive members (XGBoost, GP,
        TreeEnsemble) never silently mispredict on a mismatched order.

        Raises
        ------
        RuntimeError : if called before fit() (no recorded column order).
        """
        if self._feature_cols is None:
            raise RuntimeError("EnsemblePredictor.predict_row() called before fit()")
        row = {col: float(features[col]) for col in self._feature_cols}
        return self.predict(pd.DataFrame([row], columns=self._feature_cols))

    def predict(self, features: pd.DataFrame) -> EnsemblePrediction:
        """
        Ensemble prediction with uncertainty quantification.

        Returns:
            EnsemblePrediction with point + credible interval + uncertainty sources
        """
        individual_predictions = {}
        individual_uncertainties = {}

        # Get predictions from all models
        for name, model in self.models.items():
            try:
                point, uncertainty = model.predict_with_uncertainty(features)
                individual_predictions[name] = point
                individual_uncertainties[name] = uncertainty
            except Exception as e:
                log.error("ensemble_member_failed", model=name, error=str(e), exc_info=True)
                individual_predictions[name] = 0.0
                individual_uncertainties[name] = 0.5

        # Weighted average of predictions
        ensemble_point = sum(individual_predictions[m] * self.weights[m] for m in self.models)

        # Aleatoric uncertainty: average of individual model uncertainties
        aleatoric = np.mean(list(individual_uncertainties.values()))

        # Epistemic uncertainty: disagreement between models
        model_disagreement = np.std(list(individual_predictions.values()))
        epistemic = model_disagreement

        # Total uncertainty
        total_uncertainty = np.sqrt(aleatoric**2 + epistemic**2)

        # Credible interval (95%)
        ci_lower = ensemble_point - 1.96 * total_uncertainty
        ci_upper = ensemble_point + 1.96 * total_uncertainty

        # Best model (lowest uncertainty)
        best_model = min(individual_uncertainties, key=lambda k: individual_uncertainties[k])

        # Update weights based on recent performance
        self._update_weights()

        log.info(
            "ensemble_prediction",
            point=ensemble_point,
            ci=[ci_lower, ci_upper],
            aleatoric=aleatoric,
            epistemic=epistemic,
            best_model=best_model,
        )

        return EnsemblePrediction(
            point_estimate=ensemble_point,
            credible_lower=ci_lower,
            credible_upper=ci_upper,
            model_disagreement=float(model_disagreement),
            aleatoric_uncertainty=float(aleatoric),
            epistemic_uncertainty=float(epistemic),
            best_model=best_model,
            model_weights=self.weights.copy(),
            individual_predictions=individual_predictions,
        )

    def _update_weights(self):
        """
        Update weights based on model performance.
        Better models get higher weights.

        BUG FIX (crash at cold start): at construction time, before any
        model has been fit, every model reports rmse=inf. 1/(inf+0.01)
        evaluates to exactly 0.0 for every model, so `total` was 0.0 and
        the final normalization `w / total` raised ZeroDivisionError --
        meaning EnsemblePredictor could not even be instantiated without
        crashing, let alone used. Caught by testing the cold-start path
        explicitly (no test previously exercised __init__ without an
        immediate full fit).
        """
        # Get RMSE from all models
        performance = {
            name: model.get_performance_metrics().get("rmse", np.inf)
            for name, model in self.models.items()
        }

        # Weight inversely by RMSE (lower error = higher weight).
        # Explicitly zero out (rather than silently underflow/inf-divide)
        # any model that hasn't reported a finite RMSE yet.
        inverse_rmse = {
            name: (1.0 / (rmse + 0.01)) if np.isfinite(rmse) else 0.0
            for name, rmse in performance.items()
        }

        total = sum(inverse_rmse.values())

        if total <= 0.0:
            # Cold start: no model has finite performance data yet (e.g.
            # immediately after construction, before .fit() is called, or
            # if every model's optional dependency -- statsmodels/xgboost/
            # tensorflow -- is missing). There is no performance signal to
            # differentiate models on, so fall back to equal weighting
            # rather than crashing or silently producing NaN weights.
            n = len(self.models)
            self.weights = {name: 1.0 / n for name in self.models}
            log.warning(
                "ensemble_weights_cold_start_fallback",
                reason="no model has finite RMSE yet; using equal weights",
                n_models=n,
            )
        else:
            self.weights = {name: w / total for name, w in inverse_rmse.items()}

    # ------------------------------------------------------------------
    # Persistence — mirrors src/regime/detector.py's save/load pattern
    # (joblib + a SHA-256 sidecar manifest verified on load).
    # ------------------------------------------------------------------

    def save(self, model_dir: str | Path, symbol: str, timeframe: str) -> Path:
        """Serialize the fitted ensemble to disk via joblib."""
        path = Path(model_dir) / _MODEL_FILENAME.format(
            symbol=symbol.replace("/", "_"),
            timeframe=timeframe,
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "models": self.models,
            "weights": self.weights,
            "feature_cols": self._feature_cols,
            "symbol": symbol,
            "timeframe": timeframe,
        }
        joblib.dump(payload, path, compress=3)
        _write_manifest(path)
        log.info("ensemble.saved", path=str(path))
        return path

    @classmethod
    def load(cls, model_dir: str | Path, symbol: str, timeframe: str) -> EnsemblePredictor:
        """
        Restore a previously saved EnsemblePredictor from disk.

        Raises
        ------
        FileNotFoundError : if no saved ensemble exists for symbol/timeframe.
        """
        path = Path(model_dir) / _MODEL_FILENAME.format(
            symbol=symbol.replace("/", "_"),
            timeframe=timeframe,
        )
        if not path.exists():
            raise FileNotFoundError(f"No saved ensemble model at {path} — call fit() first.")
        _verify_manifest(path)
        payload: dict = joblib.load(path)
        instance = cls()
        instance.models = payload["models"]
        instance.weights = payload["weights"]
        instance._feature_cols = payload["feature_cols"]
        return instance
