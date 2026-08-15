"""
Online Learning Hook — TASK-008.

NOT wired into the live signal path: nothing imports this module, so no
prediction is blended with the batch model today. The blending weight and
warm-up policy below describe how a caller should use it, not behaviour any
running code exhibits. Wiring it means touching src/engine/signal_engine.py's
prediction path, which is a live-trading change, not a refactor.

Incremental SGD classifier that updates after every resolved bar, catching
label drift between expensive XGBoost batch retrains (AFML Ch.11).

Design:
  • sklearn SGDClassifier with log_loss + partial_fit — zero new dependencies.
  • Maintains a separate direction model and meta-label model, mirroring the
    batch trainer's two-model architecture.
  • Predictions are soft-blended with the batch model at a configurable weight
    (default: online weight 0.15, batch weight 0.85) — online alone is never
    authoritative until warm-up window is met.
  • Persists to disk via joblib so state survives restarts.
  • Fail-open: any error during learn/predict returns None so the caller falls
    back to the batch model without interruption.

Authority:
  - Bottou (2010) "Large-Scale Machine Learning with Stochastic Gradient Descent"
  - López de Prado (2018) AFML Ch.11 — label drift detection via rolling OOS accuracy
  - scikit-learn SGDClassifier.partial_fit docs
"""

from __future__ import annotations

import contextlib
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

import joblib
import numpy as np
import structlog
from sklearn.linear_model import SGDClassifier
from sklearn.preprocessing import StandardScaler


if TYPE_CHECKING:
    pass


log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Minimum samples before online predictions are used at all.
_WARMUP_SAMPLES: int = 50

# Blend weight: online_pred * ONLINE_WEIGHT + batch_pred * (1 - ONLINE_WEIGHT).
# Keep low — online model is a drift detector, not a replacement.
_ONLINE_WEIGHT: float = 0.15

# Rolling accuracy window for drift detection.
_ACCURACY_WINDOW: int = 100

# Model file names inside the model_dir.
_DIR_MODEL_FILE = "online_direction.pkl"
_META_MODEL_FILE = "online_meta.pkl"


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class OnlinePrediction:
    """Blended prediction from online + batch models."""

    direction: int  # 1=long, 0=short
    p_long: float  # blended P(long)
    p_bet: float  # blended P(bet)
    online_weight: float  # actual weight applied (0 if not warmed up)
    online_samples: int  # samples seen by online models


@dataclass
class _OnlineModel:
    """Wraps an SGDClassifier + its scaler and sample counter."""

    clf: SGDClassifier
    scaler: StandardScaler
    n_samples: int = 0
    _accuracy_buf: deque[int] = field(default_factory=lambda: deque(maxlen=_ACCURACY_WINDOW))

    def partial_fit(self, X: np.ndarray, y: np.ndarray) -> None:
        X_scaled = self.scaler.partial_fit(X).transform(X)
        self.clf.partial_fit(X_scaled, y, classes=np.array([0, 1]))
        self.n_samples += len(y)

    def predict_proba(self, X: np.ndarray) -> np.ndarray:
        """Returns shape (n, 2) probability matrix."""
        X_scaled = self.scaler.transform(X)
        return self.clf.predict_proba(X_scaled)

    def record_outcome(self, predicted: int, actual: int) -> None:
        self._accuracy_buf.append(int(predicted == actual))

    @property
    def rolling_accuracy(self) -> float | None:
        if len(self._accuracy_buf) < 10:
            return None
        return float(np.mean(self._accuracy_buf))


# ---------------------------------------------------------------------------
# OnlineTrainer
# ---------------------------------------------------------------------------


class OnlineTrainer:
    """
    Incremental online learning layer over the XGBoost batch models.

    Usage::

        ot = OnlineTrainer(model_dir=Path("models/BTC_USDT_15m"))

        # Each bar: learn from the just-resolved label
        ot.learn_direction(feature_vec, label=1)  # 1=long closed profitably
        ot.learn_meta(feature_vec, p_long, label=1)

        # At prediction time: blend with batch model outputs
        blended = ot.blend(
            batch_p_long=0.62,
            batch_p_bet=0.71,
            feature_vec=vec,
        )

    The returned OnlinePrediction carries both the blended signal and the
    raw online weight applied, so callers can log or gate on it.
    """

    def __init__(self, model_dir: Path | None = None) -> None:
        self._model_dir = model_dir
        self._dir_model = self._make_dir_model()
        self._meta_model = self._make_meta_model()
        self._log = log.bind(component="online_trainer")

        if model_dir is not None:
            self._try_load(model_dir)

    # ------------------------------------------------------------------
    # Factory helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _make_dir_model() -> _OnlineModel:
        clf = SGDClassifier(
            loss="log_loss",
            penalty="l2",
            alpha=1e-4,
            learning_rate="optimal",
            random_state=42,
            warm_start=False,
        )
        return _OnlineModel(clf=clf, scaler=StandardScaler())

    @staticmethod
    def _make_meta_model() -> _OnlineModel:
        clf = SGDClassifier(
            loss="log_loss",
            penalty="l2",
            alpha=1e-4,
            learning_rate="optimal",
            random_state=42,
            warm_start=False,
        )
        return _OnlineModel(clf=clf, scaler=StandardScaler())

    # ------------------------------------------------------------------
    # Learning
    # ------------------------------------------------------------------

    def learn_direction(
        self,
        feature_vec: np.ndarray,
        label: int,
        *,
        predicted: int | None = None,
    ) -> None:
        """
        Incrementally update the direction model with one resolved sample.

        Args:
            feature_vec: 1-D array of feature values (same order as FEATURE_COLUMNS).
            label:       Ground-truth direction (1=long, 0=short).
            predicted:   The direction predicted at the time (for accuracy tracking).
        """
        try:
            X = np.asarray(feature_vec, dtype=np.float64).reshape(1, -1)
            y = np.array([int(label)])
            self._dir_model.partial_fit(X, y)
            if predicted is not None:
                self._dir_model.record_outcome(predicted, label)
        except Exception as exc:
            self._log.debug("online_trainer.learn_direction_failed", error=str(exc), exc_info=True)

    def learn_meta(
        self,
        feature_vec: np.ndarray,
        p_long: float,
        label: int,
        *,
        predicted: int | None = None,
    ) -> None:
        """
        Incrementally update the meta-label model.

        Args:
            feature_vec: 1-D feature array.
            p_long:      Direction model's P(long) at prediction time.
            label:       Ground-truth meta-label (1=bet, 0=skip).
            predicted:   Meta prediction at the time (for accuracy tracking).
        """
        try:
            base = np.asarray(feature_vec, dtype=np.float64)
            X = np.append(base, float(p_long)).reshape(1, -1)
            y = np.array([int(label)])
            self._meta_model.partial_fit(X, y)
            if predicted is not None:
                self._meta_model.record_outcome(predicted, label)
        except Exception as exc:
            self._log.debug("online_trainer.learn_meta_failed", error=str(exc), exc_info=True)

    # ------------------------------------------------------------------
    # Prediction / blending
    # ------------------------------------------------------------------

    def blend(
        self,
        batch_p_long: float,
        batch_p_bet: float,
        feature_vec: np.ndarray,
    ) -> OnlinePrediction:
        """
        Return a blended direction + meta prediction.

        If the online models haven't hit warmup, returns the batch values
        unchanged (online_weight=0). Fail-open on any error.
        """
        n_dir = self._dir_model.n_samples
        n_meta = self._meta_model.n_samples
        warmed = min(n_dir, n_meta) >= _WARMUP_SAMPLES

        online_p_long = batch_p_long
        online_p_bet = batch_p_bet
        applied_weight = 0.0

        if warmed:
            try:
                base = np.asarray(feature_vec, dtype=np.float64)
                X_dir = base.reshape(1, -1)
                dir_proba = self._dir_model.predict_proba(X_dir)
                online_p_long_raw = float(dir_proba[0, 1])

                X_meta = np.append(base, batch_p_long).reshape(1, -1)
                meta_proba = self._meta_model.predict_proba(X_meta)
                online_p_bet_raw = float(meta_proba[0, 1])

                w = _ONLINE_WEIGHT
                online_p_long = w * online_p_long_raw + (1 - w) * batch_p_long
                online_p_bet = w * online_p_bet_raw + (1 - w) * batch_p_bet
                applied_weight = w

            except Exception as exc:
                self._log.debug("online_trainer.blend_failed", error=str(exc), exc_info=True)
                # Fail-open: fall back to batch values
                online_p_long = batch_p_long
                online_p_bet = batch_p_bet
                applied_weight = 0.0

        direction = 1 if online_p_long >= 0.5 else 0
        return OnlinePrediction(
            direction=direction,
            p_long=online_p_long,
            p_bet=online_p_bet,
            online_weight=applied_weight,
            online_samples=n_dir,
        )

    # ------------------------------------------------------------------
    # Drift reporting
    # ------------------------------------------------------------------

    def accuracy_report(self) -> dict[str, float | int | None]:
        """Current rolling accuracy for both models — for logging / alerting."""
        return {
            "dir_samples": self._dir_model.n_samples,
            "dir_rolling_accuracy": self._dir_model.rolling_accuracy,
            "meta_samples": self._meta_model.n_samples,
            "meta_rolling_accuracy": self._meta_model.rolling_accuracy,
        }

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def save(self, model_dir: Path | None = None) -> None:
        """Persist both models to disk. No-op if no model_dir configured."""
        target = model_dir or self._model_dir
        if target is None:
            return
        try:
            target.mkdir(parents=True, exist_ok=True)
            joblib.dump(self._dir_model, target / _DIR_MODEL_FILE)
            joblib.dump(self._meta_model, target / _META_MODEL_FILE)
            self._log.debug(
                "online_trainer.saved",
                dir=str(target),
                dir_samples=self._dir_model.n_samples,
                meta_samples=self._meta_model.n_samples,
            )
        except Exception as exc:
            self._log.warning("online_trainer.save_failed", error=str(exc), exc_info=True)

    def _try_load(self, model_dir: Path) -> None:
        """Load persisted models if present. Silent no-op on any failure."""
        dir_path = model_dir / _DIR_MODEL_FILE
        meta_path = model_dir / _META_MODEL_FILE
        try:
            if dir_path.exists():
                self._dir_model = joblib.load(dir_path)
                self._log.info(
                    "online_trainer.loaded_direction",
                    samples=self._dir_model.n_samples,
                )
            if meta_path.exists():
                self._meta_model = joblib.load(meta_path)
                self._log.info(
                    "online_trainer.loaded_meta",
                    samples=self._meta_model.n_samples,
                )
        except Exception as exc:
            self._log.warning("online_trainer.load_failed", error=str(exc), exc_info=True)
            # Reset to fresh models to avoid corrupt state.
            self._dir_model = self._make_dir_model()
            self._meta_model = self._make_meta_model()

    # ------------------------------------------------------------------
    # Reset (e.g. after a batch retrain that obsoletes the online state)
    # ------------------------------------------------------------------

    def reset(self, model_dir: Path | None = None) -> None:
        """
        Discard accumulated online state.

        Call after a batch retrain so the online model re-adapts from a clean
        slate on top of the new batch model's feature space.
        """
        self._dir_model = self._make_dir_model()
        self._meta_model = self._make_meta_model()
        target = model_dir or self._model_dir
        if target is not None:
            for f in [target / _DIR_MODEL_FILE, target / _META_MODEL_FILE]:
                with contextlib.suppress(OSError):
                    f.unlink(missing_ok=True)
        self._log.info("online_trainer.reset")
