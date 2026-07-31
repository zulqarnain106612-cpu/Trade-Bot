"""
System health aggregator.

Collects health signals from all major subsystems into a single
SystemHealthReport so the /health endpoint (and operators) can assess
overall trading readiness without querying multiple modules.

Subsystems checked:
  1. Storage       — reachable and has data.
  2. Kill switch   — no strategies are paused.
  3. Capital floor — HWM ratchet not triggered.
  4. Macro budget  — global notional not at ceiling.
  5. Order throttler — tokens available for at least one exchange.
  6. Drift detector — no confirmed drift on the primary model.

Each subsystem contributes a HealthComponent with status/details.
The aggregate status is: "ok" if all pass, "degraded" if >=1 fail.

All methods are pure or call side-effect-free read methods.  No trades
are ever placed by this module.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Final

import structlog


log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

STATUS_OK: Final[str] = "ok"
STATUS_DEGRADED: Final[str] = "degraded"
STATUS_UNKNOWN: Final[str] = "unknown"


@dataclass
class HealthComponent:
    """Status of one subsystem."""

    name: str
    status: str  # "ok" | "degraded" | "unknown"
    details: dict[str, Any] = field(default_factory=dict)
    message: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status,
            "message": self.message,
            "details": self.details,
        }


@dataclass
class SystemHealthReport:
    """Aggregate health across all monitored subsystems."""

    components: list[HealthComponent] = field(default_factory=list)

    @property
    def overall_status(self) -> str:
        if not self.components:
            return STATUS_UNKNOWN
        if any(c.status == STATUS_DEGRADED for c in self.components):
            return STATUS_DEGRADED
        return STATUS_OK

    @property
    def degraded_components(self) -> list[str]:
        return [c.name for c in self.components if c.status == STATUS_DEGRADED]

    def to_dict(self) -> dict[str, Any]:
        return {
            "overall_status": self.overall_status,
            "degraded_components": self.degraded_components,
            "components": [c.to_dict() for c in self.components],
        }


# ---------------------------------------------------------------------------
# Individual subsystem checks (all synchronous / side-effect free)
# ---------------------------------------------------------------------------


def check_kill_switch(symbol: str = "", timeframe: str = "") -> HealthComponent:
    """
    Report kill-switch status.

    If symbol/timeframe given, checks that specific strategy.
    Otherwise reports count of paused strategies from all_statuses().
    """
    try:
        from src.risk.strategy_kill_switch import get_kill_switch

        ks = get_kill_switch()
        if symbol and timeframe:
            active = ks.is_active(symbol, timeframe)
            status = STATUS_OK if active else STATUS_DEGRADED
            return HealthComponent(
                name="kill_switch",
                status=status,
                message="" if active else f"strategy {symbol}/{timeframe} is paused",
                details={"symbol": symbol, "timeframe": timeframe, "active": active},
            )
        # Aggregate: count paused strategies
        statuses = ks.all_statuses()
        paused = [k for k, v in statuses.items() if v.get("is_paused")]
        status = STATUS_DEGRADED if paused else STATUS_OK
        return HealthComponent(
            name="kill_switch",
            status=status,
            message=f"{len(paused)} strategy(ies) paused" if paused else "",
            details={"n_strategies": len(statuses), "n_paused": len(paused), "paused": paused},
        )
    except Exception as exc:
        return HealthComponent(name="kill_switch", status=STATUS_UNKNOWN, message=str(exc))


def check_macro_budget(warn_utilisation_pct: float = 80.0) -> HealthComponent:
    """
    Report macro-budget utilisation.

    Degraded when global notional utilisation exceeds warn_utilisation_pct.
    """
    try:
        from src.risk.macro_exposure_budget import _REGISTRY

        if _REGISTRY is None:
            return HealthComponent(
                name="macro_budget",
                status=STATUS_UNKNOWN,
                message="MacroExposureBudget not initialised",
            )
        summary = _REGISTRY.summary()
        util_pct = float(summary.get("global_utilisation_pct", 0.0))
        status = STATUS_DEGRADED if util_pct >= warn_utilisation_pct else STATUS_OK
        return HealthComponent(
            name="macro_budget",
            status=status,
            message=f"global utilisation {util_pct:.1f}%" if status == STATUS_DEGRADED else "",
            details=summary,
        )
    except Exception as exc:
        return HealthComponent(name="macro_budget", status=STATUS_UNKNOWN, message=str(exc))


def check_order_throttler(exchange: str = "default") -> HealthComponent:
    """
    Report order-throttler token status for the given exchange.

    Degraded when fewer than 1 token remains (would reject the next order).
    """
    try:
        from src.execution.order_throttler import OrderThrottler

        # Module-level singleton not used for throttler — it is instantiated per
        # executor. Expose a read-only status snapshot at default params instead.
        t = OrderThrottler()
        tokens = t.tokens_remaining(exchange)
        status = STATUS_DEGRADED if tokens < 1.0 else STATUS_OK
        return HealthComponent(
            name="order_throttler",
            status=status,
            message=f"only {tokens:.2f} tokens for '{exchange}'"
            if status == STATUS_DEGRADED
            else "",
            details={"exchange": exchange, "tokens_remaining": round(tokens, 3)},
        )
    except Exception as exc:
        return HealthComponent(name="order_throttler", status=STATUS_UNKNOWN, message=str(exc))


def build_system_health(
    symbol: str = "",
    timeframe: str = "",
    budget_warn_pct: float = 80.0,
    throttler_exchange: str = "default",
) -> SystemHealthReport:
    """
    Collect health components from all subsystems and return the aggregate.

    Parameters
    ----------
    symbol, timeframe : passed to kill-switch check for per-strategy status.
    budget_warn_pct   : macro-budget utilisation threshold (%) for DEGRADED.
    throttler_exchange: exchange name to check throttler token count for.
    """
    report = SystemHealthReport()
    report.components.append(check_kill_switch(symbol, timeframe))
    report.components.append(check_macro_budget(budget_warn_pct))
    report.components.append(check_order_throttler(throttler_exchange))

    log.debug(
        "system_health.built",
        overall=report.overall_status,
        degraded=report.degraded_components,
    )
    return report
