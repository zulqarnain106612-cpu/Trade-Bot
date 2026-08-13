"""
Abstract base class for trade executors (VUL-038).

Both LiveExecutor and PaperExecutor must implement this interface.
Replaces the unsafe cast(LiveExecutor, ...) pattern in api/main.py —
all API code should type against AbstractExecutor.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from src.execution.idempotency import IdempotencyRegistry


class AbstractExecutor(ABC):
    """Common interface shared by LiveExecutor and PaperExecutor."""

    @property
    @abstractmethod
    def idempotency(self) -> IdempotencyRegistry:
        """
        Registry of order idempotency keys this executor has submitted (LAW3).

        Part of the abstract interface rather than a live-only detail: paper
        and live must dedupe identically, or a duplicate-submission bug hides
        in paper trading and only appears with real money. It is exposed so
        reconciliation and recovery paths can ask "did this intent already go
        out?" without reaching into a concrete executor.
        """
        ...

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
    async def submit_signal(self, *args, **kwargs) -> tuple[str | None, str]: ...

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

    @abstractmethod
    async def mark_to_market(self, prices: dict[str, float]) -> float:
        """
        Update unrealized PnL for all open positions given latest prices.

        GAP-013: added to the abstract interface so the orchestrator's
        position-monitor loop can call this through AbstractExecutor without
        an unsafe cast to a concrete executor type.
        """
        ...

    @abstractmethod
    async def close_position(
        self,
        trade_id: str,
        exit_price: float,
        exit_reason: str,
    ) -> float:
        """
        Close an open position at the given price, returning realized net PnL.

        GAP-013: added to the abstract interface for the same reason as
        mark_to_market above -- the position-monitor loop needs to close
        positions that trip a stop-loss / take-profit / time-exit condition.
        """
        ...
