"""
Strategy promotion gauntlet — v6 Autonomous Research & Strategy Discovery.

Any auto-discovered strategy candidate (from factor_search.py or a future
generation mechanism) must pass this gauntlet in paper trading before it
is eligible for registration in the live StrategyRegistry (v2). Mirrors
the same discipline already applied to model promotion (v4's
ModelRegistry) and strategy kill-switch re-enable (v2) — auto-discovery
never auto-promotes to live capital.

Authority:
  - López de Prado (2018) AFML Ch.11 — backtest overfitting; a discovered
    factor/strategy must prove itself out-of-sample under real paper
    trading conditions, not just a historical replay
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from src.diagnostics.attribution import AttributedFill, compute_attribution

_MS_PER_DAY = 86_400_000.0


@dataclass(frozen=True, slots=True)
class GauntletCriteria:
    """Minimum bar a candidate strategy must clear before promotion."""

    min_trades: int = 30
    min_days_running: int = 14
    min_sharpe: float = 0.5
    max_drawdown_pct: float = 0.20


@dataclass(frozen=True, slots=True)
class GauntletObservation:
    """Candidate strategy's actual paper-trading track record so far."""

    trade_count: int
    days_running: float
    realized_sharpe: float
    realized_max_drawdown_pct: float


@dataclass(frozen=True, slots=True)
class GauntletResult:
    passed: bool
    failed_criteria: tuple[str, ...]


# Frozen and slotted, so one shared instance is safe -- and evaluating it at
# import time rather than per call keeps the default out of the signature.
_DEFAULT_CRITERIA = GauntletCriteria()


def evaluate_gauntlet(
    observation: GauntletObservation, criteria: GauntletCriteria = _DEFAULT_CRITERIA
) -> GauntletResult:
    """
    Pure evaluation: a candidate must clear every criterion simultaneously.
    Returns which criteria failed (if any) for a clear promotion/rejection
    audit trail — never partial credit.
    """
    failed: list[str] = []

    if observation.trade_count < criteria.min_trades:
        failed.append(f"trade_count {observation.trade_count} < min_trades {criteria.min_trades}")
    if observation.days_running < criteria.min_days_running:
        failed.append(
            f"days_running {observation.days_running} < min_days_running "
            f"{criteria.min_days_running}"
        )
    if observation.realized_sharpe < criteria.min_sharpe:
        failed.append(
            f"realized_sharpe {observation.realized_sharpe:.3f} < min_sharpe {criteria.min_sharpe}"
        )
    if observation.realized_max_drawdown_pct > criteria.max_drawdown_pct:
        failed.append(
            f"realized_max_drawdown_pct {observation.realized_max_drawdown_pct:.3f} > "
            f"max_drawdown_pct {criteria.max_drawdown_pct}"
        )

    return GauntletResult(passed=len(failed) == 0, failed_criteria=tuple(failed))


def observation_from_fills(
    strategy_id: str,
    fills: list[AttributedFill],
    equity_usd: float,
    now_ms: int | None = None,
    first_entry_ms: int | None = None,
    lifetime_trade_count: int | None = None,
) -> GauntletObservation:
    """
    Build a GauntletObservation from a candidate's realized paper fills.

    `days_running` is measured from the earliest entry to *now*, not to the
    last exit: a candidate that traded twice on day one and then went quiet
    has still been running, and crediting it only two days would let it
    re-clear the min_days_running bar forever.

    `first_entry_ms` and `lifetime_trade_count` override what `fills` alone
    can show. The attribution tracker retains a bounded window, so once a
    long-lived strategy's oldest fills age out, deriving these from `fills`
    would reset its apparent age and shrink its trade count — letting a
    candidate that already failed the bar quietly re-enter the running.
    Callers holding the tracker should pass both.

    `realized_max_drawdown_pct` converts the attribution tracker's USD
    drawdown against `equity_usd`. A non-positive equity yields 1.0 (100%
    drawdown) rather than a division error or a flattering 0.0 — the
    gauntlet must fail closed when it cannot measure the denominator.
    """
    attribution = compute_attribution(strategy_id, fills)
    own_fills = [f for f in fills if f.strategy_id == strategy_id]

    first_entry = first_entry_ms
    if first_entry is None and own_fills:
        first_entry = min(f.entry_ts for f in own_fills)

    if first_entry is None:
        days_running = 0.0
    else:
        now = now_ms if now_ms is not None else int(datetime.now(tz=UTC).timestamp() * 1000)
        days_running = max(0.0, (now - first_entry) / _MS_PER_DAY)

    drawdown_pct = 1.0 if equity_usd <= 0.0 else attribution.max_drawdown_usd / equity_usd

    return GauntletObservation(
        trade_count=(
            lifetime_trade_count if lifetime_trade_count is not None else attribution.trade_count
        ),
        days_running=days_running,
        realized_sharpe=attribution.sharpe,
        realized_max_drawdown_pct=drawdown_pct,
    )
