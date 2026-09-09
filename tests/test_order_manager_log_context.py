"""An order's log lines must name the order, and stop naming it afterwards.

order_manager and router both log through structlog, and nothing said which
order a line belonged to. With several orders in flight -- and the FSM retry
loop logging per attempt -- the lines interleave and cannot be separated.

The idempotency key is already the order's stable identity (it is the
venue's client order id), so binding it is a naming change rather than a new
concept. It composes with the tick's trace_id: a fill can be traced back to
the signal that asked for it.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
import structlog

from src.execution.order_fsm import OrderFSMError
from src.execution.order_manager import OrderManager


@pytest.fixture(autouse=True)
def _clean_context():
    structlog.contextvars.clear_contextvars()
    yield
    structlog.contextvars.clear_contextvars()


def _manager_capturing_context(seen: list[dict]) -> OrderManager:
    manager = OrderManager()

    async def _body(*_args, **_kwargs):
        seen.append(dict(structlog.contextvars.get_contextvars()))
        return MagicMock(), {}

    manager._place_order_with_fsm = _body  # type: ignore[method-assign]
    return manager


async def test_order_key_is_bound_for_the_submission() -> None:
    seen: list[dict] = []
    manager = _manager_capturing_context(seen)

    await manager.place_order_with_fsm(
        MagicMock(), "BTC/USDT", "buy", 1.0, idempotency_key="key-abc"
    )

    assert seen == [{"order_key": "key-abc"}]


async def test_order_key_is_unbound_afterwards() -> None:
    manager = _manager_capturing_context([])

    await manager.place_order_with_fsm(
        MagicMock(), "BTC/USDT", "buy", 1.0, idempotency_key="key-abc"
    )

    assert "order_key" not in structlog.contextvars.get_contextvars()


async def test_order_key_is_unbound_when_the_submission_raises() -> None:
    manager = OrderManager()

    async def _boom(*_args, **_kwargs):
        raise RuntimeError("exchange down")

    manager._place_order_with_fsm = _boom  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="exchange down"):
        await manager.place_order_with_fsm(
            MagicMock(), "BTC/USDT", "buy", 1.0, idempotency_key="key-abc"
        )

    assert "order_key" not in structlog.contextvars.get_contextvars()


async def test_the_surrounding_trace_id_survives() -> None:
    """unbind, not clear: the tick's trace_id must outlive the order."""
    seen: list[dict] = []
    manager = _manager_capturing_context(seen)
    structlog.contextvars.bind_contextvars(trace_id="tick-1")

    await manager.place_order_with_fsm(
        MagicMock(), "BTC/USDT", "buy", 1.0, idempotency_key="key-abc"
    )

    assert seen == [{"trace_id": "tick-1", "order_key": "key-abc"}]
    assert structlog.contextvars.get_contextvars() == {"trace_id": "tick-1"}


async def test_validation_failures_bind_nothing() -> None:
    """The guards run before the bind, so a rejected order leaves no context."""
    manager = OrderManager()
    manager._place_order_with_fsm = AsyncMock()  # type: ignore[method-assign]

    with pytest.raises(OrderFSMError):
        await manager.place_order_with_fsm(MagicMock(), "BTC/USDT", "buy", 1.0, idempotency_key="")

    assert "order_key" not in structlog.contextvars.get_contextvars()
