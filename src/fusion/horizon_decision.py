"""One fail-closed decision over horizon-specific, OOS-validated forecasts."""

from __future__ import annotations

import math
import time
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class UnifiedHorizonDecision:
    action: str
    direction: int
    expected_return: float | None
    uncertainty: float | None
    conflict: bool
    contributions: dict[int, float]
    model_versions: dict[int, str]
    freshness_seconds: dict[int, float]
    abstain_reason: str | None
    regime_id: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "direction": self.direction,
            "expected_return": self.expected_return,
            "uncertainty": self.uncertainty,
            "conflict": self.conflict,
            "contributions": dict(self.contributions),
            "model_versions": dict(self.model_versions),
            "freshness_seconds": dict(self.freshness_seconds),
            "abstain_reason": self.abstain_reason,
            "regime_id": self.regime_id,
        }


def fuse_horizon_predictions(
    predictions: Iterable[Any],
    horizons: dict[str, dict[str, Any]],
    *,
    now: float | None = None,
    max_age_seconds: float = 30.0,
    regime_id: int | None = None,
    minimum_oos_samples: int = 100,
) -> UnifiedHorizonDecision:
    """Weight only promoted forecasts with measured OOS skill and uncertainty.

    Expected returns are weighted by excess OOS directional skill, inverse
    uncertainty, and a duration relevance term. Confidence alone is never a
    weight. Missing, stale, untrained, or uncalibrated candidates are omitted.
    """
    now = time.time() if now is None else float(now)
    by_id = {int(spec["id"]): spec for spec in horizons.values()}
    accepted: list[tuple[int, float, float, float, str, float]] = []
    for prediction in predictions:
        horizon_id = int(prediction.horizon_id)
        spec = by_id.get(horizon_id)
        if spec is None or prediction.status not in {"READY", "PROMOTED"}:
            continue
        if regime_id is None:
            continue
        if prediction.error or not prediction.model_id or not prediction.model_version:
            continue
        if not getattr(prediction, "confidence_calibrated", False):
            continue
        if not getattr(prediction, "uncertainty_calibrated", False):
            continue
        pred_ts = getattr(prediction, "prediction_ts", None)
        score = getattr(prediction, "oos_score", None)
        oos_samples = int(getattr(prediction, "oos_sample_count", 0))
        uncertainty = float(prediction.magnitude_sigma)
        expected = float(prediction.magnitude_mu)
        if (
            pred_ts is None
            or score is None
            or oos_samples < minimum_oos_samples
            or not math.isfinite(float(score))
        ):
            continue
<<<<<<< HEAD
        try:
            prediction_timestamp = float(pred_ts)
        except (TypeError, ValueError, OverflowError):
            continue
        if not math.isfinite(prediction_timestamp):
            continue
        age = now - prediction_timestamp
=======
        age = now - float(pred_ts)
>>>>>>> origin/main
        if age < 0 or age > max_age_seconds:
            continue
        score = float(score)
        if score <= 0.5 or not math.isfinite(uncertainty) or uncertainty <= 0:
            continue
        if not math.isfinite(expected):
            continue
        relevance = 1.0 / math.sqrt(max(1.0, float(spec["seconds"])))
        weight = (score - 0.5) * relevance / uncertainty
        if weight <= 0 or not math.isfinite(weight):
            continue
        accepted.append(
            (horizon_id, expected, uncertainty, weight, str(prediction.model_version), age)
        )

    if not accepted:
        return UnifiedHorizonDecision(
            "ABSTAIN",
            0,
            None,
            None,
            False,
            {},
            {},
            {},
            "no fresh OOS-validated experts",
            regime_id,
        )
    total = sum(item[3] for item in accepted)
    expected = sum(item[1] * item[3] for item in accepted) / total
    uncertainty = math.sqrt(sum((item[2] * item[3] / total) ** 2 for item in accepted))
    positive = sum(item[3] for item in accepted if item[1] > 0) / total
    negative = sum(item[3] for item in accepted if item[1] < 0) / total
    conflict = positive > 0.25 and negative > 0.25
    contributions = {item[0]: item[3] / total for item in accepted}
    versions = {item[0]: item[4] for item in accepted}
    freshness = {item[0]: item[5] for item in accepted}
    if conflict or abs(expected) <= uncertainty:
        reason = "expert disagreement" if conflict else "expected return does not clear uncertainty"
        return UnifiedHorizonDecision(
            "ABSTAIN",
            0,
            expected,
            uncertainty,
            conflict,
            contributions,
            versions,
            freshness,
            reason,
            regime_id,
        )
    direction = 1 if expected > 0 else -1
    return UnifiedHorizonDecision(
        "BUY" if direction > 0 else "SELL",
        direction,
        expected,
        uncertainty,
        conflict,
        contributions,
        versions,
        freshness,
        None,
        regime_id,
    )
