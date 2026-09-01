"""
Risk & Uncertainty Quantification Layer (CAT-6).

Sits between Consensus and signal output.
"""

from __future__ import annotations

import numpy as np
import structlog

log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)


def uncertainty_score(ci_low: float, ci_high: float, consensus: float) -> tuple[float, str]:
    """Classify confidence based on CI width as % of consensus price."""
    if consensus <= 0:
        return 1.0, "suppress"
    width_pct = (ci_high - ci_low) / consensus
    if width_pct < 0.02:
        return width_pct, "high_confidence"
    if width_pct < 0.08:
        return width_pct, "moderate"
    return width_pct, "suppress"


def tail_risk_score(jump_prob: float, liquidity_score: float) -> float:
    """Combined tail risk from E-11 jump probability and E-17 liquidity score."""
    score = jump_prob * (1.0 / max(liquidity_score, 0.01))
    return float(min(score, 1.0))


def mae_estimate(consensus: float, yz_vol: float, horizon_hours: int, z_99: float = 2.576) -> float:
    """Maximum adverse excursion at 99th percentile."""
    return consensus * yz_vol * np.sqrt(horizon_hours / 8760) * z_99


class RiskQuantifier:
    def quantify(
        self,
        *,
        ci_low: float,
        ci_high: float,
        consensus: float,
        jump_prob: float = 0.0,
        liquidity_score: float = 1.0,
        yz_vol: float = 0.5,
        horizon_hours: int = 4,
    ) -> dict:
        width_pct, uncertainty_label = uncertainty_score(ci_low, ci_high, consensus)
        tr_score = tail_risk_score(jump_prob, liquidity_score)
        mae = mae_estimate(consensus, yz_vol, horizon_hours)

        if tr_score > 0.3:
            log.warning("tail_risk_active", tail_risk=tr_score, consensus=consensus)

        return {
            "uncertainty_label": uncertainty_label,
            "ci_width_pct": width_pct,
            "tail_risk_score": tr_score,
            "tail_risk_active": tr_score > 0.3,
            "mae_99": mae,
        }
