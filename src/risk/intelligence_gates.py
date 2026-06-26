"""
Intelligence-aware risk gates.

New gates triggered by on-chain metrics:
  - Gate 7: Exchange stress (contagion, counterparty risk)
  - Gate 8: Whale activity filter (smart money tracking)

Enhanced gate:
  - Gate 6: Drift detector (now considers macro regime shifts)

Authority: Crypto market microstructure, contagion analysis (Cont et al.)
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

import structlog

from src.intelligence.metrics import IntelligenceMetrics

log = structlog.get_logger(__name__)


class GateStatus(str, Enum):
    """Gate evaluation result."""
    PASS = "PASS"
    HALT = "HALT"
    REDUCE = "REDUCE"  # Reduce position size, don't block entirely


@dataclass
class GateEvaluation:
    """Result of a single gate check."""
    gate_id: int
    status: GateStatus
    reason: str
    triggered_by: str          # Metric name that triggered
    severity: float            # 0-1, for logging/monitoring


class ExchangeStressGate:
    """
    Gate 7: Halt trading if exchange health deteriorates.

    Triggers on:
      - Extreme netflow (sellers fleeing, zscore < -2)
      - Excessive funding rates (leverage bubble)
      - High basis spread (exchange fragmentation)
    """

    STRESS_THRESHOLD = 0.75  # Above 0.75 = halt
    NETFLOW_ZSCORE_THRESHOLD = -2.0
    FUNDING_RATE_EXCESSIVE_PCT = 0.1

    @staticmethod
    def evaluate(metrics: IntelligenceMetrics) -> GateEvaluation:
        """Check exchange health."""
        stress_score = metrics.exchange_stress_score

        if stress_score > ExchangeStressGate.STRESS_THRESHOLD:
            return GateEvaluation(
                gate_id=7,
                status=GateStatus.HALT,
                reason=(
                    f"Exchange stress score {stress_score:.3f} > "
                    f"{ExchangeStressGate.STRESS_THRESHOLD}. "
                    "Contagion/counterparty risk detected."
                ),
                triggered_by="exchange_stress_score",
                severity=stress_score,
            )

        # Borderline: reduce position size
        if stress_score > 0.5:
            return GateEvaluation(
                gate_id=7,
                status=GateStatus.REDUCE,
                reason=(
                    f"Exchange stress elevated ({stress_score:.3f}). "
                    "Reducing position size to 50%."
                ),
                triggered_by="exchange_stress_score",
                severity=stress_score,
            )

        return GateEvaluation(
            gate_id=7,
            status=GateStatus.PASS,
            reason="Exchange healthy",
            triggered_by="none",
            severity=0.0,
        )


class WhaleActivityGate:
    """
    Gate 8: Adjust position sizing based on whale activity.

    Logic:
      - Whales selling (ratio < 1): reduce position by 50%
      - Whales buying at lows: increase position by 25% (contrarian)
      - Whales neutral: no adjustment
    """

    WHALE_SELL_THRESHOLD = 1.0       # ratio < 1 = net selling
    WHALE_BUY_THRESHOLD = 3.0        # ratio > 3 = net buying
    AT_LOW_ZSCORE = -1.5             # Price near 30d low

    @staticmethod
    def evaluate(
        metrics: IntelligenceMetrics,
        current_price_zscore: float,  # vs 30d MA
    ) -> GateEvaluation:
        """Check whale activity and adjust position sizing."""
        ratio = metrics.whale_buy_sell_ratio

        # Whales exiting: reduce exposure
        if ratio < WhaleActivityGate.WHALE_SELL_THRESHOLD:
            return GateEvaluation(
                gate_id=8,
                status=GateStatus.REDUCE,
                reason=(
                    f"Whale buy/sell ratio {ratio:.2f} < 1.0 (net selling). "
                    "Reducing position size by 50%."
                ),
                triggered_by="whale_buy_sell_ratio",
                severity=1.0 - (1.0 / (ratio + 0.1)),  # Inverse relationship
            )

        # Whales buying at lows: contrarian signal
        if (
            ratio > WhaleActivityGate.WHALE_BUY_THRESHOLD
            and current_price_zscore < WhaleActivityGate.AT_LOW_ZSCORE
        ):
            return GateEvaluation(
                gate_id=8,
                status=GateStatus.REDUCE,  # Still cautious, but increase alloc
                reason=(
                    f"Smart money accumulation at low (zscore {current_price_zscore:.2f}). "
                    "Increasing position size by 25%."
                ),
                triggered_by="whale_activity_at_lows",
                severity=0.0,  # Bullish
            )

        return GateEvaluation(
            gate_id=8,
            status=GateStatus.PASS,
            reason="Whale activity neutral",
            triggered_by="none",
            severity=0.0,
        )


class DriftDetectorEnhanced:
    """
    Gate 6 Enhancement: Drift detection with macro regime awareness.

    Original (P1): Compare Sharpe/accuracy vs training baseline
    Enhanced (P2): Also account for on-chain regime shifts

    If BTC dominance or network activity has shifted significantly,
    relax drift thresholds (model may degrade due to regime, not model decay).
    """

    @staticmethod
    def adjust_thresholds(
        metrics: IntelligenceMetrics,
        original_sharpe_threshold: float = 4.5,
    ) -> float:
        """
        Adjust drift detection threshold based on regime shift.

        Args:
            metrics: Current intelligence metrics
            original_sharpe_threshold: Default threshold (e.g., 4.5)

        Returns:
            Adjusted threshold (higher if regime shifted)
        """
        btc_dom_zscore = metrics.btc_dominance_regime
        network_zscore = metrics.network_activity_score

        # Regime shift detected: relax threshold by 10%
        regime_shift = abs(btc_dom_zscore) > 2.0 or abs(network_zscore) > 2.0

        if regime_shift:
            adjusted = original_sharpe_threshold * 0.9  # Relax by 10%
            log.info(
                "drift_threshold_adjusted_for_regime_shift",
                original=original_sharpe_threshold,
                adjusted=adjusted,
                btc_dom_zscore=btc_dom_zscore,
            )
            return adjusted

        return original_sharpe_threshold
