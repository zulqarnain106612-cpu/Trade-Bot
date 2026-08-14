"""
UTXO Curve — Hodler Index.

Age-weighted measure of how long UTXOs have been idle.  Older, larger UTXOs
indicate supply shock risk: long-term holders who do not sell even during
downturns are constraining circulating supply.

hodler_index ∈ [0, 1]:
  0 → all supply is freshly moved (young supply)
  1 → all supply has been dormant for ≥ 365 days (aged supply)

supply_shock_proxy = hodler_index > 0.75 → reduce short exposure
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

import numpy as np
import structlog


log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class UTXOCurveResult:
    hodler_index: float  # [0, 1]
    supply_shock_risk: bool  # True when hodler_index > 0.75
    young_supply_pct: float  # fraction of supply aged < 30 days
    aged_supply_pct: float  # fraction of supply aged > 365 days
    mean_age_days: float


def compute_hodler_index(utxo_set: list[dict]) -> UTXOCurveResult:
    """
    Compute the hodler index from a UTXO set.

    Each UTXO dict must have:
      timestamp:  Unix timestamp (seconds) of when the UTXO was created
      value_btc:  BTC value of the UTXO

    The hodler index uses exponential age weighting:
        weight_i = 1 - exp(-age_days_i / 365)
        hodler_index = weighted_avg(weight_i, weights=value_btc_i)
    """
    if not utxo_set:
        return UTXOCurveResult(
            hodler_index=0.0,
            supply_shock_risk=False,
            young_supply_pct=0.0,
            aged_supply_pct=0.0,
            mean_age_days=0.0,
        )

    now = datetime.now(UTC).timestamp()
    ages_days = np.array(
        [(now - float(u.get("timestamp", now))) / 86400.0 for u in utxo_set],
        dtype=float,
    )
    values = np.array([float(u.get("value_btc", u.get("amount", 0.0))) for u in utxo_set])

    # Clamp negative ages (clock skew)
    ages_days = np.maximum(ages_days, 0.0)

    weights = 1.0 - np.exp(-ages_days / 365.0)
    total_value = values.sum()
    if total_value <= 0:
        return UTXOCurveResult(
            hodler_index=0.0,
            supply_shock_risk=False,
            young_supply_pct=0.0,
            aged_supply_pct=0.0,
            mean_age_days=float(ages_days.mean()),
        )

    hodler_index = float(np.average(weights, weights=values))
    young_supply_pct = float(values[ages_days < 30].sum() / total_value)
    aged_supply_pct = float(values[ages_days > 365].sum() / total_value)
    mean_age_days = float(np.average(ages_days, weights=values))

    return UTXOCurveResult(
        hodler_index=hodler_index,
        supply_shock_risk=hodler_index > 0.75,
        young_supply_pct=young_supply_pct,
        aged_supply_pct=aged_supply_pct,
        mean_age_days=mean_age_days,
    )


def utxo_curve_feature_vector(result: UTXOCurveResult) -> dict[str, float]:
    """Convert UTXOCurveResult to a flat feature dict for the ensemble."""
    return {
        "hodler_index": result.hodler_index,
        "supply_shock_risk": float(result.supply_shock_risk),
        "young_supply_pct": result.young_supply_pct,
        "aged_supply_pct": result.aged_supply_pct,
        "mean_age_days": result.mean_age_days,
    }
