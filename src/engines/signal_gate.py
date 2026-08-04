"""
Consensus-to-Trade-Signal gate (Gap G-09 fix).

Bridges Crypto-Box consensus layer → Trade-Bot strategy layer.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class TradeSignal:
    symbol: str
    direction: int  # +1 long / -1 short / 0 neutral
    confidence: float  # 0-1, dampened by uncertainty
    kelly_multiplier: float  # 0-1, scales Kelly fraction
    regime: str
    ttl_hours: int
    warnings: list[str] = field(default_factory=list)
    consensus_price: float = 0.0
    spot_price: float = 0.0


def consensus_to_signal(
    *,
    consensus: float,
    spot: float,
    uncertainty_label: str,
    agreement: float,
    tail_risk: float,
    e16_flag: bool,
    regime: str,
    ttl_hours: int,
    symbol: str,
    raw_confidence: float = 0.5,
) -> TradeSignal:
    """
    Convert consensus layer output to a TradeSignal.

    Gap G-09 fix: threshold-based direction (±0.5% predicted move).
    Gap G-10 fix: confidence-scaled Kelly multiplier.
    """
    warnings: list[str] = []
    pct_diff = (consensus - spot) / spot if spot > 0 else 0.0

    # Direction gate
    if uncertainty_label == "suppress":
        direction = 0
        warnings.append("uncertainty_suppress")
    elif e16_flag:
        direction = 0
        warnings.append("manipulation_circuit_breaker")
    elif pct_diff > 0.005:
        direction = 1
    elif pct_diff < -0.005:
        direction = -1
    else:
        direction = 0

    # Dampen confidence under low agreement
    dampened_conf = raw_confidence * (agreement if agreement > 0 else 0.0)
    if uncertainty_label == "suppress":
        dampened_conf = 0.0

    # Gap G-10 fix: Kelly multiplier
    suppress_flag = 0.0 if uncertainty_label == "suppress" else 1.0
    kelly_mult = float(agreement * (1.0 - tail_risk) * suppress_flag)

    if tail_risk > 0.3:
        warnings.append("tail_risk_active")

    return TradeSignal(
        symbol=symbol,
        direction=direction,
        confidence=round(dampened_conf, 4),
        kelly_multiplier=round(max(0.0, kelly_mult), 4),
        regime=regime,
        ttl_hours=ttl_hours,
        warnings=warnings,
        consensus_price=consensus,
        spot_price=spot,
    )
