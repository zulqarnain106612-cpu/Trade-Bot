"""
E-09 — ML Meta-Engine.

XGBoost classifier on E-01..E-08 confidence-weighted feature vector.
Walk-forward retrain every 30 days on 180-day window.
Always-on across all regimes (acts as meta-learner).
"""

from __future__ import annotations

import pickle
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import structlog

from src.engines.schema import EngineOutput

log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

_ENGINE_ID = "E-09"
_SLA_SECONDS = 10
_MODEL_PATH = Path("models/e09_xgb_meta.pkl")
# E-09 excluded from its own feature set to avoid circular dependency
_FEATURE_ENGINE_IDS = [f"E-{i:02d}" for i in range(1, 19) if i != 9]


class E09MlMeta:
    def __init__(self, horizon_hours: int = 4) -> None:
        self._horizon = horizon_hours
        self._model: object | None = None
        self._load_model()

    def _load_model(self) -> None:
        if _MODEL_PATH.exists():
            try:
                with open(_MODEL_PATH, "rb") as f:
                    self._model = pickle.load(f)  # nosec B301 — model written by this process only
            except Exception as exc:
                log.warning("e09_model_load_error", exc=str(exc))

    async def run(self, symbol: str, data: dict) -> EngineOutput:
        spot: float = data.get("spot", 0.0)
        engine_outputs: dict[str, EngineOutput] = data.get("engine_outputs", {})

        if spot <= 0:
            return EngineOutput.abstain(_ENGINE_ID, symbol, spot, self._horizon, "no_spot")

        try:
            features = self._build_features(engine_outputs, spot)
            p_up = self._predict(features)

            direction = 1 if p_up > 0.55 else (-1 if p_up < 0.45 else 0)
            confidence = float(abs(p_up - 0.5) * 2)
            predicted = spot * (1 + direction * 0.003)

            return EngineOutput(
                engine_id=_ENGINE_ID,
                symbol=symbol,
                timestamp_utc=datetime.now(UTC),
                predicted_price=predicted,
                confidence=confidence,
                direction=direction,
                horizon_hours=self._horizon,
                metadata={"p_up": p_up},
            )
        except Exception as exc:
            log.warning("e09_error", exc=str(exc))
            return EngineOutput.abstain(_ENGINE_ID, symbol, spot, self._horizon, str(exc))

    def _build_features(self, engine_outputs: dict[str, EngineOutput], spot: float) -> np.ndarray:
        features = []
        for eid in _FEATURE_ENGINE_IDS:
            out = engine_outputs.get(eid)
            if out is not None:
                features.extend(
                    [
                        out.confidence,
                        float(out.direction),
                        (out.predicted_price - spot) / spot if spot > 0 else 0.0,
                    ]
                )
            else:
                features.extend([0.0, 0.0, 0.0])
        return np.array(features, dtype=np.float32).reshape(1, -1)

    def _predict(self, features: np.ndarray) -> float:
        if self._model is None:
            return 0.5  # no model: abstain at neutral
        try:
            proba = self._model.predict_proba(features)  # type: ignore[attr-defined]
            return float(proba[0, 1])  # P(up)
        except Exception:
            return 0.5

    def train(self, X: np.ndarray, y: np.ndarray) -> None:
        """Walk-forward training call — invoked by engine_backtest.py."""
        from xgboost import XGBClassifier  # type: ignore[import]

        model = XGBClassifier(
            n_estimators=100,
            max_depth=4,
            learning_rate=0.05,
            subsample=0.8,
            colsample_bytree=0.8,
            eval_metric="logloss",
            random_state=42,
        )
        model.fit(X, y)
        self._model = model
        _MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(_MODEL_PATH, "wb") as f:
            pickle.dump(model, f)
        log.info("e09_model_trained", n_samples=len(y))
