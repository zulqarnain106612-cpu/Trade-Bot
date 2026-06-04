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
