"""Residual branches in the paper and live executors.

Paper: the exit-leg slippage model and its fallbacks, and the duplicate-entry
guard. Live: the zero-fill refusal and partial-fill warning on close, the
duplicate-order suppression in _place_market_order, and the idempotency
registries both executors expose.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import ccxt
import pytest
from test_live_executor_coverage import _make_executor, _make_position

from src.execution.idempotency import DuplicateOrderError, IdempotencyRecord
from src.execution.paper import PaperPosition


# ---------------------------------------------------------------------------
# Paper: exit-leg slippage
# ---------------------------------------------------------------------------


def _paper_position(direction: int = 1, adv: float = 1_000_000.0, qty: float = 1.0):
    return PaperPosition(
        trade_id="t1",
        symbol="BTC/USDT",
        timeframe="15m",
        direction=direction,
        entry_price=50_000.0,
        quantity=qty,
        notional_usd=50_000.0,
        entry_ts=1_700_000_000_000,
        kelly_fraction=0.02,
        regime_at_entry=0,
        meta_label_prob=0.7,
        raw_signal=1.0,
        approved_by="auto",
        execution_mode="automatic",
        fee_usd=1.0,
        adv_20d_at_entry=adv,
        spread_bps_at_entry=2.0,
    )


def _paper_executor():
    from src.execution.paper import PaperExecutor

    executor = PaperExecutor.__new__(PaperExecutor)
    import structlog

    executor._log = structlog.get_logger("paper_test")
    return executor


class TestPaperExitSlippage:
    @pytest.mark.parametrize(
        ("position", "mark"),
        [
            (_paper_position(adv=0.0), 50_000.0),  # liquidity context unknown
            (_paper_position(qty=0.0), 50_000.0),  # nothing to close
            (_paper_position(), 0.0),  # no usable mark
        ],
    )
    def test_no_adjustment_without_a_usable_liquidity_context(self, position, mark):
        assert _paper_executor()._slipped_exit_price(mark, position) == mark

    def test_closing_a_long_fills_below_the_mark(self):
        price = _paper_executor()._slipped_exit_price(50_000.0, _paper_position(direction=1))
        assert price < 50_000.0

    def test_closing_a_short_fills_above_the_mark(self):
        price = _paper_executor()._slipped_exit_price(50_000.0, _paper_position(direction=0))
        assert price > 50_000.0

    def test_a_failing_slippage_model_falls_back_to_the_mark(self):
        with patch(
            "src.execution.paper.SlippageModel",
            side_effect=RuntimeError("no liquidity curve"),
        ):
            price = _paper_executor()._slipped_exit_price(50_000.0, _paper_position())

        assert price == 50_000.0


# ---------------------------------------------------------------------------
# Live: close-order fill accounting
# ---------------------------------------------------------------------------


def _close_order(**overrides) -> dict:
    base = {"id": "ord-close", "status": "closed", "filled": 0.1, "average": 50_500.0}
    return base | overrides


def _executor_with_position(order: dict):
    executor = _make_executor()
    position = _make_position()
    executor._positions = {position.trade_id: position}
    executor._place_market_order = AsyncMock(return_value=order)
    return executor, position


def test_a_close_order_that_filled_nothing_leaves_the_position_open():
    executor, _ = _executor_with_position(_close_order(filled=0.0))

    with pytest.raises(RuntimeError, match="filled 0"):
        asyncio.run(executor.close_position("trade-1", 50_500.0, "take_profit"))

    assert "trade-1" in executor._positions  # exposure is still live


def test_a_missing_fill_quantity_is_read_as_fully_filled():
    order = _close_order()
    order.pop("filled")
    executor, _ = _executor_with_position(order)

    asyncio.run(executor.close_position("trade-1", 50_500.0, "take_profit"))

    assert "trade-1" not in executor._positions


def test_a_partial_close_is_still_booked_but_flagged():
    executor, _ = _executor_with_position(_close_order(filled=0.05))

    asyncio.run(executor.close_position("trade-1", 50_500.0, "take_profit"))

    assert "trade-1" not in executor._positions


def test_closing_an_unknown_trade_is_an_error():
    executor = _make_executor()

    with pytest.raises(KeyError):
        asyncio.run(executor.close_position("nope", 50_000.0, "manual"))


# ---------------------------------------------------------------------------
# Live: duplicate order suppression
# ---------------------------------------------------------------------------


def test_a_duplicate_order_intent_is_refused_rather_than_resubmitted():
    executor = _make_executor()
    executor._fetcher.get_order_exchange = MagicMock(return_value=MagicMock(id="binance"))
    executor._await_throttle_token = AsyncMock(return_value=None)

    record = IdempotencyRecord(key="k1")
    record.order_id = "already-sent"
    executor._order_manager.place_order_with_fsm = AsyncMock(
        side_effect=DuplicateOrderError("k1", record)
    )

    with pytest.raises(ccxt.ExchangeError, match="Duplicate order suppressed"):
        asyncio.run(
            executor._place_market_order("BTC/USDT", "buy", 0.1, purpose="entry", intent_id="i-1")
        )


# ---------------------------------------------------------------------------
# Both executors expose their idempotency registry
# ---------------------------------------------------------------------------


def test_the_live_registry_is_the_order_manager_s():
    executor = _make_executor()

    assert executor.idempotency is executor._order_manager.idempotency


def test_the_paper_registry_is_the_one_entries_are_reserved_against():
    from src.execution.idempotency import IdempotencyRegistry
    from src.execution.paper import PaperExecutor

    executor = PaperExecutor.__new__(PaperExecutor)
    registry = IdempotencyRegistry()
    executor._idempotency = registry

    assert executor.idempotency is registry


def test_paper_refuses_a_second_identical_entry_intent(tmp_path):
    """Paper dedupes on the same key as live -- the same intent twice opens once."""
    import contextlib
    import os

    from src.data.storage import StorageBackend
    from src.execution.paper import PaperExecutor
    from src.risk.kelly import KellyResult

    db_path = str(tmp_path / "paper.db")

    async def _run() -> tuple[str | None, str | None]:
        storage = StorageBackend(db_path=db_path)
        await storage.initialize()
        executor = PaperExecutor(storage, starting_capital=100_000.0)
        await executor.initialize()
        kelly = KellyResult(
            kelly_fraction=0.04,
            adjusted_fraction=0.02,
            capital_usd=100_000.0,
            entry_price=50_000.0,
            quantity=0.1,
            notional_usd=5_000.0,
            is_capped=False,
        )
        common = dict(
            symbol="BTC/USDT",
            timeframe="15m",
            direction=1,
            kelly_result=kelly,
            regime_state=0,
            meta_label_prob=0.7,
            raw_signal=1.0,
            current_price=50_000.0,
            approved_by="auto",
        )
        try:
            first = await executor._open_position_internal(**common)
            second = await executor._open_position_internal(**common)
            return first, second
        finally:
            await storage.close()

    first, second = asyncio.run(_run())

    assert first is not None
    assert second is None  # the duplicate intent was suppressed

    for ext in ("", "-wal", "-shm"):
        with contextlib.suppress(FileNotFoundError):
            os.remove(db_path + ext)


class TestEmergencyFlatten:
    """The stranded-fill path: the order filled, the position could not be
    recorded, so the exposure is immediately flattened and the cash effect of
    the round trip is booked."""

    def _executor(self, order):
        executor = _make_executor(cash=5_100.0)
        executor._place_market_order = AsyncMock(return_value=order)
        return executor

    def _place(self, executor, direction: int):
        from test_live_executor_coverage import _kelly

        return asyncio.run(
            executor._place_and_record(
                symbol="BTC/USDT",
                timeframe="1h",
                direction=direction,
                kelly_result=_kelly(notional=5_000.0, qty=0.2, price=50_000.0),
                regime_state=0,
                meta_label_prob=0.7,
                raw_signal=1.0,
                approved_by="auto",
            )
        )

    def test_a_stranded_short_entry_books_the_flatten_the_other_way_round(self):
        order = {
            "id": "ord-1",
            "status": "closed",
            "filled": 0.2,
            "amount": 0.2,
            "average": 50_000.0,
            "fees": [{"currency": "USDT", "cost": 1.0}],
        }
        executor = self._executor(order)
        cash_before = executor._cash

        assert self._place(executor, direction=0) is None
        # entry and flatten filled at the same price, so only the two fees move
        assert executor._cash == pytest.approx(cash_before - 2.0)

    def test_a_flatten_that_the_exchange_refuses_leaves_the_fill_stranded(self):
        order = {
            "id": "ord-1",
            "status": "closed",
            "filled": 0.2,
            "amount": 0.2,
            "average": 50_000.0,
            "fees": [{"currency": "USDT", "cost": 1.0}],
        }
        executor = self._executor(order)
        calls = {"n": 0}

        async def _place(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                return order
            raise ccxt.ExchangeError("flatten rejected")

        executor._place_market_order = _place

        assert self._place(executor, direction=1) is None
        assert calls["n"] == 2  # the flatten was attempted


def test_an_approval_that_disappears_before_resolution_is_not_approved():
    """The event fires, but the queue entry is gone by the time the waiter
    reclaims it -- that is refused, never read as an approval."""
    executor = _make_executor()

    async def _run():
        request = MagicMock()
        request._event = asyncio.Event()
        request._event.set()

        class _RacyQueue(dict):
            """Hands out the request, then loses it -- another task popped it."""

            def pop(self, key, default=None):
                super().pop(key, default)
                return None

        executor._approval_queue = _RacyQueue({"req-1": request})
        return await executor._await_approval("req-1", 0.5)

    assert asyncio.run(_run()) == (False, "")
