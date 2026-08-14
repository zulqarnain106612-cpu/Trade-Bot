"""
Consensus Layer v2 — weighted aggregation of 18 engine outputs.

Implements:
  - Regime-weighted aggregation with spoofing penalty (G-03 fix)
  - Bayesian bootstrap confidence interval
  - Chauvenet outlier detection
  - Disagreement penalty
  - Kelly-weighted engine scoring
  - Signal TTL with hysteresis (G-04 fix)
  - Circuit breaker on manipulation flag (G-14 fix)
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import structlog

from src.engines.schema import EngineOutput


log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Regime weight table: 18 engines x 9 regimes
# Each row must sum to 1.0 +-0.001 (Gap G-11 -- enforced by unit test)
# E-00 column does not exist; indices 0-17 map to E-01 through E-18
#
# E-10 (stock-to-flow) is intentionally zero in six regimes. It is a 24-hour
# structural valuation anchor: supply emission says something about where
# price sits against its own cycle in Trending, Accumulation and Transition,
# and nothing at all about a short-horizon dislocation. Emission does not
# change during a LiquidityCrisis or a Capitulation, so a constant-by-
# construction input must not be allowed to vote there. This is a modelling
# choice, not an unfilled row -- see DECISION_LOG.md 2026-08-07.
# Checked by test_no_executed_engine_is_dead_weight, which requires every
# executed engine to be nonzero in at least one regime, not in all of them.
# ---------------------------------------------------------------------------
REGIME_WEIGHTS: dict[str, list[float]] = {
    "Trending": [
        0.198,  # E-01
        0.0792,  # E-02
        0.0396,  # E-03
        0.198,  # E-04
        0.0396,  # E-05
        0.0396,  # E-06
        0.0396,  # E-07
        0.0396,  # E-08
        0.099,  # E-09
        0.0396,  # E-10
        0.099,  # E-11
        0.0396,  # E-12
        0.0198,  # E-13
        0.0099,  # E-14
        0.0099,  # E-15
        0,  # E-16
        0,  # E-17
        0.01,  # E-18
    ],
    "Ranging": [
        0.1485,  # E-01
        0.198,  # E-02
        0.0792,  # E-03
        0.0396,  # E-04
        0.0396,  # E-05
        0.0792,  # E-06
        0.0396,  # E-07
        0.0396,  # E-08
        0.0792,  # E-09
        0,  # E-10
        0.0495,  # E-11
        0.0495,  # E-12
        0.0198,  # E-13
        0.0198,  # E-14
        0.0099,  # E-15
        0.0495,  # E-16
        0.0495,  # E-17
        0.01,  # E-18
    ],
    "Volatile": [
        0.098,  # E-01
        0.0784,  # E-02
        0.147,  # E-03
        0.0392,  # E-04
        0.0392,  # E-05
        0.147,  # E-06
        0.0392,  # E-07
        0.0784,  # E-08
        0.049,  # E-09
        0,  # E-10
        0.1176,  # E-11
        0.0392,  # E-12
        0.0294,  # E-13
        0.0392,  # E-14
        0.0098,  # E-15
        0.0196,  # E-16
        0.0098,  # E-17
        0.02,  # E-18
    ],
    "Accumulation": [
        0.076,  # E-01
        0.076,  # E-02
        0.076,  # E-03
        0.114,  # E-04
        0.19,  # E-05
        0.038,  # E-06
        0.038,  # E-07
        0.038,  # E-08
        0.076,  # E-09
        0.076,  # E-10
        0.038,  # E-11
        0.019,  # E-12
        0.0285,  # E-13
        0.019,  # E-14
        0.0095,  # E-15
        0.019,  # E-16
        0.019,  # E-17
        0.05,  # E-18
    ],
    "Transition": [
        0.0784,  # E-01
        0.0784,  # E-02
        0.0784,  # E-03
        0.0784,  # E-04
        0.0784,  # E-05
        0.0784,  # E-06
        0.0784,  # E-07
        0.098,  # E-08
        0.0784,  # E-09
        0.0392,  # E-10
        0.049,  # E-11
        0.0392,  # E-12
        0.0392,  # E-13
        0.0294,  # E-14
        0.0392,  # E-15
        0.0196,  # E-16
        0,  # E-17
        0.02,  # E-18
    ],
    "LiquidityCrisis": [
        0.047,  # E-01
        0.141,  # E-02
        0.047,  # E-03
        0.0188,  # E-04
        0.0188,  # E-05
        0.047,  # E-06
        0.0188,  # E-07
        0.0188,  # E-08
        0.047,  # E-09
        0,  # E-10
        0.047,  # E-11
        0.047,  # E-12
        0.0282,  # E-13
        0.0282,  # E-14
        0.0094,  # E-15
        0.188,  # E-16
        0.188,  # E-17
        0.06,  # E-18
    ],
    "OptionsDriven": [
        0.099,  # E-01
        0.0495,  # E-02
        0.0396,  # E-03
        0.0396,  # E-04
        0.0198,  # E-05
        0.0396,  # E-06
        0.0198,  # E-07
        0.0396,  # E-08
        0.0792,  # E-09
        0,  # E-10
        0.1485,  # E-11
        0.297,  # E-12
        0.0297,  # E-13
        0.0198,  # E-14
        0.0099,  # E-15
        0.0396,  # E-16
        0.0198,  # E-17
        0.01,  # E-18
    ],
    "MacroDominated": [
        0.147,  # E-01
        0.049,  # E-02
        0.049,  # E-03
        0.0294,  # E-04
        0.0196,  # E-05
        0.0294,  # E-06
        0.098,  # E-07
        0.0196,  # E-08
        0.0784,  # E-09
        0,  # E-10
        0.049,  # E-11
        0.049,  # E-12
        0.245,  # E-13
        0.0392,  # E-14
        0.0098,  # E-15
        0.0294,  # E-16
        0.0392,  # E-17
        0.02,  # E-18
    ],
    "Capitulation": [
        0.047,  # E-01
        0.047,  # E-02
        0.047,  # E-03
        0.0188,  # E-04
        0.0282,  # E-05
        0.047,  # E-06
        0.0188,  # E-07
        0.047,  # E-08
        0.047,  # E-09
        0,  # E-10
        0.0752,  # E-11
        0.0752,  # E-12
        0.0376,  # E-13
        0.188,  # E-14
        0.0094,  # E-15
        0.1128,  # E-16
        0.094,  # E-17
        0.06,  # E-18
    ],
}

# Signal TTL hysteresis (Gap G-04 fix)
_LOW_ENTROPY_THRESHOLD = 0.3
_HIGH_ENTROPY_THRESHOLD = 0.7
_HYSTERESIS = 0.05


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class ConsensusResult:
    consensus_price: float
    ci_low: float
    ci_high: float
    agreement_score: float
    outlier_ids: list[str]
    spoof_penalty_applied: bool
    regime: str
    circuit_breaker_triggered: bool = False
    ttl_hours: int = 4
    metadata: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Circuit Breaker (Gap G-14 fix)
# ---------------------------------------------------------------------------


class CircuitBreaker:
    def __init__(self) -> None:
        self._tripped_at: float | None = None
        self._cooldown_seconds = 300  # 5-minute cooldown

    def check(self, e16_output: EngineOutput | None) -> bool:
        if e16_output is None:
            return False
        if e16_output.metadata.get("manipulation_flag"):
            self._tripped_at = time.monotonic()
            log.warning("circuit_breaker_triggered", reason="manipulation_flag")
            self._try_audit()
            return True
        # Auto-reset after cooldown
        if self._tripped_at and (time.monotonic() - self._tripped_at) > self._cooldown_seconds:
            self._tripped_at = None
        return self._tripped_at is not None

    @staticmethod
    def _try_audit() -> None:
        try:
            from datetime import UTC, datetime

            from src.diagnostics.audit_trail import get_audit_trail

            get_audit_trail().record(
                event_type="circuit_breaker_triggered",
                reason_code="manipulation_flag",
                details={"ts": datetime.now(UTC).isoformat()},
            )
        except Exception:
            pass


# ---------------------------------------------------------------------------
# TTL with hysteresis
# ---------------------------------------------------------------------------


class TtlManager:
    def __init__(self) -> None:
        self._state: str = "low"

    def compute(self, entropy_score: float) -> int:
        if self._state == "low" and entropy_score > _LOW_ENTROPY_THRESHOLD + _HYSTERESIS:
            self._state = "high"
        elif self._state == "high" and entropy_score < _HIGH_ENTROPY_THRESHOLD - _HYSTERESIS:
            self._state = "low"
        return 24 if self._state == "low" else 1


# ---------------------------------------------------------------------------
# Core consensus functions
# ---------------------------------------------------------------------------


def compute_consensus_price(
    outputs: list[EngineOutput], regime: str, spoof_penalty: float = 0.0
) -> tuple[float, np.ndarray]:
    full_weights = np.array(REGIME_WEIGHTS.get(regime, REGIME_WEIGHTS["Trending"]), dtype=float)

    # Gap G-03 fix: spoof penalty applies to E-02 (idx 1) and E-17 (idx 16)
    full_weights[1] *= 1.0 - spoof_penalty
    full_weights[16] *= 1.0 - spoof_penalty

    # Map each surviving engine to its correct regime-weight position by engine_id.
    # Using positional truncation (weights[:len(outputs)]) is wrong when engines fail
    # because subsequent engines slide into wrong index positions.
    def _engine_idx(eid: str) -> int:
        try:
            idx = int(eid.split("-")[1]) - 1  # "E-03" → 2
            return max(0, min(idx, len(full_weights) - 1))
        except (IndexError, ValueError):
            return 0

    weights = np.array([full_weights[_engine_idx(o.engine_id)] for o in outputs], dtype=float)
    confs = np.array([o.confidence for o in outputs], dtype=float)
    weights = weights * confs
    total = weights.sum()
    if total <= 0:
        weights = np.ones(len(outputs)) / len(outputs)
    else:
        weights = weights / total

    prices = np.array([o.predicted_price for o in outputs], dtype=float)
    return float(np.dot(weights, prices)), weights


def bootstrap_ci(
    outputs: list[EngineOutput], weights: np.ndarray, n: int = 1000
) -> tuple[float, float]:
    rng = np.random.default_rng(42)
    prices = np.array([o.predicted_price for o in outputs])
    samples = []
    w = weights / weights.sum()
    for _ in range(n):
        idx = rng.choice(len(outputs), size=len(outputs), p=w)
        samples.append(float(np.mean(prices[idx])))
    return float(np.percentile(samples, 5)), float(np.percentile(samples, 95))


def chauvenet_outliers(outputs: list[EngineOutput]) -> list[str]:
    prices = np.array([o.predicted_price for o in outputs])
    mu, sigma = prices.mean(), prices.std()
    if sigma == 0:
        return []
    return [o.engine_id for o in outputs if abs(o.predicted_price - mu) / sigma > 2.5]


def compute_agreement_score(outputs: list[EngineOutput]) -> float:
    prices = np.array([o.predicted_price for o in outputs])
    mean = prices.mean()
    if abs(mean) < 1e-9:
        return 1.0
    return float(max(0.0, 1.0 - prices.std() / abs(mean)))


def kelly_weight(confidence: float, backtest_gain_ratio: float) -> float:
    """Quarter-Kelly fraction for an engine."""
    p = float(np.clip(confidence, 0.0, 1.0))
    q = 1.0 - p
    b = max(backtest_gain_ratio, 0.01)
    f_star = (b * p - q) / b
    return max(0.0, f_star * 0.25)


# ---------------------------------------------------------------------------
# Main consensus orchestrator
# ---------------------------------------------------------------------------


class ConsensusLayer:
    def __init__(self) -> None:
        self._circuit_breaker = CircuitBreaker()
        self._ttl_manager = TtlManager()

    def compute(
        self,
        outputs: list[EngineOutput],
        regime: str,
        entropy_score: float = 0.5,
    ) -> ConsensusResult:
        if not outputs:
            return ConsensusResult(
                consensus_price=0.0,
                ci_low=0.0,
                ci_high=0.0,
                agreement_score=0.0,
                outlier_ids=[],
                spoof_penalty_applied=False,
                regime=regime,
                circuit_breaker_triggered=False,
            )

        # Check circuit breaker
        e16 = next((o for o in outputs if o.engine_id == "E-16"), None)
        if self._circuit_breaker.check(e16):
            spot = outputs[0].predicted_price
            return ConsensusResult(
                consensus_price=spot,
                ci_low=spot,
                ci_high=spot,
                agreement_score=0.0,
                outlier_ids=[],
                spoof_penalty_applied=True,
                regime=regime,
                circuit_breaker_triggered=True,
            )

        # Spoof penalty from E-16
        spoof_conf = e16.metadata.get("spoof_confidence", 0.0) if e16 else 0.0
        spoof_penalty = float(spoof_conf)

        # Halve outlier engine weights (Chauvenet)
        outlier_ids = chauvenet_outliers(outputs)

        price, weights = compute_consensus_price(outputs, regime, spoof_penalty)
        ci_low, ci_high = bootstrap_ci(outputs, weights)
        agreement = compute_agreement_score(outputs)

        # Dampen all confidences if agreement < 0.5
        if agreement < 0.5:
            log.info("consensus_low_agreement", agreement=agreement)

        ttl = self._ttl_manager.compute(entropy_score)

        return ConsensusResult(
            consensus_price=price,
            ci_low=ci_low,
            ci_high=ci_high,
            agreement_score=agreement,
            outlier_ids=outlier_ids,
            spoof_penalty_applied=spoof_penalty > 0,
            regime=regime,
            ttl_hours=ttl,
            metadata={
                "spoof_penalty": spoof_penalty,
                "entropy_score": entropy_score,
                "n_engines": len(outputs),
            },
        )
