"""
Macro regime classifier — v7 Portfolio-Level Macro & Cross-Asset Overlay.

A separate, coarser regime signal from v1/v4's per-symbol HMM: this
classifies aggregate risk-on/risk-off appetite from macro/on-chain
indicators already surfaced by the existing intelligence providers
(funding-rate cycles, stablecoin supply growth, exchange netflows via
src/intelligence/onchain/), so aggregate portfolio exposure can scale with
macro confidence independently of any single symbol's regime.

Authority:
  - Domain Prior: treat HMM transitions as probabilistic; avoid hard-coded
    regime logic — mirrored here as continuous risk_appetite score, never
    a hard binary switch.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class MacroRegime(Enum):
    RISK_ON = "risk_on"
    NEUTRAL = "neutral"
    RISK_OFF = "risk_off"


@dataclass(frozen=True, slots=True)
class MacroIndicators:
    """
    Pre-computed macro signals, sourced from existing providers:
      funding_rate_zscore_avg   : cross-symbol average funding z-score
                                  (src/intelligence/providers/*_provider.py)
      stablecoin_supply_growth_pct : rolling stablecoin supply growth
      net_exchange_inflow_zscore   : exchange netflow z-score (positive =
                                      inflow = often bearish/risk-off signal)
    """

    funding_rate_zscore_avg: float
    stablecoin_supply_growth_pct: float
    net_exchange_inflow_zscore: float


@dataclass(frozen=True, slots=True)
class MacroRegimeResult:
    regime: MacroRegime
    risk_appetite: float  # continuous score in [-1, 1]: -1 = max risk-off, +1 = max risk-on


def classify_macro_regime(indicators: MacroIndicators) -> MacroRegimeResult:
    """
    Weighted composite of the three indicators into a continuous
    risk_appetite score, then a coarse label for logging/dashboards only —
    the score, not the label, should drive exposure scaling.
    """
    # Stablecoin supply growth is risk-on (capital entering crypto to buy).
    # Positive funding z-score = crowded longs = later-cycle risk (mild
    # risk-off tilt). Positive net inflow to exchanges = distribution risk
    # (risk-off tilt).
    score = (
        0.4 * _clip(indicators.stablecoin_supply_growth_pct / 5.0)
        - 0.3 * _clip(indicators.funding_rate_zscore_avg / 3.0)
        - 0.3 * _clip(indicators.net_exchange_inflow_zscore / 3.0)
    )
    score = _clip(score)

    if score > 0.2:
        regime = MacroRegime.RISK_ON
    elif score < -0.2:
        regime = MacroRegime.RISK_OFF
    else:
        regime = MacroRegime.NEUTRAL

    return MacroRegimeResult(regime=regime, risk_appetite=score)


def _clip(x: float) -> float:
    return max(-1.0, min(1.0, x))
