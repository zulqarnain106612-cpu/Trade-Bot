"""
Macro-conditioned exposure budget — v7 Portfolio-Level Macro Overlay.

Scales aggregate portfolio exposure with macro regime confidence
(src/intelligence/macro_regime.py), layered strictly *underneath* the
existing Kelly ceiling — this budget can only shrink exposure below what
Kelly already allows, never expand beyond it (Domain Prior: Kelly is a
ceiling, not a target, and this module must not become a backdoor around
that ceiling).

Authority:
  - Domain Prior: Kelly is a ceiling, not a target; enforce drawdown and
    position limits — this module composes with, never replaces, Kelly.
"""

from __future__ import annotations

from dataclasses import dataclass

from src.intelligence.macro_regime import MacroRegimeResult

_MIN_BUDGET_SCALAR: float = 0.25  # Even at max risk-off, never fully zero out
_MAX_BUDGET_SCALAR: float = 1.0  # Never scale ABOVE the Kelly-derived size


@dataclass(frozen=True, slots=True)
class MacroExposureBudget:
    """Multiplicative scalar derived from macro risk appetite."""

    scalar: float
    reason: str


def compute_macro_exposure_scalar(macro: MacroRegimeResult) -> MacroExposureBudget:
    """
    Linear map from risk_appetite in [-1, 1] to a scalar in
    [_MIN_BUDGET_SCALAR, _MAX_BUDGET_SCALAR]. Never exceeds 1.0 — this
    function can only shrink Kelly-derived sizing, never amplify it.
    """
    # risk_appetite=-1 -> _MIN_BUDGET_SCALAR; risk_appetite=+1 -> _MAX_BUDGET_SCALAR
    normalized = (macro.risk_appetite + 1.0) / 2.0  # [0, 1]
    scalar = _MIN_BUDGET_SCALAR + normalized * (_MAX_BUDGET_SCALAR - _MIN_BUDGET_SCALAR)
    scalar = max(_MIN_BUDGET_SCALAR, min(_MAX_BUDGET_SCALAR, scalar))

    return MacroExposureBudget(
        scalar=scalar,
        reason=(
            f"macro regime={macro.regime.value} risk_appetite={macro.risk_appetite:.3f} "
            f"-> exposure_scalar={scalar:.3f}"
        ),
    )


def apply_macro_budget_to_kelly_fraction(
    kelly_fraction: float, budget: MacroExposureBudget
) -> float:
    """
    Applies the macro scalar to an already-computed Kelly fraction.
    Guaranteed <= kelly_fraction since budget.scalar <= 1.0 by construction.
    """
    if kelly_fraction < 0.0:
        raise ValueError(f"kelly_fraction must be non-negative, got {kelly_fraction}")
    return kelly_fraction * budget.scalar


# ---------------------------------------------------------------------------
# Group / global notional budget
#
# /risk/size-check answers "may this notional be added?", which needs the book
# as it stands now, not just the macro scalar above. Exposure is read from the
# unified ledger (the same book /ledger reports) rather than tracked here, so
# there is one source of truth for what is open and no state to fall behind.
# ---------------------------------------------------------------------------

_MAX_GROUP_NOTIONAL_PCT: float = 0.40  # per asset group, as a fraction of capital
_MAX_GLOBAL_NOTIONAL_PCT: float = 1.00  # whole book, as a fraction of capital

# Which asset group a symbol belongs to. Anything unlisted is "other", which
# shares the same per-group ceiling rather than escaping it.
_SYMBOL_GROUPS: dict[str, str] = {
    "BTC/USDT": "crypto_large_cap",
    "ETH/USDT": "crypto_large_cap",
    "SOL/USDT": "crypto_mid_cap",
    "BNB/USDT": "crypto_mid_cap",
}


def group_for_symbol(symbol: str) -> str:
    """The asset group a symbol is budgeted under."""
    return _SYMBOL_GROUPS.get(symbol.upper(), "other")


@dataclass(frozen=True, slots=True)
class BudgetCheck:
    """Verdict on adding `requested_notional` to `group`."""

    allowed: bool
    group: str
    symbol: str
    requested_notional: float
    current_group_notional: float
    current_global_notional: float
    group_limit: float
    global_limit: float
    reason: str


class MacroExposureBudgetChecker:
    """
    Per-group and whole-book notional ceilings, both scaled off capital.

    Like the macro scalar above, this can only refuse exposure — it never
    authorises a size that some other limit has already rejected.
    """

    def __init__(
        self,
        capital_usd: float,
        max_group_pct: float = _MAX_GROUP_NOTIONAL_PCT,
        max_global_pct: float = _MAX_GLOBAL_NOTIONAL_PCT,
    ) -> None:
        if capital_usd <= 0:
            raise ValueError(f"capital_usd must be positive, got {capital_usd}")
        self._capital = capital_usd
        self._group_limit = capital_usd * max_group_pct
        self._global_limit = capital_usd * max_global_pct

    def _open_notionals(self, group: str) -> tuple[float, float]:
        """(group notional, global notional) currently open, in USD."""
        from src.execution.unified_ledger import get_unified_ledger

        group_total = 0.0
        global_total = 0.0
        for position in get_unified_ledger().all_positions:
            notional = abs(position.quantity) * position.entry_price
            global_total += notional
            if group_for_symbol(position.symbol) == group:
                group_total += notional
        return group_total, global_total

    def check(
        self,
        *,
        symbol: str,
        group: str,
        requested_notional: float,
    ) -> BudgetCheck:
        """Whether `requested_notional` fits under both ceilings."""
        current_group, current_global = self._open_notionals(group)

        reason = ""
        if requested_notional <= 0:
            reason = f"non_positive_notional={requested_notional}"
        elif current_group + requested_notional > self._group_limit:
            reason = (
                f"group_limit_exceeded: {group} would hold "
                f"{current_group + requested_notional:.2f} > {self._group_limit:.2f}"
            )
        elif current_global + requested_notional > self._global_limit:
            reason = (
                f"global_limit_exceeded: book would hold "
                f"{current_global + requested_notional:.2f} > {self._global_limit:.2f}"
            )

        return BudgetCheck(
            allowed=not reason,
            group=group,
            symbol=symbol,
            requested_notional=requested_notional,
            current_group_notional=current_group,
            current_global_notional=current_global,
            group_limit=self._group_limit,
            global_limit=self._global_limit,
            reason=reason,
        )


def get_budget(capital_usd: float) -> MacroExposureBudgetChecker:
    """Budget checker sized for this account's capital."""
    return MacroExposureBudgetChecker(capital_usd=capital_usd)
