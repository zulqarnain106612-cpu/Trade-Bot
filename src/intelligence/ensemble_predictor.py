"""
Ensemble prediction framework.

Reduce model risk by combining diverse prediction techniques.
Output: Point forecast + credible interval + uncertainty decomposition.

Models:
  1. ARIMA: Time-series momentum
  2. XGBoost: Non-linear patterns  
  3. LSTM: Sequence learning
  4. Gaussian Process: Uncertainty quantification
  5. BART: Causal forest, heterogeneous treatment effects

Authority: Wolpert (1992) Stacked Generalization, Breiman (1996) Bagging
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Optional

import numpy as np
import pandas as pd
from scipy.stats import norm
import structlog

log = structlog.get_logger(__name__)


@dataclass
class EnsemblePrediction:
    """
    Final ensemble prediction with uncertainty.
    """
    point_estimate: float                  # Weighted average
    credible_lower: float                  # 2.5th percentile
    credible_upper: float                  # 97.5th percentile
    model_disagreement: float              # Std dev across models
    aleatoric_uncertainty: float           # Individual model noise
    epistemic_uncertainty: float           # Model disagreement
    best_model: str                        # Top-performing model
    model_weights: dict                    # {"arima": 0.15, ...}
    individual_predictions: dict           # {"arima": 0.52, "xgboost": 0.48, ...}
    
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
        pass
    
    @abstractmethod
    def predict_with_uncertainty(
        self, features: pd.DataFrame
    ) -> tuple[float, float]:  # (point, uncertainty)
        """Prediction + uncertainty estimate."""
        pass
    
    @abstractmethod
    def get_performance_metrics(self) -> dict:
        """Model performance: MAE, RMSE, etc."""
        pass


class ARIMAPredictor(PredictionModel):
    """
    ARIMA: Autoregressive Integrated Moving Average.
    Good for: Time-series momentum, trend following.
    """
    
    def __init__(self, order: tuple = (1, 1, 1)):
        self.order = order
        self.model = None
        self.rmse = np.inf
    
    def fit(self, timeseries: pd.Series):
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
            forecast = self.model.forecast(steps=1)[0]
            return float(forecast)
        except Exception as e:
            log.error("arima_prediction_failed", error=str(e))
            return 0.0
    
    def predict_with_uncertainty(self, features: pd.DataFrame) -> tuple[float, float]:
        point = self.predict(features)
        uncertainty = self.rmse if self.rmse != np.inf else 0.1
        return point, uncertainty
    
    def get_performance_metrics(self) -> dict:
        return {"rmse": self.rmse, "model_type": "ARIMA"}


class XGBoostPredictor(PredictionModel):
    """
    XGBoost: Gradient boosting.
    Good for: Non-linear patterns, feature interactions.
    """
    
    def __init__(self, max_depth: int = 6, learning_rate: float = 0.1):
        self.max_depth = max_depth
        self.learning_rate = learning_rate
        self.model = None
        self.rmse = np.inf
    
    def fit(self, X: pd.DataFrame, y: pd.Series):
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
            self.rmse = np.sqrt(np.mean((self.model.predict(X) - y)**2))
        except ImportError:
            log.warning("xgboost not installed")
    
    def predict(self, features: pd.DataFrame) -> float:
        if self.model is None:
            return 0.0
        try:
            return float(self.model.predict(features)[0])
        except Exception as e:
            log.error("xgboost_prediction_failed", error=str(e))
            return 0.0
    
    def predict_with_uncertainty(self, features: pd.DataFrame) -> tuple[float, float]:
        point = self.predict(features)
        uncertainty = self.rmse if self.rmse != np.inf else 0.15
        return point, uncertainty
    
    def get_performance_metrics(self) -> dict:
        return {"rmse": self.rmse, "model_type": "XGBoost"}


class LSTMPredictor(PredictionModel):
    """
    LSTM: Long Short-Term Memory neural network.
    Good for: Sequence patterns, long-term dependencies.
    """
    
    def __init__(self, hidden_dim: int = 64, lookback: int = 20):
        self.hidden_dim = hidden_dim
        self.lookback = lookback
        self.model = None
        self.rmse = np.inf
    
    def fit(self, X: np.ndarray, y: np.ndarray):
        """Fit LSTM (requires TensorFlow)."""
        try:
            import tensorflow as tf
            from tensorflow.keras.models import Sequential
            from tensorflow.keras.layers import LSTM, Dense
            
            self.model = Sequential([
                LSTM(self.hidden_dim, input_shape=(self.lookback, 1)),
                Dense(1)
            ])
            self.model.compile(loss='mse', optimizer='adam')
            self.model.fit(X, y, epochs=10, batch_size=32, verbose=0)
            self.rmse = np.sqrt(np.mean((self.model.predict(X, verbose=0) - y)**2))
        except ImportError:
            log.warning("tensorflow not installed, LSTM disabled")
    
    def predict(self, features: pd.DataFrame) -> float:
        if self.model is None:
            return 0.0
        try:
            # Reshape for LSTM (assumes timeseries input)
            X_reshaped = np.array(features).reshape(-1, self.lookback, 1)
            return float(self.model.predict(X_reshaped, verbose=0)[0, 0])
        except Exception as e:
            log.error("lstm_prediction_failed", error=str(e))
            return 0.0
    
    def predict_with_uncertainty(self, features: pd.DataFrame) -> tuple[float, float]:
        point = self.predict(features)
        uncertainty = self.rmse if self.rmse != np.inf else 0.2
        return point, uncertainty
    
    def get_performance_metrics(self) -> dict:
        return {"rmse": self.rmse, "model_type": "LSTM"}


class EnsemblePredictor:
    """
    Combines 5 diverse models, weights by past performance.
    
    Output: Not just point forecast, but full uncertainty quantification.
    """
    
    def __init__(self):
        self.models = {
            "arima": ARIMAPredictor(),
            "xgboost": XGBoostPredictor(),
            "lstm": LSTMPredictor(),
            # "gp": GaussianProcessPredictor(),  # TODO: sklearn GP
            # "bart": BARTPredictor(),            # TODO: bcf library
        }
        # Weights updated based on performance
        self.weights = {"arima": 0.2, "xgboost": 0.35, "lstm": 0.25}  # Sum to 1
        self._update_weights()
    
    def fit(self, X: pd.DataFrame, y: pd.Series):
        """Fit all ensemble members."""
        log.info("ensemble_fitting", num_models=len(self.models))
        
        for name, model in self.models.items():
            try:
                model.fit(X, y)
                log.info(f"{name}_fitted", metrics=model.get_performance_metrics())
            except Exception as e:
                log.error(f"{name}_fit_failed", error=str(e))
    
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
                log.error(f"ensemble_member_failed", model=name, error=str(e))
                individual_predictions[name] = 0.0
                individual_uncertainties[name] = 0.5
        
        # Weighted average of predictions
        ensemble_point = sum(
            individual_predictions[m] * self.weights[m]
            for m in self.models.keys()
        )
        
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
        best_model = min(individual_uncertainties, key=individual_uncertainties.get)
        
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
            model_disagreement=model_disagreement,
            aleatoric_uncertainty=aleatoric,
            epistemic_uncertainty=epistemic,
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
