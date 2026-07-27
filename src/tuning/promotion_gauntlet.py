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


def evaluate_gauntlet(
    observation: GauntletObservation, criteria: GauntletCriteria = GauntletCriteria()
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
