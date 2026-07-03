"""
ProbabilisticMetricsAdapter
===========================
Wraps BinanceIntelligenceProvider (or any provider returning a raw flat dict)
and replaces the deterministic scalar outputs with Bayesian-posterior estimates
from the probabilistic models built in this session.

Preserves the EXACT same interface that GateContext expects:
    exchange_stress_score   : float in [0, 1]   (Gate 9 input)
    whale_buy_sell_ratio    : float in [0, 10]  (Gate 10 input)

So orchestrator wiring is minimal: swap the raw provider output for the
post-processed output from this adapter with zero changes to gates.py.

Design constraints:
  - Must be non-blocking in the async tick path.
  - Probabilistic models are synchronous (numpy-based); wrap in
    asyncio.get_event_loop().run_in_executor if the MLE paths are called,
    but since the RiskQuantifier cache ensures ~0.05ms per hot call, we
    call them directly in the async context without an executor.
  - On any provider failure, fail open (return None for affected fields)
    so the existing gate fail-open logic handles it correctly.

Authority:
  GateContext field contracts: src/risk/gates.py
  BinanceIntelligenceProvider interface: src/intelligence/providers/binance_provider.py
  Bayesian models: src/intelligence/probabilistic.py (this session)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

import structlog

from src.intelligence.probabilistic import (
    BayesianExchangeStressModel,
    BayesianWhaleActivityModel,
)

log = structlog.get_logger(__name__)


@dataclass
class ProbabilisticGateInputs:
    """
    Scalar gate inputs computed via Bayesian posteriors.

    These fields map 1-to-1 onto GateContext fields so the caller can
    do:
        ctx = RiskGateContext(
            ...existing fields...,
            exchange_stress_score=p_inputs.exchange_stress_score,
            whale_buy_sell_ratio=p_inputs.whale_buy_sell_ratio,
        )

    Fields are Optional so that a provider failure produces None, which
    the existing gate fail-open logic (``if exchange_stress_score is None:
    return GateResult.pass_gate()``) handles correctly without changes.
    """
    exchange_stress_score: Optional[float]    # Bayesian P(exchange failure)
    whale_buy_sell_ratio: Optional[float]     # Bayesian estimate of true ratio
    # Metadata — not consumed by gates but useful for logging/monitoring.
    exchange_stress_confidence: float = 0.0
    whale_ratio_confidence: float = 0.0
    raw_stress_score: Optional[float] = None  # Pre-Bayesian deterministic score
    raw_whale_ratio: Optional[float] = None


class ProbabilisticMetricsAdapter:
    """
    Post-processes raw provider metrics through Bayesian models.

    Usage::

        adapter = ProbabilisticMetricsAdapter()
        raw_metrics = await binance_provider.fetch_metrics()
        p_inputs = adapter.process(raw_metrics)

        ctx = RiskGateContext(
            ...,
            exchange_stress_score=p_inputs.exchange_stress_score,
            whale_buy_sell_ratio=p_inputs.whale_buy_sell_ratio,
        )
    """

    def __init__(
        self,
        # Confidence threshold below which the whale ratio is considered
        # too uncertain to trust — adapter returns None (gate fails open)
        # rather than acting on a low-confidence estimate.
        min_whale_confidence: float = 0.20,
        # Approximate whale transaction sample size per fetch window.
        # BinanceIntelligenceProvider derives its ratio from 1h of kline
        # taker-flow data, which typically contains ~200-500 data points.
        # Passed to BayesianWhaleActivityModel for proper uncertainty
        # quantification. Override if the provider window changes.
        whale_sample_size: int = 300,
    ) -> None:
        self._stress_model = BayesianExchangeStressModel()
        self._whale_model = BayesianWhaleActivityModel()
        self._min_whale_confidence = min_whale_confidence
        self._whale_sample_size = whale_sample_size

    def process(
        self,
        raw_metrics: dict[str, Any],
    ) -> ProbabilisticGateInputs:
        """
        Apply Bayesian post-processing to provider metrics.

        Args:
            raw_metrics: dict returned by BinanceIntelligenceProvider.fetch_metrics()
                         (or any dict with the same field names).

        Returns:
            ProbabilisticGateInputs with Bayesian-posterior scalars.
        """
        exchange_stress_score = None
        exchange_stress_confidence = 0.0
        raw_stress = raw_metrics.get("exchange_stress_score")

        if raw_stress is not None:
            try:
                prediction = self._stress_model.predict_failure_probability(
                    netflow_zscore=float(raw_metrics.get("exchange_netflow_7d_zscore", 0.0)),
                    funding_rate=float(raw_metrics.get("binance_funding_rate_pct", 0.0)),
                    basis_spread=float(raw_metrics.get("cross_exchange_basis_spread_bps", 0.0)),
                    reserve_ratio=float(raw_metrics.get("exchange_reserve_ratio", 0.35)),
                )
                exchange_stress_score = prediction.point_estimate
                exchange_stress_confidence = prediction.confidence

                log.info(
                    "probabilistic_adapter.exchange_stress",
                    raw_score=round(float(raw_stress), 4),
                    bayesian_prob=round(exchange_stress_score, 4),
                    confidence=round(exchange_stress_confidence, 3),
                    ci_lower=round(prediction.lower_credible_interval, 4),
                    ci_upper=round(prediction.upper_credible_interval, 4),
                )
            except Exception as exc:
                log.error(
                    "probabilistic_adapter.exchange_stress_failed",
                    error=str(exc),
                    fallback="None (gate fails open)",
                )
                # Fail open: gate will PASS when score is None
                exchange_stress_score = None

        # --- Whale ratio ------------------------------------------------
        whale_buy_sell_ratio = None
        whale_ratio_confidence = 0.0
        raw_whale = raw_metrics.get("whale_buy_sell_ratio")

        if raw_whale is not None:
            try:
                estimation = self._whale_model.estimate_true_ratio(
                    observed_ratio=float(raw_whale),
                    sample_size=self._whale_sample_size,
                )
                whale_ratio_confidence = estimation.confidence

                if whale_ratio_confidence >= self._min_whale_confidence:
                    whale_buy_sell_ratio = estimation.point_estimate
                else:
                    # Insufficient confidence: fail open rather than
                    # acting on an unreliable estimate.
                    log.warning(
                        "probabilistic_adapter.whale_confidence_too_low",
                        confidence=round(whale_ratio_confidence, 3),
                        threshold=self._min_whale_confidence,
                        action="gate_fails_open",
                    )

                log.info(
                    "probabilistic_adapter.whale_ratio",
                    raw_ratio=round(float(raw_whale), 4),
                    bayesian_estimate=round(estimation.point_estimate, 4),
                    confidence=round(whale_ratio_confidence, 3),
                    ci_lower=round(estimation.lower_credible_interval, 4),
                    ci_upper=round(estimation.upper_credible_interval, 4),
                )
            except Exception as exc:
                log.error(
                    "probabilistic_adapter.whale_ratio_failed",
                    error=str(exc),
                    fallback="None (gate fails open)",
                )

        return ProbabilisticGateInputs(
            exchange_stress_score=exchange_stress_score,
            whale_buy_sell_ratio=whale_buy_sell_ratio,
            exchange_stress_confidence=exchange_stress_confidence,
            whale_ratio_confidence=whale_ratio_confidence,
            raw_stress_score=float(raw_stress) if raw_stress is not None else None,
            raw_whale_ratio=float(raw_whale) if raw_whale is not None else None,
        )
