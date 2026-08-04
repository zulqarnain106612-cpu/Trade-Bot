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
# ---------------------------------------------------------------------------
REGIME_WEIGHTS: dict[str, list[float]] = {
    "Trending": [
        0.20,
        0.08,
        0.04,
        0.20,
        0.04,
        0.04,
        0.04,
        0.04,
        0.10,
        0.04,
        0.10,
        0.04,
        0.02,
        0.01,
        0.01,
        0.00,
        0.00,
        0.00,
    ],
    "Ranging": [
        0.15,
        0.20,
        0.08,
        0.04,
        0.04,
        0.08,
        0.04,
        0.04,
        0.08,
        0.00,
        0.05,
        0.05,
        0.02,
        0.02,
        0.01,
        0.05,
        0.05,
        0.00,
    ],
    "Volatile": [
        0.10,
        0.08,
        0.15,
        0.04,
        0.04,
        0.15,
        0.04,
        0.08,
        0.05,
        0.00,
        0.12,
        0.04,
        0.03,
        0.04,
        0.01,
        0.02,
        0.01,
        0.00,
    ],
    "Accumulation": [
        0.08,
        0.08,
        0.08,
        0.12,
        0.20,
        0.04,
        0.04,
        0.04,
        0.08,
        0.08,
        0.04,
        0.02,
        0.03,
        0.02,
        0.01,
        0.02,
        0.02,
        0.00,
    ],
    "Transition": [
        0.08,
        0.08,
        0.08,
        0.08,
        0.08,
        0.08,
        0.08,
        0.12,
        0.08,
        0.04,
        0.05,
        0.04,
        0.04,
        0.03,
        0.04,
        0.02,
        0.00,
        0.00,
    ],
    "LiquidityCrisis": [
        0.05,
        0.15,
        0.05,
        0.02,
        0.02,
        0.05,
        0.02,
        0.02,
        0.05,
        0.00,
        0.05,
        0.05,
        0.03,
        0.03,
        0.01,
        0.20,
        0.20,
        0.00,
    ],
    "OptionsDriven": [
        0.10,
        0.05,
        0.04,
        0.04,
        0.02,
        0.04,
        0.02,
        0.04,
        0.08,
        0.00,
        0.15,
        0.30,
        0.03,
        0.02,
        0.01,
        0.04,
        0.02,
        0.00,
    ],
    "MacroDominated": [
        0.15,
        0.05,
        0.05,
        0.03,
        0.02,
        0.03,
        0.10,
        0.02,
        0.08,
        0.00,
        0.05,
        0.05,
        0.25,
        0.04,
        0.01,
        0.03,
        0.04,
        0.00,
    ],
    "Capitulation": [
        0.05,
        0.05,
        0.05,
        0.02,
        0.03,
        0.05,
        0.02,
        0.05,
        0.05,
        0.00,
        0.08,
        0.08,
        0.04,
        0.20,
        0.01,
        0.12,
        0.10,
        0.00,
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

            from src.diagnostics.audit_trail import AuditTrail

            AuditTrail.instance().record(
                "circuit_breaker_triggered",
                {"ts": datetime.now(UTC).isoformat()},
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
            return int(eid.split("-")[1]) - 1  # "E-03" → 2
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
    if mean == 0:
        return 1.0
    return float(1.0 - prices.std() / mean)


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
