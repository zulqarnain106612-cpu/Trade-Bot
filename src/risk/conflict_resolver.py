"""
HorizonConflictResolver — regime-weighted direction vote across 10 horizons.

When horizon signals conflict (e.g. h1 is long, h5 is short), this class
resolves the conflict by weighting each horizon's direction by the current
regime confidence for that horizon.

regime_weights: [10] — softmax over regime-model confidences per horizon.
Horizons with higher regime confidence get more vote weight.

Output:
  direction: np.sign(weighted_sum) ∈ {-1, 0, 1}
  weight:    np.abs(weighted_sum)  ∈ [0, 1]
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import structlog


log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class ConflictResolution:
    direction: int  # -1/0/+1
    weight: float  # absolute weighted vote strength
    conflict: bool  # True if horizons disagreed significantly
    agreement_ratio: float  # fraction of horizons voting with the winner


class HorizonConflictResolver:
    """
    Weighted voting across horizon signals with regime-based weights.

    Also computes conflict detection: if |agreement_ratio| < 0.6, the signal
    is conflicted and should be suppressed or reduced in size.
    """

    def __init__(self, conflict_threshold: float = 0.6) -> None:
        self._conflict_threshold = conflict_threshold

    def resolve(
        self,
        signals: list[dict],
        regime_weights: np.ndarray | None = None,
    ) -> ConflictResolution:
        """
        Resolve horizon signals into a single direction.

        signals: list of dicts with 'direction' (-1/0/+1) and 'confidence'
        regime_weights: [len(signals)] array; if None, uses confidence as weights

        Returns a ConflictResolution with the aggregated direction.
        """
        if not signals:
            return ConflictResolution(direction=0, weight=0.0, conflict=True, agreement_ratio=0.0)

        directions = np.array([float(s.get("direction", 0)) for s in signals])
        confidences = np.array([float(s.get("confidence", 0.0)) for s in signals])

        if regime_weights is None:
            weights = confidences
        else:
            rw = np.array(regime_weights, dtype=float)
            if len(rw) != len(signals):
                rw = np.ones(len(signals))
            weights = rw * confidences

        weight_sum = weights.sum()
        if weight_sum < 1e-9:
            return ConflictResolution(direction=0, weight=0.0, conflict=True, agreement_ratio=0.0)

        weighted_dir = float(np.average(directions, weights=weights))
        direction = int(np.sign(weighted_dir))

        # Agreement ratio: fraction of signals voting with the majority direction
        if direction != 0:
            agreeing = np.sum(weights[directions * direction > 0])
            agreement = float(agreeing / weight_sum)
        else:
            agreement = 0.5

        conflict = agreement < self._conflict_threshold

        return ConflictResolution(
            direction=direction,
            weight=float(abs(weighted_dir)),
            conflict=conflict,
            agreement_ratio=agreement,
        )

    def resolve_with_ecc(
        self,
        signals: list[dict],
        regime_weights: np.ndarray | None,
        ecc_anomaly: float,
    ) -> ConflictResolution:
        """
        Resolve with ECC anomaly boost.

        When ecc_anomaly > 0.85 (whale alert threshold), the ECC signal
        overrides the conflict status and forces caution (reduce size).
        """
        result = self.resolve(signals, regime_weights)
        if ecc_anomaly > 0.85:
            log.warning("ecc_whale_alert", ecc_anomaly=ecc_anomaly)
            return ConflictResolution(
                direction=result.direction,
                weight=result.weight * (1.0 - ecc_anomaly * 0.5),
                conflict=True,  # force conflict = reduce size
                agreement_ratio=result.agreement_ratio,
            )
        return result
