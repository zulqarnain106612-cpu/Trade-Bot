"""
Derivatives features: Open Interest, funding rate, liquidations.

Consolidates the derivatives fields the exchange providers already return
(src/intelligence/providers/*_provider.py) into a single typed bundle.

This docstring previously said it consolidated a Deribit provider module
that has never existed in this repository. That is worth stating rather
than quietly deleting, because the missing module is the reason one
strategy is inert: options_carry_v1 needs an implied-vol surface to compute
OptionsCarryContext.implied_vol_zscore, and no provider here fetches one.
The fields below — open interest, funding rate, liquidation pressure — are
the derivatives data this process actually has, and none of them is an IV.

Wiring options carry therefore needs a real options-venue provider (Deribit
is the obvious source, and ccxt supports it), plus a rolling IV history for
the z-score. Until that exists the family abstains with an explicit reason
rather than being fed a fabricated vol.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import structlog

log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class DerivativesFeatures:
    open_interest_usd: float  # total open interest in USD across venues
    funding_rate: float  # annualised funding rate (e.g. 0.01 = 1% p.a.)
    liquidation_pressure: float  # net liquidation volume (USD) in last window, signed
    oi_change_pct: float  # OI change % vs previous snapshot
    funding_premium: float  # spot vs perp basis in bps


class DerivativesFeatureExtractor:
    """
    Extracts derivatives features from multiple data providers.

    Accepts the same `data` dict that the existing EngineOrchestrator passes
    to each engine, so it requires zero API changes.
    """

    def __init__(self) -> None:
        self._prev_oi: float = 0.0

    def extract(self, data: dict[str, Any]) -> DerivativesFeatures:
        """
        Extract derivatives features from the orchestrator data dict.

        Expected keys (all optional, defaults to 0.0):
          oi_usd            — open interest in USD
          funding_rate      — annualised funding rate (float)
          liquidations_usd  — net signed liquidation volume USD
          spot              — spot price
          perp_price        — perpetual contract price
        """
        oi = float(data.get("oi_usd", 0.0))
        funding = float(data.get("funding_rate", 0.0))
        liquidations = float(data.get("liquidations_usd", 0.0))
        spot = float(data.get("spot", 1.0))
        perp = float(data.get("perp_price", spot))

        oi_change = (oi - self._prev_oi) / max(abs(self._prev_oi), 1.0) if self._prev_oi else 0.0
        self._prev_oi = oi

        funding_premium_bps = (perp - spot) / max(spot, 1e-9) * 10_000

        return DerivativesFeatures(
            open_interest_usd=oi,
            funding_rate=funding,
            liquidation_pressure=liquidations,
            oi_change_pct=oi_change,
            funding_premium=funding_premium_bps,
        )

    def to_feature_vector(self, features: DerivativesFeatures) -> dict[str, float]:
        return {
            "oi_usd": features.open_interest_usd,
            "funding_rate": features.funding_rate,
            "liquidation_pressure": features.liquidation_pressure,
            "oi_change_pct": features.oi_change_pct,
            "funding_premium_bps": features.funding_premium,
        }
