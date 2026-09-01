"""
Capital allocator - portfolio-level capital split across registry strategies.

Three policies are available:

  equal_weight_allocate          - baseline: uniform split, Carver (2019) Ch.11
  performance_weighted_allocate  - Sharpe-weighted split using live attribution
                                   data; falls back to equal-weight for strategies
                                   without sufficient trade history.
  risk_parity_allocate           - inverse-volatility weighting so each strategy
                                   contributes equal P&L variance; Qian (2005).

Authority:
  - Carver (2019) Systematic Trading Ch.11 - equal-weight as a robust
    starting allocation before optimizing
  - Sharpe (1966) "Mutual Fund Performance" - risk-adjusted weight signal
  - Lopez de Prado (2018) AFML Ch.16 - portfolio construction across
    heterogeneous strategies
  - Qian (2005) "Risk Parity Portfolios" - inverse-volatility capital allocation
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import structlog

from src.strategies.registry import StrategyProtocol

log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

# Minimum trades required before a strategy is given a Sharpe-derived weight
# rather than the equal-weight fallback.  Below this threshold the Sharpe
# estimate is too noisy to trust (central-limit-theorem rule of thumb: >= 30).
_MIN_TRADES_FOR_SHARPE: int = 30

# Floor weight given to strategies with negative Sharpe - keeps them in the
# portfolio at a minimal allocation rather than zeroing them (they may be in
# a temporary drawdown, not permanently broken).  Expressed as a fraction of
# the equal-weight share so it scales with portfolio size.
_NEG_SHARPE_WEIGHT_FRACTION: float = 0.25

# Minimum trades required before a strategy's realized P&L volatility is used
# for risk-parity weighting.  Below this, the strategy gets the equal-weight
# share (same warm-up guard as the Sharpe estimator).
_MIN_TRADES_FOR_VOL: int = 20

# Minimum annualized vol floor (in USD) for risk-parity weights so strategies
# with near-zero variance don't dominate by receiving infinite weight.
_VOL_FLOOR_USD: float = 1.0


@dataclass(frozen=True, slots=True)
class AllocationResult:
    """Capital fraction assigned to each strategy_id, summing to <= 1.0."""

    fractions: dict[str, float]
    method: str = "equal_weight"

    def total(self) -> float:
        return sum(self.fractions.values())


def _cap_and_renormalize(
    raw: dict[str, float],
    strategies: tuple[StrategyProtocol, ...],
) -> dict[str, float]:
    """
    Apply per-strategy required_capital_fraction() caps then renormalize to
    sum <= 1.0.  Strategies absent from `raw` receive 0.0.
    """
    cap_map = {s.strategy_id: s.required_capital_fraction() for s in strategies}
    capped = {sid: min(w, cap_map.get(sid, 1.0)) for sid, w in raw.items()}
    total = sum(capped.values())
    if total <= 0.0:
        result = {s.strategy_id: 0.0 for s in strategies}
    else:
        result = {sid: w / total * min(total, 1.0) for sid, w in capped.items()}
    for s in strategies:
        result.setdefault(s.strategy_id, 0.0)
    return result


def equal_weight_allocate(
    strategies: tuple[StrategyProtocol, ...],
    enabled_ids: set[str],
) -> AllocationResult:
    """
    Split capital equally among enabled strategies, capped per-strategy by
    required_capital_fraction() and renormalized to sum to at most 1.0.

    Strategies not in enabled_ids (e.g. kill-switched) receive 0.0.
    """
    active = [s for s in strategies if s.strategy_id in enabled_ids]
    if not active:
        return AllocationResult(
            fractions={s.strategy_id: 0.0 for s in strategies},
            method="equal_weight",
        )

    equal_share = 1.0 / len(active)
    raw = {s.strategy_id: equal_share for s in active}
    return AllocationResult(fractions=_cap_and_renormalize(raw, strategies), method="equal_weight")


def performance_weighted_allocate(
    strategies: tuple[StrategyProtocol, ...],
    enabled_ids: set[str],
    *,
    metric: str = "sortino",
) -> AllocationResult:
    """
    Risk-adjusted capital allocation using live per-strategy attribution data.

    Algorithm
    ---------
    1. Pull `AttributionTracker.snapshot()` per strategy.
    2. Strategies with < _MIN_TRADES_FOR_SHARPE fills get the equal-weight
       share as their weight (insufficient data -- no edge to exploit).
    3. Strategies with >= _MIN_TRADES_FOR_SHARPE fills:
         - positive metric value -> proportional weight
         - negative metric value -> floor at _NEG_SHARPE_WEIGHT_FRACTION * equal_share
           (keeps them alive at low allocation during a drawdown rather than
           zeroing them out based on potentially noisy recent history)
    4. Weights are capped by required_capital_fraction() then renormalized.

    Parameters
    ----------
    metric : "sortino" (default), "sharpe", or "calmar".
        Sortino is preferred for crypto because it does not penalize
        upside volatility; Calmar is useful during trend regimes.
        Falls back to Sharpe if the requested metric is unavailable.

    Falls back to equal_weight_allocate if attribution data is unavailable.

    Authority: Sharpe (1966), Sortino & Price (1994), Carver (2019) Ch.11,
    AFML Ch.16.
    """
    from src.diagnostics.attribution import get_attribution_tracker

    if metric not in ("sortino", "sharpe", "calmar"):
        raise ValueError(f"metric must be 'sortino', 'sharpe', or 'calmar', got {metric!r}")

    active = [s for s in strategies if s.strategy_id in enabled_ids]
    if not active:
        return AllocationResult(
            fractions={s.strategy_id: 0.0 for s in strategies},
            method="performance_weighted",
        )

    equal_share = 1.0 / len(active)

    try:
        snapshot = get_attribution_tracker().snapshot()
    except Exception as exc:
        log.warning("allocator.attribution_snapshot_failed", error=str(exc), exc_info=True)
        return equal_weight_allocate(strategies, enabled_ids)

    raw: dict[str, float] = {}
    for s in active:
        attr = snapshot.get(s.strategy_id)
        if attr is None or attr.trade_count < _MIN_TRADES_FOR_SHARPE:
            # Warm-up phase - give equal share so new strategies get capital
            raw[s.strategy_id] = equal_share
        else:
            score = getattr(attr, metric, None) or attr.sharpe
            if score >= 0.0:
                raw[s.strategy_id] = score
            else:
                # Negative score - floor so the strategy isn't fully excluded
                raw[s.strategy_id] = _NEG_SHARPE_WEIGHT_FRACTION * equal_share

    # If all active strategies are in warm-up (all equal_share), the result is
    # identical to equal_weight - no normalization surprise.
    total_raw = sum(raw.values())
    if total_raw <= 0.0:
        return equal_weight_allocate(strategies, enabled_ids)

    # Scale weights to sum to 1.0 before capping (capping may reduce total below 1)
    normalized = {sid: w / total_raw for sid, w in raw.items()}

    log.debug(
        "allocator.performance_weighted",
        weights={sid: round(w, 4) for sid, w in normalized.items()},
    )
    return AllocationResult(
        fractions=_cap_and_renormalize(normalized, strategies),
        method="performance_weighted",
    )


def risk_parity_allocate(
    strategies: tuple[StrategyProtocol, ...],
    enabled_ids: set[str],
) -> AllocationResult:
    """
    Inverse-volatility capital allocation (Qian 2005 risk-parity).

    Each active strategy's weight is proportional to 1/std(P&L), so every
    strategy contributes equal expected P&L variance to the portfolio.
    Strategies without enough trade history fall back to the equal-weight
    share — identical warm-up guard to performance_weighted_allocate.

    Algorithm
    ---------
    1. Pull realized P&L series per strategy from AttributionTracker.
    2. Compute vol = std(pnl_usd) for strategies with >= _MIN_TRADES_FOR_VOL fills.
    3. inv_vol = 1 / max(vol, _VOL_FLOOR_USD) — floor prevents zero-vol
       strategies from dominating.
    4. Normalize inv_vol weights, cap by required_capital_fraction(), renorm.
    5. Fall back to equal_weight_allocate if attribution data is unavailable.

    Authority: Qian (2005), Carver (2019) Ch.11.
    """
    from src.diagnostics.attribution import get_attribution_tracker

    active = [s for s in strategies if s.strategy_id in enabled_ids]
    if not active:
        return AllocationResult(
            fractions={s.strategy_id: 0.0 for s in strategies},
            method="risk_parity",
        )

    equal_share = 1.0 / len(active)

    try:
        tracker = get_attribution_tracker()
        snapshot = tracker.snapshot()
        # Build per-strategy P&L series for vol computation. fills_for() is
        # the public accessor and reads only that strategy's retained window,
        # so this no longer scans every fill in the process for each caller.
        pnl_series: dict[str, list[float]] = {
            s.strategy_id: [f.pnl_usd for f in tracker.fills_for(s.strategy_id)] for s in active
        }
    except Exception as exc:
        log.warning("allocator.risk_parity_attribution_failed", error=str(exc), exc_info=True)
        return equal_weight_allocate(strategies, enabled_ids)

    raw: dict[str, float] = {}
    for s in active:
        pnls = pnl_series.get(s.strategy_id, [])
        attr = snapshot.get(s.strategy_id)
        n = attr.trade_count if attr is not None else 0
        if n < _MIN_TRADES_FOR_VOL or len(pnls) < 2:
            # Warm-up: give equal share so new strategies get capital.
            raw[s.strategy_id] = equal_share
        else:
            mean = sum(pnls) / len(pnls)
            var = sum((x - mean) ** 2 for x in pnls) / len(pnls)
            vol = math.sqrt(var)
            raw[s.strategy_id] = 1.0 / max(vol, _VOL_FLOOR_USD)

    total_raw = sum(raw.values())
    if total_raw <= 0.0:
        return equal_weight_allocate(strategies, enabled_ids)

    normalized = {sid: w / total_raw for sid, w in raw.items()}

    log.debug(
        "allocator.risk_parity",
        weights={sid: round(w, 4) for sid, w in normalized.items()},
    )
    return AllocationResult(
        fractions=_cap_and_renormalize(normalized, strategies),
        method="risk_parity",
    )
