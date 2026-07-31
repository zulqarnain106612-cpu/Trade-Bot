"""
Macro exposure budget — cross-asset notional allocation control.

Prevents over-concentration in correlated asset groups by enforcing
per-group and global notional caps relative to total capital.

Two levels of control:

  1. Group budget  — the sum of |notional| across all positions in the
     same asset group (e.g. "BTC-correlated", "stablecoins", "alts")
     must stay within ``group_cap_pct`` of capital.

  2. Global budget — the sum of ALL |notional| across every group must
     stay within ``global_cap_pct`` of capital (gross exposure ceiling).

Usage:

    budget = MacroExposureBudget(capital_usd=100_000)
    budget.set_group_cap("BTC-correlated", 0.30)   # 30% of capital
    budget.update("BTC/USDT:USDT", "BTC-correlated", 15_000)
    result = budget.check("ETH/USDT:USDT", "BTC-correlated", 10_000)
    if result.allowed:
        ...

Authority:
  Roncalli (2013) "Introduction to Risk Parity and Budgeting" Ch.5 —
    group-level risk allocation and notional budgeting.
  Grinold & Kahn (2000) "Active Portfolio Management" — risk budget
    decomposition and exposure management.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Final

import structlog


log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

_DEFAULT_GLOBAL_CAP_PCT: Final[float] = 3.0  # gross leverage <= 3x
_DEFAULT_GROUP_CAP_PCT: Final[float] = 0.50  # 50% of capital per group
_EPS: Final[float] = 1e-9


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BudgetCheckResult:
    """Result of a pre-trade budget check."""

    allowed: bool
    symbol: str
    group: str
    requested_notional: float
    current_group_notional: float
    current_global_notional: float
    group_cap: float  # absolute USD cap for this group
    global_cap: float  # absolute USD global cap
    reason: str  # empty when allowed

    def to_dict(self) -> dict:
        return {
            "allowed": self.allowed,
            "symbol": self.symbol,
            "group": self.group,
            "requested_notional": round(self.requested_notional, 2),
            "current_group_notional": round(self.current_group_notional, 2),
            "current_global_notional": round(self.current_global_notional, 2),
            "group_cap": round(self.group_cap, 2),
            "global_cap": round(self.global_cap, 2),
            "reason": self.reason,
        }


# ---------------------------------------------------------------------------
# Main class
# ---------------------------------------------------------------------------


class MacroExposureBudget:
    """
    Cross-asset notional exposure budget enforcer.

    Parameters
    ----------
    capital_usd:
        Total trading capital. Used to compute absolute dollar caps.
    global_cap_pct:
        Maximum gross notional as a multiple of capital. Default 3.0 (3x).
    default_group_cap_pct:
        Default per-group cap as fraction of capital. Default 0.50 (50%).
    """

    def __init__(
        self,
        capital_usd: float,
        global_cap_pct: float = _DEFAULT_GLOBAL_CAP_PCT,
        default_group_cap_pct: float = _DEFAULT_GROUP_CAP_PCT,
    ) -> None:
        if capital_usd <= 0:
            raise ValueError(f"capital_usd must be positive, got {capital_usd}")
        if global_cap_pct <= 0:
            raise ValueError(f"global_cap_pct must be positive, got {global_cap_pct}")
        if not (0 < default_group_cap_pct <= global_cap_pct):
            raise ValueError(
                f"default_group_cap_pct must be in (0, global_cap_pct], "
                f"got {default_group_cap_pct}"
            )

        self._capital = capital_usd
        self._global_cap_pct = global_cap_pct
        self._default_group_cap_pct = default_group_cap_pct

        # symbol → (group, signed_notional)
        self._positions: dict[str, tuple[str, float]] = {}
        # group → override cap fraction (None = use default)
        self._group_caps: dict[str, float] = {}

        self._last_updated_ts: float = 0.0

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------

    def set_capital(self, capital_usd: float) -> None:
        """Update capital (e.g. after a deposit or loss event)."""
        if capital_usd <= 0:
            raise ValueError(f"capital_usd must be positive, got {capital_usd}")
        self._capital = capital_usd

    def set_group_cap(self, group: str, cap_pct: float) -> None:
        """
        Override the per-group cap fraction.

        Parameters
        ----------
        group:
            Asset group name (e.g. "BTC-correlated").
        cap_pct:
            Maximum |notional| for this group as a fraction of capital.
            E.g. 0.30 means at most 30% of capital.
        """
        if not (0 < cap_pct <= self._global_cap_pct):
            raise ValueError(
                f"cap_pct must be in (0, global_cap_pct={self._global_cap_pct}], " f"got {cap_pct}"
            )
        self._group_caps[group] = cap_pct

    # ------------------------------------------------------------------
    # Position tracking
    # ------------------------------------------------------------------

    def update(self, symbol: str, group: str, notional: float) -> None:
        """
        Record or update a position's notional exposure.

        Parameters
        ----------
        symbol:
            Instrument identifier (e.g. "BTC/USDT:USDT").
        group:
            Asset group this instrument belongs to.
        notional:
            Signed notional in USD. Positive = long, negative = short.
            Use 0 to remove a position.
        """
        if notional == 0.0:
            self._positions.pop(symbol, None)
        else:
            self._positions[symbol] = (group, notional)
        self._last_updated_ts = time.time()

    def remove(self, symbol: str) -> None:
        """Remove a closed position."""
        self._positions.pop(symbol, None)

    def clear(self) -> None:
        """Reset all positions."""
        self._positions.clear()

    # ------------------------------------------------------------------
    # Query
    # ------------------------------------------------------------------

    def _group_notional(self, group: str, exclude_symbol: str | None = None) -> float:
        return sum(
            abs(n)
            for sym, (g, n) in self._positions.items()
            if g == group and sym != exclude_symbol
        )

    def _global_notional(self, exclude_symbol: str | None = None) -> float:
        return sum(abs(n) for sym, (_, n) in self._positions.items() if sym != exclude_symbol)

    def _group_cap_usd(self, group: str) -> float:
        cap_pct = self._group_caps.get(group, self._default_group_cap_pct)
        return cap_pct * self._capital

    def _global_cap_usd(self) -> float:
        return self._global_cap_pct * self._capital

    def check(
        self,
        symbol: str,
        group: str,
        requested_notional: float,
    ) -> BudgetCheckResult:
        """
        Pre-trade check: would adding ``requested_notional`` breach any cap?

        If the symbol already has a position, it is excluded from the
        current tally so the check reflects the *net change* correctly.

        Parameters
        ----------
        symbol:
            Instrument to trade.
        group:
            Asset group the instrument belongs to.
        requested_notional:
            New (signed) notional to set for this symbol. The check uses
            the absolute value.

        Returns
        -------
        BudgetCheckResult
        """
        abs_req = abs(requested_notional)
        cur_group = self._group_notional(group, exclude_symbol=symbol)
        cur_global = self._global_notional(exclude_symbol=symbol)
        group_cap = self._group_cap_usd(group)
        global_cap = self._global_cap_usd()

        new_group = cur_group + abs_req
        new_global = cur_global + abs_req

        if new_group > group_cap + _EPS:
            reason = (
                f"group_cap_breach: group={group!r} "
                f"new_notional={new_group:.0f} > cap={group_cap:.0f}"
            )
            log.warning(
                "macro_budget.group_cap_breach", group=group, new_notional=new_group, cap=group_cap
            )
            return BudgetCheckResult(
                allowed=False,
                symbol=symbol,
                group=group,
                requested_notional=requested_notional,
                current_group_notional=cur_group,
                current_global_notional=cur_global,
                group_cap=group_cap,
                global_cap=global_cap,
                reason=reason,
            )

        if new_global > global_cap + _EPS:
            reason = f"global_cap_breach: " f"new_notional={new_global:.0f} > cap={global_cap:.0f}"
            log.warning("macro_budget.global_cap_breach", new_notional=new_global, cap=global_cap)
            return BudgetCheckResult(
                allowed=False,
                symbol=symbol,
                group=group,
                requested_notional=requested_notional,
                current_group_notional=cur_group,
                current_global_notional=cur_global,
                group_cap=group_cap,
                global_cap=global_cap,
                reason=reason,
            )

        return BudgetCheckResult(
            allowed=True,
            symbol=symbol,
            group=group,
            requested_notional=requested_notional,
            current_group_notional=cur_group,
            current_global_notional=cur_global,
            group_cap=group_cap,
            global_cap=global_cap,
            reason="",
        )

    # ------------------------------------------------------------------
    # Reporting
    # ------------------------------------------------------------------

    def summary(self) -> dict:
        """Return current utilisation across all groups and global."""
        groups: dict[str, dict] = {}
        for sym, (grp, notional) in self._positions.items():
            if grp not in groups:
                groups[grp] = {
                    "symbols": [],
                    "total_notional": 0.0,
                    "cap": self._group_cap_usd(grp),
                }
            groups[grp]["symbols"].append(sym)
            groups[grp]["total_notional"] += abs(notional)

        global_notional = self._global_notional()
        return {
            "capital_usd": self._capital,
            "global_notional": round(global_notional, 2),
            "global_cap": round(self._global_cap_usd(), 2),
            "global_utilisation_pct": round(
                global_notional / max(self._global_cap_usd(), _EPS) * 100, 1
            ),
            "groups": {
                g: {
                    "total_notional": round(v["total_notional"], 2),
                    "cap": round(v["cap"], 2),
                    "utilisation_pct": round(v["total_notional"] / max(v["cap"], _EPS) * 100, 1),
                    "symbols": sorted(v["symbols"]),
                }
                for g, v in groups.items()
            },
        }

    @property
    def capital(self) -> float:
        return self._capital

    @property
    def n_positions(self) -> int:
        return len(self._positions)


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_REGISTRY: MacroExposureBudget | None = None


def get_budget(capital_usd: float | None = None) -> MacroExposureBudget:
    """
    Return module-level singleton MacroExposureBudget.

    On first call, ``capital_usd`` must be provided.
    """
    global _REGISTRY
    if _REGISTRY is None:
        if capital_usd is None:
            raise RuntimeError("Must provide capital_usd on first call to get_budget()")
        _REGISTRY = MacroExposureBudget(capital_usd=capital_usd)
    elif capital_usd is not None:
        _REGISTRY.set_capital(capital_usd)
    return _REGISTRY
