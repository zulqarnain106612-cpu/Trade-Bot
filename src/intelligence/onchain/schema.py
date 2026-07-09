"""
OCI-007 — Canonical on-chain metrics schema.

Defines the unified dict structure that all OnChainProviders must return.
Used by the aggregator (OCI-008) for validation and safe merging.

All fields are floats. Missing/disabled fields return their neutral value.
`confidence` in [0.0, 1.0] reflects data completeness.
`timestamp` is a Unix epoch int (stored as float for dict uniformity).

Authority:
  Glassnode field taxonomy: https://glassnode.com/metrics
  CryptoQuant field reference: https://docs.cryptoquant.com
  Coinglass field reference: https://open-api-v3.coinglass.com/api/docs
"""
from __future__ import annotations

from typing import Final

# ---------------------------------------------------------------------------
# Neutral defaults — every provider must return these if data is unavailable
# ---------------------------------------------------------------------------

ONCHAIN_NEUTRAL: Final[dict[str, float]] = {
    # --- Exchange flow (CryptoQuant / Arkham) ---
    "exchange_reserve_ratio": 0.5,
    "exchange_netflow_7d_zscore": 0.0,
    # --- Miner flow (Dune / CryptoQuant) ---
    "miner_netflow_signal": 0.0,
    # --- Derivatives: OI, funding, liquidations (Coinglass / Binance / OKX) ---
    "futures_oi_change_pct": 0.0,
    "binance_funding_rate_pct": 0.0,
    "liquidation_pressure_24h_zscore": 0.0,
    "liquidation_cascade_risk_usd": 0.0,
    # --- Whale / L/S (Coinglass / Arkham) ---
    "whale_buy_sell_ratio": 1.0,
    # --- Cross-exchange stress (blended) ---
    "exchange_stress_score": 0.0,
    # --- Glassnode-gated (provisioned when key present) ---
    "staking_unlock_risk": 0.0,
    "entity_exchange_imbalance": 0.0,
    # --- DeFi TVL (DefiLlama) ---
    "defi_tvl_7d_change_pct": 0.0,
    # --- On-chain sentiment (Dune Analytics) ---
    "mvrv_z_score": 0.0,
    "sopr": 0.0,
    # --- Cross-market macro (CoinGecko / blockchain.info) ---
    "btc_dominance_regime": 0.0,
    "stablecoin_reserve_ratio": 0.5,
    "network_activity_score": 0.0,
    # --- Meta ---
    "confidence": 0.0,
    "timestamp": 0.0,
}

# Fields that contribute additional MVRV stress (internal; merged by aggregator)
_INTERNAL_FIELDS: Final[frozenset[str]] = frozenset({
    "exchange_stress_score_mvrv_contrib",
})

# Fields that are only populated with paid data sources
GATED_FIELDS: Final[frozenset[str]] = frozenset({
    "exchange_netflow_7d_zscore",
    "exchange_reserve_ratio",
    "miner_netflow_signal",
    "staking_unlock_risk",
    "entity_exchange_imbalance",
    # Dune Analytics (paid key required)
    "mvrv_z_score",
    "sopr",
})

# Required output fields (every provider result must have at least these)
REQUIRED_FIELDS: Final[frozenset[str]] = frozenset({"confidence", "timestamp"})

# All public fields (excludes internal)
ALL_FIELDS: Final[frozenset[str]] = frozenset(ONCHAIN_NEUTRAL.keys())


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------

def validate_provider_result(
    result: dict[str, float],
    provider_id: str,
    *,
    strict: bool = False,
) -> dict[str, float]:
    """
    Validate and sanitize a provider result dict.

    - Ensures required fields are present.
    - Clamps `confidence` to [0.0, 1.0].
    - Replaces NaN/Inf with neutral values.
    - Strips internal fields from the public output.
    - If `strict=True`, raises ValueError on missing required fields.
      Otherwise logs a warning and fills with neutral.

    Returns a clean copy.
    """
    import logging
    import math
    log = logging.getLogger(__name__)

    out: dict[str, float] = dict(ONCHAIN_NEUTRAL)

    for field, value in result.items():
        if field in _INTERNAL_FIELDS:
            continue  # handled separately by aggregator
        if not isinstance(value, (int, float)):
            continue
        if math.isnan(value) or math.isinf(value):
            log.warning(
                "onchain.schema.invalid_value",
                extra={"provider": provider_id, "field": field, "value": value},
            )
            continue
        out[field] = float(value)

    # Enforce required fields
    for req in REQUIRED_FIELDS:
        if req not in result:
            msg = f"Provider '{provider_id}' missing required field '{req}'"
            if strict:
                raise ValueError(msg)
            log.warning("onchain.schema.missing_required", extra={"detail": msg})

    # Clamp confidence
    out["confidence"] = max(0.0, min(1.0, out.get("confidence", 0.0)))

    return out


def merge_onchain_results(
    results: list[dict[str, float]],
    *,
    confidence_weighted: bool = True,
) -> dict[str, float]:
    """
    Merge multiple validated provider results into one.

    Strategy:
      - Each field: confidence-weighted mean across providers that
        returned a non-neutral value for that field.
      - If all providers are neutral for a field, the field stays neutral.
      - `exchange_stress_score_mvrv_contrib` (internal) is summed additively
        into `exchange_stress_score`.
      - Final `confidence` = weighted mean of provider confidences.
    """
    import math

    if not results:
        return dict(ONCHAIN_NEUTRAL)

    merged: dict[str, float] = dict(ONCHAIN_NEUTRAL)
    total_confidence = sum(r.get("confidence", 0.0) for r in results)

    for field in ALL_FIELDS:
        if field in ("confidence", "timestamp"):
            continue
        neutral = ONCHAIN_NEUTRAL[field]
        weighted_sum = 0.0
        weight_total = 0.0
        for r in results:
            val = r.get(field, neutral)
            if math.isnan(val) or math.isinf(val):
                continue
            if abs(val - neutral) < 1e-9:
                continue  # skip neutral contributions
            w = r.get("confidence", 0.0) if confidence_weighted else 1.0
            weighted_sum += val * w
            weight_total += w
        if weight_total > 1e-9:
            merged[field] = weighted_sum / weight_total

    # MVRV additive contribution to exchange_stress_score
    mvrv_sum = sum(
        r.get("exchange_stress_score_mvrv_contrib", 0.0)
        for r in results
        if isinstance(r.get("exchange_stress_score_mvrv_contrib"), (int, float))
    )
    if abs(mvrv_sum) > 1e-9:
        merged["exchange_stress_score"] = min(
            1.0, merged["exchange_stress_score"] + mvrv_sum
        )

    # Final confidence & timestamp
    n = len(results)
    merged["confidence"] = total_confidence / n if n > 0 else 0.0
    timestamps = [r.get("timestamp", 0.0) for r in results if r.get("timestamp", 0.0) > 0]
    merged["timestamp"] = max(timestamps) if timestamps else 0.0

    return merged
