"""
E-08 — Topology / Persistent Homology engine.

Vietoris-Rips complex on sliding window embedding.
Wasserstein distance spike → regime break suppression.
Deps: giotto-tda (optional; graceful fallback).
"""

from __future__ import annotations

from collections import deque
from datetime import UTC, datetime

import numpy as np
import pandas as pd
import structlog

from src.engines.schema import EngineOutput

log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

_ENGINE_ID = "E-08"
_SLA_SECONDS = 5
_WINDOW = 128
_EMBEDDING_DIM = 3
_EMBEDDING_LAG = 1
_W_DIST_SIGMA_THRESHOLD = 2.0


def _sliding_window_embed(series: np.ndarray, dim: int, lag: int) -> np.ndarray:
    n = len(series)
    n_windows = n - (dim - 1) * lag
    if n_windows <= 0:
        return np.empty((0, dim))
    result = np.zeros((n_windows, dim))
    for i in range(n_windows):
        result[i] = [series[i + j * lag] for j in range(dim)]
    return result


def _persistence_entropy(diagrams: list) -> float:
    """Persistence entropy from birth/death pairs."""
    total_lifetime = 0.0
    lifetimes = []
    for diagram in diagrams:
        for birth, death in diagram:
            if np.isfinite(death) and death > birth:
                lt = death - birth
                lifetimes.append(lt)
                total_lifetime += lt
    if total_lifetime == 0 or not lifetimes:
        return 0.0
    probs = np.array(lifetimes) / total_lifetime
    return float(-np.sum(probs * np.log(probs + 1e-12)))


class E08Topology:
    def __init__(self, horizon_hours: int = 4) -> None:
        self._horizon = horizon_hours
        self._w_dist_history: deque[float] = deque(maxlen=200)

    async def run(self, symbol: str, data: dict) -> EngineOutput:
        df: pd.DataFrame | None = data.get("ohlcv")
        spot: float = data.get("spot", 0.0)
        if df is None or len(df) < _WINDOW or spot <= 0:
            return EngineOutput.abstain(
                _ENGINE_ID, symbol, spot, self._horizon, "insufficient_data"
            )

        try:
            series = df["close"].values[-_WINDOW:]
            embedding = _sliding_window_embed(series, _EMBEDDING_DIM, _EMBEDDING_LAG)

            w_dist, h_topo = self._compute_tda(embedding)

            # Detect regime break: Wasserstein spike
            self._w_dist_history.append(w_dist)

            regime_break = False
            if len(self._w_dist_history) > 10:
                hist = np.array(list(self._w_dist_history)[:-1])
                z = (w_dist - hist.mean()) / (hist.std() + 1e-9)
                regime_break = z > _W_DIST_SIGMA_THRESHOLD

            if regime_break:
                return EngineOutput(
                    engine_id=_ENGINE_ID,
                    symbol=symbol,
                    timestamp_utc=datetime.now(UTC),
                    predicted_price=spot,
                    confidence=0.0,
                    direction=0,
                    horizon_hours=self._horizon,
                    metadata={"regime_break": True, "w_dist": w_dist, "h_topo": h_topo},
                )

            # Clean topology: delegate direction to E-07 PCA
            pca_dir = data.get("e07_direction", 0)
            confidence = float(1.0 - min(h_topo, 1.0)) if h_topo < 0.3 else 0.2

            return EngineOutput(
                engine_id=_ENGINE_ID,
                symbol=symbol,
                timestamp_utc=datetime.now(UTC),
                predicted_price=spot * (1 + pca_dir * 0.001),
                confidence=confidence,
                direction=int(pca_dir),
                horizon_hours=self._horizon,
                metadata={"regime_break": False, "w_dist": w_dist, "h_topo": h_topo},
            )
        except Exception as exc:
            log.warning("e08_error", exc=str(exc))
            return EngineOutput.abstain(_ENGINE_ID, symbol, spot, self._horizon, str(exc))

    def _compute_tda(self, embedding: np.ndarray) -> tuple[float, float]:
        try:
            from gtda.homology import VietorisRipsPersistence  # type: ignore[import]

            vr = VietorisRipsPersistence(homology_dimensions=[0, 1])
            diagram = vr.fit_transform([embedding])[0]
            # Split by homology dimension
            h0 = diagram[diagram[:, 2] == 0][:, :2]
            h1 = diagram[diagram[:, 2] == 1][:, :2]
            entropy = _persistence_entropy([h0, h1])
            # Wasserstein distance: use max persistence as proxy
            w_dist = float(np.max(h1[:, 1] - h1[:, 0])) if len(h1) > 0 else 0.0
            return w_dist, entropy
        except ImportError:
            # Fallback: use distance matrix proxy
            dists = np.sqrt(np.sum((embedding[:, None] - embedding[None, :]) ** 2, axis=-1))
            w_dist = float(np.percentile(dists, 95))
            h_topo = float(np.std(dists) / (np.mean(dists) + 1e-9))
            return w_dist, h_topo
