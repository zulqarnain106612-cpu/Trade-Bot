"""
A write that raises must not release the lock with a transaction open.

BUGFIX-001 in insert_trade established the failure: a statement that raises
inside the write lock leaves the shared aiosqlite connection holding an open
transaction, which deadlocks every subsequent write -- including the WAL
checkpoint on close(). The fix was applied at exactly one of the eleven
write sites.

update_trade_exit is its direct twin and had no handler at all, so any
UPDATE failure (locked, disk full, constraint) stranded the connection and
took the whole storage layer down with it. _write_ctx now holds the lock and
rolls back on the way out for every write path.
"""

from __future__ import annotations

import pytest

from src.data.storage import StorageBackend


class _Conn:
    """Minimal stand-in for the shared aiosqlite connection."""

    def __init__(self) -> None:
        self.rollbacks = 0
        self.commits = 0

    async def rollback(self) -> None:
        self.rollbacks += 1

    async def commit(self) -> None:
        self.commits += 1


@pytest.fixture
def backend(tmp_path):
    b = StorageBackend(str(tmp_path / "t.db"))
    b._conn = _Conn()
    return b


async def test_an_exception_inside_the_write_rolls_back(backend) -> None:
    with pytest.raises(RuntimeError):
        async with backend._write_ctx():
            raise RuntimeError("statement failed")

    assert backend._conn.rollbacks == 1


async def test_the_exception_still_propagates(backend) -> None:
    # Swallowing it would be worse than the deadlock: the caller would
    # believe the write succeeded.
    with pytest.raises(ValueError, match="boom"):
        async with backend._write_ctx():
            raise ValueError("boom")


async def test_a_clean_write_does_not_roll_back(backend) -> None:
    async with backend._write_ctx():
        await backend._conn.commit()

    assert backend._conn.rollbacks == 0
    assert backend._conn.commits == 1


async def test_cancellation_also_rolls_back(backend) -> None:
    # CancelledError inherits from BaseException, not Exception; a shutdown
    # mid-write must not strand the connection either.
    import asyncio

    with pytest.raises(asyncio.CancelledError):
        async with backend._write_ctx():
            raise asyncio.CancelledError()

    assert backend._conn.rollbacks == 1


async def test_the_lock_is_released_after_a_failure(backend) -> None:
    # The deadlock this guards against is only visible if the next writer can
    # actually acquire the lock afterwards.
    with pytest.raises(RuntimeError):
        async with backend._write_ctx():
            raise RuntimeError("first write failed")

    async with backend._write_ctx():
        await backend._conn.commit()

    assert backend._conn.commits == 1


async def test_nested_handlers_rolling_back_twice_is_harmless(backend) -> None:
    # insert_trade rolls back itself before raising ValueError; rollback on a
    # connection with no open transaction is a no-op, so the outer guard is
    # safe to layer on top.
    with pytest.raises(ValueError):
        async with backend._write_ctx():
            await backend._conn.rollback()
            raise ValueError("duplicate id")

    assert backend._conn.rollbacks == 2
