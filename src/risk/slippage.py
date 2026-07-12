"""
Slippage and market-impact model — Almgren-Chriss square-root impact.

GAP-001: Kelly sizing (src/risk/kelly.py) computes edge from model
probabilities alone and has no notion of real execution cost. Binance
market orders pay the quoted spread plus a market-impact cost that grows
with order size relative to liquidity — ignoring this overstates edge by
several bps per trade, which compounds materially at trade volume (see
.project-intel/RISK_LOG.md Risk-001).

Model (Almgren & Chriss 2001, square-root law):

    impact_bps  = impact_coeff_bps * sqrt(qty / adv_20d)
    total_bps   = spread_bps + impact_bps

`qty / adv_20d` is the order's participation rate against 20-day average
daily volume — the standard liquidity-normalisation used in the
transaction-cost-analysis literature so the impact coefficient is
comparable across symbols with very different absolute volume.

All functions/methods here are pure (no I/O, no exchange calls) so they
are independently testable and reusable by the live executor, the paper
executor (for realistic cost simulation), and the back-test harness.

Authority:
  - Almgren, R. & Chriss, N. (2001) "Optimal Execution of Portfolio
    Transactions", Journal of Risk 3(2): 5-39.
  - López de Prado (2018) AFML Ch.3 — transaction-cost-aware bet sizing;
    a bet sized without execution cost is not actually edge-positive.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Final

import structlog

from src.config import RiskSettings
from src.tuning.live_overrides import effective_risk_settings


DEFAULT_SLIPPAGE_SPREAD_BPS: Final[float] = 2.0
DEFAULT_SLIPPAGE_IMPACT_COEFF_BPS: Final[float] = 10.0
DEFAULT_SLIPPAGE_VETO_MARGIN_BPS: Final[float] = 1.0


log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

_BPS: Final[float] = 10_000.0


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SlippageEstimate:
    """
    Estimated execution cost for a single proposed order.

    symbol             : trading pair, e.g. "BTC/USDT"
    qty                : proposed order quantity, base-asset units
    notional_usd       : qty * price
    adv_20d            : 20-day average daily volume used, base-asset units
    spread_bps         : half-spread cost component, in basis points
    impact_bps         : Almgren-Chriss sqrt market-impact component, in bps
    total_slippage_bps : spread_bps + impact_bps
    total_cost_usd     : total_slippage_bps applied to notional_usd
    participation_rate : qty / adv_20d (fraction of one day's average volume)
    """

    symbol: str
    qty: float
    notional_usd: float
    adv_20d: float
    spread_bps: float
    impact_bps: float
    total_slippage_bps: float
    total_cost_usd: float
    participation_rate: float

    def as_dict(self) -> dict[str, object]:
        return {
            "symbol": self.symbol,
            "qty": round(self.qty, 8),
            "notional_usd": round(self.notional_usd, 2),
            "adv_20d": round(self.adv_20d, 4),
            "spread_bps": round(self.spread_bps, 4),
            "impact_bps": round(self.impact_bps, 4),
            "total_slippage_bps": round(self.total_slippage_bps, 4),
            "total_cost_usd": round(self.total_cost_usd, 4),
            "participation_rate": round(self.participation_rate, 6),
        }


# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------


class SlippageModel:
    """
    Almgren-Chriss square-root market-impact model.

    estimate()           : pure cost calculation for a proposed order.
    veto_if_negative_ev() : True when the signal's expected edge would be
                            consumed (or exceeded) by estimated execution
                            cost plus a configured safety margin — i.e.
                            the trade is not actually expected-value
                            positive once realistic costs are included.
    """

    def __init__(self, cfg: RiskSettings | None = None) -> None:
        self._cfg = cfg or effective_risk_settings()

    def estimate(
        self,
        symbol: str,
        qty: float,
        price: float,
        adv_20d: float,
        spread_bps: float | None = None,
    ) -> SlippageEstimate:
        """
        Estimate spread + market-impact cost for a proposed order.

        Parameters
        ----------
        symbol     : trading pair, e.g. "BTC/USDT"
        qty        : proposed order quantity (base-asset units), must be > 0
        price      : reference price (mid or last) used to compute notional,
                     must be > 0
        adv_20d    : 20-day average daily volume in base-asset units,
                     must be > 0 (caller derives this from src/data/storage.py
                     bars; this module does not query storage itself so it
                     stays pure/testable)
        spread_bps : observed quoted spread in bps from the live order book;
                     falls back to cfg.slippage_default_spread_bps when not
                     supplied (e.g. order book temporarily unavailable)

        Returns
        -------
        SlippageEstimate

        Raises
        ------
        ValueError if qty, price, or adv_20d is <= 0, or spread_bps < 0.
        """
        if not math.isfinite(qty) or qty <= 0.0:
            raise ValueError(f"qty must be a finite value > 0, got {qty}")
        if not math.isfinite(price) or price <= 0.0:
            raise ValueError(f"price must be a finite value > 0, got {price}")
        if not math.isfinite(adv_20d) or adv_20d <= 0.0:
            raise ValueError(f"adv_20d must be a finite value > 0, got {adv_20d}")

        spread = spread_bps if spread_bps is not None else self._cfg.slippage_default_spread_bps
        if not math.isfinite(spread) or spread < 0.0:
            raise ValueError(f"spread_bps must be a finite value >= 0, got {spread}")

        participation_rate = qty / adv_20d
        impact_bps = self._cfg.slippage_impact_coeff_bps * math.sqrt(participation_rate)

        total_slippage_bps = spread + impact_bps
        notional_usd = qty * price
        total_cost_usd = notional_usd * (total_slippage_bps / _BPS)

        result = SlippageEstimate(
            symbol=symbol,
            qty=qty,
            notional_usd=notional_usd,
            adv_20d=adv_20d,
            spread_bps=spread,
            impact_bps=impact_bps,
            total_slippage_bps=total_slippage_bps,
            total_cost_usd=total_cost_usd,
            participation_rate=participation_rate,
        )
        log.debug("slippage.estimate", **result.as_dict())
        return result

    def veto_if_negative_ev(
        self,
        expected_edge_bps: float,
        slippage: SlippageEstimate,
    ) -> bool:
        """
        Veto the trade when expected execution cost would erase the
        signal's expected edge, net of a configured safety margin.

        Parameters
        ----------
        expected_edge_bps : signal's expected gross edge for this trade, in
                             bps. Computed upstream by the signal engine
                             from p_long / meta-label probability and the
                             modelled win/loss ratio — this module does not
                             compute edge itself, only cost.
        slippage           : SlippageEstimate produced by estimate().

        Returns
        -------
        True  -> veto: net EV <= 0 once slippage + margin are subtracted.
        False -> trade may proceed; net EV remains positive.
        """
        if not math.isfinite(expected_edge_bps):
            log.error("slippage.invalid_expected_edge", expected_edge_bps=expected_edge_bps)
            return True

        margin = self._cfg.slippage_veto_margin_bps
        net_edge_bps = expected_edge_bps - slippage.total_slippage_bps - margin

        vetoed = net_edge_bps <= 0.0
        if vetoed:
            log.warning(
                "slippage.veto",
                expected_edge_bps=round(expected_edge_bps, 4),
                total_slippage_bps=round(slippage.total_slippage_bps, 4),
                margin_bps=margin,
                net_edge_bps=round(net_edge_bps, 4),
                symbol=slippage.symbol,
                participation_rate=round(slippage.participation_rate, 6),
            )
        return vetoed
