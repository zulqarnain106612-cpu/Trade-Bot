"""
Probabilistic risk gates (P2+ enhancement).

Replaces deterministic thresholds with Bayesian probability models.

Gate 7: Exchange Stress Detection (P(exchange_failure | data))
Gate 8: Whale Activity Filter (with causal effects)
Gate 6 Enhanced: Drift + Regime Detection (Bayesian changepoint + causal)
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional

import numpy as np
import pandas as pd
import structlog

from src.intelligence.probabilistic import (
    BayesianExchangeStressModel,
    BayesianWhaleActivityModel,
    BayesianRegimeDetection,
    ProbabilisticPrediction,
)
from src.intelligence.risk_quantification import RiskQuantifier
from src.intelligence.causal_inference import CausalInferenceEngine

log = structlog.get_logger(__name__)


class GateDecision(str, Enum):
    """Gate decision (probabilistic version)."""
    HALT = "HALT"
    REDUCE_75 = "REDUCE_75"  # Reduce by 75%
    REDUCE_50 = "REDUCE_50"  # Reduce by 50%
    REDUCE_25 = "REDUCE_25"  # Reduce by 25%
    HOLD = "HOLD"
    INCREASE_25 = "INCREASE_25"  # Increase by 25%
    INCREASE_50 = "INCREASE_50"  # Increase by 50%


@dataclass
class ProbabilisticGateEvaluation:
    """Result of probabilistic gate evaluation."""
    gate_id: int
    decision: GateDecision
    probability_of_halt: float         # P(should halt | data)
    confidence: float                  # 0-1, how certain are we?
    reason: str
    triggered_by: list                 # List of metrics that triggered
    severity: float                    # 0-1, severity of risk
    credible_interval: tuple           # [lower, upper] of probability


class ProbabilisticGate7:
    """
    Exchange Stress Detection: P(exchange_failure | indicators)
    
    Probabilistic alternative to deterministic Gate 7.
    """
    
    def __init__(self):
        self.model = BayesianExchangeStressModel()
    
    def evaluate(
        self,
        netflow_zscore: float,
        funding_rate: float,
        basis_spread: float,
        reserve_ratio: float,
    ) -> ProbabilisticGateEvaluation:
        """
        Evaluate exchange stress probabilistically.
        
        Old (deterministic):
            IF stress_score > 0.75 THEN HALT
        
        New (probabilistic):
            P(failure) = 0.82, CI [0.65, 0.94], confidence = 0.78
            → REDUCE_50 (not binary HALT, but continuous adjustment)
        """
        
        # Get probability estimate
        prob_prediction = self.model.predict_failure_probability(
            netflow_zscore, funding_rate, basis_spread, reserve_ratio
        )
        
        prob_failure = prob_prediction.point_estimate
        confidence = prob_prediction.confidence
        
        # Decision based on probability
        if prob_failure > 0.85:
            decision = GateDecision.HALT
            severity = min(prob_failure, 1.0)
        elif prob_failure > 0.70:
            decision = GateDecision.REDUCE_75
            severity = prob_failure
        elif prob_failure > 0.55:
            decision = GateDecision.REDUCE_50
            severity = prob_failure
        elif prob_failure > 0.40:
            decision = GateDecision.REDUCE_25
            severity = prob_failure
        else:
            decision = GateDecision.HOLD
            severity = 0.0
        
        # Identify which metrics triggered
        triggered = []
        if netflow_zscore < -2.0:
            triggered.append("extreme_netflow")
        if funding_rate > 0.15:
            triggered.append("excessive_funding")
        if basis_spread > 150:
            triggered.append("high_basis_spread")
        if reserve_ratio < 0.25:
            triggered.append("low_reserves")
        
        reason = (
            f"Exchange failure probability: {prob_failure:.1%} "
            f"(95% CI: [{prob_prediction.lower_credible_interval:.1%}, "
            f"{prob_prediction.upper_credible_interval:.1%}]). "
            f"Confidence: {confidence:.0%}. "
            f"Indicators: {', '.join(triggered) or 'none'}."
        )
        
        log.info(
            "probabilistic_gate_7",
            gate_id=7,
            decision=decision.value,
            prob_failure=prob_failure,
            confidence=confidence,
            severity=severity,
        )
        
        return ProbabilisticGateEvaluation(
            gate_id=7,
            decision=decision,
            probability_of_halt=prob_failure,
            confidence=confidence,
            reason=reason,
            triggered_by=triggered,
            severity=severity,
            credible_interval=(
                prob_prediction.lower_credible_interval,
                prob_prediction.upper_credible_interval,
            ),
        )


class ProbabilisticGate8:
    """
    Whale Activity Filter: Position sizing based on whale behavior + causal effects.
    """
    
    def __init__(self):
        self.whale_model = BayesianWhaleActivityModel()
        self.causal_engine = CausalInferenceEngine()
    
    def evaluate(
        self,
        observed_whale_ratio: float,
        sample_size: int,
        market_regime: str,
        current_price_zscore: float,
    ) -> ProbabilisticGateEvaluation:
        """
        Whale activity evaluation with causal reasoning.
        """
        
        # Step 1: Estimate true whale ratio (account for sampling uncertainty)
        true_ratio_prediction = self.whale_model.estimate_true_ratio(
            observed_whale_ratio, sample_size
        )
        
        true_ratio = true_ratio_prediction.point_estimate
        confidence = true_ratio_prediction.confidence
        
        # Step 2: Estimate causal effect on volatility
        impact = self.whale_model.estimate_market_impact(true_ratio, market_regime)
        causal_effect = impact["causal_effect"]
        
        # Step 3: Decision based on both signal AND uncertainty
        # Strong signal + high confidence → aggressive move
        # Weak signal + low confidence → conservative move
        
        decision_weight = true_ratio * confidence
        
        if true_ratio > 3.0 and confidence > 0.7:
            # Strong whale buying signal + confident
            decision = GateDecision.INCREASE_50
        elif true_ratio > 2.5 and confidence > 0.6:
            decision = GateDecision.INCREASE_25
        elif true_ratio < 1.0 and confidence > 0.7:
            # Strong whale selling + confident
            decision = GateDecision.REDUCE_50
        elif true_ratio < 1.3 and confidence > 0.6:
            decision = GateDecision.REDUCE_25
        else:
            decision = GateDecision.HOLD
        
        # Contrarian signal: whales buying at lows
        at_low = current_price_zscore < -1.5
        if true_ratio > 2.0 and at_low and confidence > 0.6:
            decision = GateDecision.INCREASE_50
            reason = f"Smart money accumulation at low (zscore {current_price_zscore:.2f}). "
        else:
            reason = f"Whale ratio: {true_ratio:.2f} (observed {observed_whale_ratio:.2f}, "
        
        reason += (
            f"sample={sample_size}, confidence={confidence:.0%}). "
            f"Causal effect on volatility: {causal_effect:.1%}. "
            f"Decision: {decision.value}."
        )
        
        log.info(
            "probabilistic_gate_8",
            gate_id=8,
            decision=decision.value,
            true_ratio=true_ratio,
            confidence=confidence,
            causal_effect=causal_effect,
        )
        
        return ProbabilisticGateEvaluation(
            gate_id=8,
            decision=decision,
            probability_of_halt=0.0 if "INCREASE" in decision.value else 0.3,
            confidence=confidence,
            reason=reason,
            triggered_by=[f"whale_ratio={true_ratio:.2f}"],
            severity=1.0 - confidence,  # Lower confidence = higher severity/uncertainty
            credible_interval=(
                true_ratio_prediction.lower_credible_interval,
                true_ratio_prediction.upper_credible_interval,
            ),
        )


class ProbabilisticGate6Enhanced:
    """
    Enhanced Drift Detection: Separate model decay from regime change.
    """
    
    def __init__(self):
        self.regime_detector = BayesianRegimeDetection()
        self.risk_quantifier = RiskQuantifier()
    
    def evaluate(
        self,
        current_sharpe: float,
        baseline_sharpe: float,
        recent_returns: pd.Series,
        btc_dominance: float,
        network_activity_zscore: float,
        liquidation_pressure_zscore: float,
    ) -> ProbabilisticGateEvaluation:
        """
        Drift detection: Distinguish model decay from regime change.
        """
        
        # Detect regime via Bayesian model
        regime_result = self.regime_detector.detect_regime(
            recent_returns,
            btc_dominance,
            network_activity_zscore,
            liquidation_pressure_zscore,
        )
        
        regime_prob = regime_result["probabilities"]
        most_likely_regime = regime_result["most_likely_regime"]
        regime_confidence = regime_result["confidence"]
        
        # Adjust drift threshold based on regime
        # Different regimes have different expected Sharpe values
        regime_baselines = {
            "bull": 5.5,
            "neutral": 4.0,
            "bear": 2.5,
        }
        
        expected_sharpe = regime_baselines[most_likely_regime]
        drift_threshold = expected_sharpe - 0.8  # Drift = drop more than 0.8 from regime baseline
        
        # Decision
        if current_sharpe < drift_threshold:
            decision = GateDecision.HALT
            reason = f"Drift detected. Sharpe {current_sharpe:.2f} < threshold {drift_threshold:.2f}. "
        else:
            decision = GateDecision.HOLD
            reason = f"No drift detected. Regime = {most_likely_regime} (conf {regime_confidence:.0%}). "
        
        reason += (
            f"Regime probabilities: bull {regime_prob['bull']:.0%}, "
            f"neutral {regime_prob['neutral']:.0%}, bear {regime_prob['bear']:.0%}."
        )
        
        log.info(
            "probabilistic_gate_6_enhanced",
            gate_id=6,
            decision=decision.value,
            current_sharpe=current_sharpe,
            regime=most_likely_regime,
            regime_confidence=regime_confidence,
        )
        
        return ProbabilisticGateEvaluation(
            gate_id=6,
            decision=decision,
            probability_of_halt=0.9 if decision == GateDecision.HALT else 0.0,
            confidence=regime_confidence,
            reason=reason,
            triggered_by=[
                f"current_sharpe={current_sharpe:.2f}",
                f"regime={most_likely_regime}",
            ],
            severity=max(0.0, (drift_threshold - current_sharpe) / drift_threshold),
            credible_interval=(drift_threshold * 0.9, drift_threshold * 1.1),
        )
