"""
Abstract base class for trade executors (VUL-038).

Both LiveExecutor and PaperExecutor must implement this interface.
Replaces the unsafe cast(LiveExecutor, ...) pattern in api/main.py —
all API code should type against AbstractExecutor.
"""

from __future__ import annotations

from abc import ABC, abstractmethod


class AbstractExecutor(ABC):
    """Common interface shared by LiveExecutor and PaperExecutor."""

    @property
    @abstractmethod
    def equity_usd(self) -> float: ...

    @property
    @abstractmethod
    def cash_usd(self) -> float: ...

    @property
    @abstractmethod
    def starting_capital(self) -> float: ...

    @property
    @abstractmethod
    def starting_equity_usd(self) -> float:
        """
        Daily-start equity for drawdown gate calculation.

        NEW-009: Exposes drawdown_tracker.daily_start_equity through the
        AbstractExecutor interface, preventing orchestrator from reaching
        through the abstraction boundary to a concrete attribute.
        """
        ...

    @abstractmethod
    def open_positions(self) -> list[dict[str, object]]: ...

    @abstractmethod
    async def open_positions_safe(self) -> list[dict[str, object]]: ...

    @abstractmethod
    def pending_approvals(self) -> list[dict[str, object]]: ...

    @abstractmethod
    async def pending_approvals_safe(self) -> list[dict[str, object]]: ...

    @abstractmethod
    async def resolve_approval(
        self,
        request_id: str,
        approved: bool,
        operator: str,
    ) -> bool: ...

    @abstractmethod
    async def submit_signal(self, *args, **kwargs): ...

    @abstractmethod
    def position_count(self) -> int: ...

    @abstractmethod
    async def get_consecutive_losses(self, symbol: str) -> int: ...

    @abstractmethod
    async def get_daily_pnl(self, symbol: str) -> float: ...

    @abstractmethod
    async def reset_daily_equity(self) -> float:
        """
        Atomically read current equity, reset the drawdown tracker's daily_start,
        and return the equity value used for the reset.

        NEW-005: Callers (orchestrator midnight reset) must use this instead of
        accessing drawdown_tracker directly to avoid torn reads during concurrent
        mark_to_market calls.
        """
        ...

    @abstractmethod
    async def get_risk_snapshot(self) -> tuple[float, float, float]:
        """
        C-08: Atomically return (equity_usd, starting_equity_usd, daily_pnl_usd)
        under the executor lock, preventing torn reads in orchestrator._tick().
        """
        ...
