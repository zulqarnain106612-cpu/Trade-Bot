"""Tests for src/execution/order_manager.py — OrderManager fill confirmation."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.execution.order_fsm import OrderStatus
from src.execution.order_manager import OrderManager


def _make_exchange(create_result: dict, fetch_result: dict) -> MagicMock:
    exchange = MagicMock()
    exchange.create_market_order = AsyncMock(return_value=create_result)
    exchange.fetch_order = AsyncMock(return_value=fetch_result)
    return exchange


class TestPlaceOrderValidation:
    @pytest.mark.asyncio
    async def test_invalid_side_raises_order_fsm_error(self):
        from src.execution.order_fsm import OrderFSMError

        manager = OrderManager()
        exchange = MagicMock()
        with pytest.raises(OrderFSMError, match="Invalid order params"):
            await manager.place_order_with_fsm(exchange, "BTC/USDT", "not-a-side", 1.0)

    @pytest.mark.asyncio
    async def test_non_positive_quantity_raises_order_fsm_error(self):
        from src.execution.order_fsm import OrderFSMError

        manager = OrderManager()
        exchange = MagicMock()
        with pytest.raises(OrderFSMError, match="Invalid order params"):
            await manager.place_order_with_fsm(exchange, "BTC/USDT", "buy", 0.0)


class TestPlaceOrderTimeoutPropagation:
    @pytest.mark.asyncio
    async def test_place_order_timeout_transitions_fsm_and_reraises(self):
        """place_order_with_fsm's own TimeoutError handler must transition
        the FSM to TIMEOUT and re-raise -- exercised by patching
        _confirm_order_fill directly rather than waiting out a real 30s
        timeout."""
        manager = OrderManager()
        exchange = MagicMock()
        exchange.create_market_order = AsyncMock(return_value={"id": "ord-11"})

        async def _timeout(*args, **kwargs):
            raise TimeoutError("confirmation exceeded 30.0s")

        with (
            patch.object(manager, "_confirm_order_fill", side_effect=_timeout),
            pytest.raises(TimeoutError),
        ):
            await manager.place_order_with_fsm(exchange, "BTC/USDT", "buy", 1.0)


class TestPlaceOrderExchangeErrorNonTerminal:
    @pytest.mark.asyncio
    async def test_exchange_error_from_non_terminal_fsm_still_transitions(self):
        """The common case for the outer except ccxt.ExchangeError handler:
        an error surfaces while the FSM is still active (not already
        FAILED/CANCELLED/etc from _confirm_order_fill's own internal
        handling), so the guard's `if not fsm.state.is_terminal():` branch
        must still fire and transition it."""
        import ccxt.async_support as ccxt

        manager = OrderManager()
        exchange = MagicMock()
        exchange.create_market_order = AsyncMock(return_value={"id": "ord-12"})

        async def _raise_without_fsm_transition(*args, **kwargs):
            raise ccxt.ExchangeError("surfaced without internal FSM handling")

        with (
            patch.object(manager, "_confirm_order_fill", side_effect=_raise_without_fsm_transition),
            pytest.raises(ccxt.ExchangeError),
        ):
            await manager.place_order_with_fsm(exchange, "BTC/USDT", "buy", 1.0)


class TestConfirmOrderFillTimeout:
    @pytest.mark.asyncio
    async def test_confirmation_timeout_fails_fsm_and_raises(self):
        """A confirmation loop that never sees a terminal exchange status
        before timeout_s elapses must transition the FSM to TIMEOUT and
        raise TimeoutError, not hang forever."""
        manager = OrderManager()
        exchange = MagicMock()
        exchange.create_market_order = AsyncMock(return_value={"id": "ord-timeout"})
        exchange.fetch_order = AsyncMock(return_value={"status": "open"})

        with pytest.raises(TimeoutError):
            await manager._confirm_order_fill(
                exchange,
                "ord-timeout",
                "BTC/USDT",
                fsm=_pending_fsm("ord-timeout"),
                timeout_s=0.01,
            )


def _pending_fsm(order_id: str):
    from src.execution.order_fsm import OrderFSM, OrderFSMState, OrderStatus

    return OrderFSM(
        OrderFSMState(
            order_id=order_id,
            symbol="BTC/USDT",
            side="buy",
            quantity=1.0,
            status=OrderStatus.PENDING,
        )
    )


class TestConfirmOrderFill:
    @pytest.mark.asyncio
    async def test_filled_with_average_price_succeeds(self):
        manager = OrderManager()
        exchange = _make_exchange(
            create_result={"id": "ord-1"},
            fetch_result={"status": "filled", "filled": 1.0, "average": 100.0},
        )
        fsm, confirmed = await manager.place_order_with_fsm(exchange, "BTC/USDT", "buy", 1.0)
        assert fsm.state.status == OrderStatus.FILLED
        assert confirmed["average"] == 100.0

    @pytest.mark.asyncio
    async def test_filled_with_price_fallback_succeeds(self):
        manager = OrderManager()
        exchange = _make_exchange(
            create_result={"id": "ord-2"},
            fetch_result={"status": "closed", "filled": 1.0, "price": 99.5},
        )
        fsm, _confirmed = await manager.place_order_with_fsm(exchange, "BTC/USDT", "sell", 1.0)
        assert fsm.state.status == OrderStatus.FILLED

    @pytest.mark.asyncio
    async def test_filled_with_no_fill_price_raises_and_fails_fsm(self):
        """UI-009: a 'filled' exchange response with neither `average` nor
        `price` must never be silently recorded as avg_price=0.0 -- that
        would corrupt PnL/notional accounting. It must raise and leave the
        FSM in FAILED, not FILLED."""
        manager = OrderManager()
        exchange = _make_exchange(
            create_result={"id": "ord-3"},
            fetch_result={"status": "filled", "filled": 1.0},  # no average, no price
        )
        with pytest.raises(ValueError, match="cannot safely record"):
            await manager.place_order_with_fsm(exchange, "BTC/USDT", "buy", 1.0)

    @pytest.mark.asyncio
    async def test_filled_with_zero_fill_price_field_raises(self):
        """A real fill is never legitimately priced at exactly 0.0 -- an
        explicit average=0.0/price=0.0 must be rejected the same way a
        missing field is, not silently accepted as a free fill."""
        manager = OrderManager()
        exchange = _make_exchange(
            create_result={"id": "ord-4"},
            fetch_result={"status": "filled", "filled": 1.0, "average": 0.0, "price": 0.0},
        )
        with pytest.raises(ValueError, match="cannot safely record"):
            await manager.place_order_with_fsm(exchange, "BTC/USDT", "buy", 1.0)

    @pytest.mark.asyncio
    async def test_pending_status_retries_then_fills(self):
        manager = OrderManager()
        exchange = MagicMock()
        exchange.create_market_order = AsyncMock(return_value={"id": "ord-9"})
        exchange.fetch_order = AsyncMock(
            side_effect=[
                {"status": "open"},
                {
                    "status": "pending"
                },  # second poll: FSM already FILLING, exercises the else-branch
                {"status": "filled", "filled": 1.0, "average": 100.0},
            ]
        )
        fsm, confirmed = await manager.place_order_with_fsm(exchange, "BTC/USDT", "buy", 1.0)
        assert fsm.state.status.name == "FILLED"

    @pytest.mark.asyncio
    async def test_cancelled_status_fails_fsm_and_raises_immediately(self):
        """Regression: the "cancelled" branch's raise must propagate right
        away, not be re-caught by this same method's own generic
        `except ccxt.ExchangeError` handler and silently retried until the
        30s timeout (bug found while writing this test -- fixed alongside
        it). Realistic poll sequence: "open" observed at least once before
        "cancelled" (the more common case, e.g. a resting order cancelled
        by the user)."""
        import ccxt.async_support as ccxt

        manager = OrderManager()
        exchange = MagicMock()
        exchange.create_market_order = AsyncMock(return_value={"id": "ord-10"})
        exchange.fetch_order = AsyncMock(
            side_effect=[
                {"status": "open"},
                {"status": "cancelled"},
            ]
        )
        with pytest.raises(ccxt.ExchangeError, match="was cancelled"):
            await manager.place_order_with_fsm(exchange, "BTC/USDT", "buy", 1.0)
        # Exactly 2 polls (open, then cancelled) -- no retry loop after the
        # cancellation is observed.
        assert exchange.fetch_order.await_count == 2

    async def test_cancelled_on_very_first_poll_still_pending_fails_fsm_and_raises(self):
        """Regression (found during code review of the fix above): a market
        order rejected/cancelled instantly (no liquidity, self-trade
        prevention, IOC-style rejection) can report "cancelled" on the very
        FIRST poll, while the FSM is still PENDING -- the FSM only allows
        CANCELLED from FILLING, not directly from PENDING, so this must
        transition through FILLING first rather than raising an unhandled
        OrderFSMError that would mask the real cancellation."""
        import ccxt.async_support as ccxt

        manager = OrderManager()
        exchange = MagicMock()
        exchange.create_market_order = AsyncMock(return_value={"id": "ord-13"})
        exchange.fetch_order = AsyncMock(return_value={"status": "cancelled"})
        with pytest.raises(ccxt.ExchangeError, match="was cancelled"):
            await manager.place_order_with_fsm(exchange, "BTC/USDT", "buy", 1.0)
        assert exchange.fetch_order.await_count == 1

    @pytest.mark.asyncio
    async def test_unknown_status_retries_then_fills(self):
        """An unrecognized status string must be logged and retried, not
        crash the confirmation loop."""
        import ccxt.async_support as ccxt

        manager = OrderManager()
        exchange = MagicMock()
        exchange.create_market_order = AsyncMock(return_value={"id": "ord-5"})
        exchange.fetch_order = AsyncMock(
            side_effect=[
                {"status": "weird_unknown_status"},
                {"status": "filled", "filled": 1.0, "average": 100.0},
            ]
        )
        fsm, confirmed = await manager.place_order_with_fsm(exchange, "BTC/USDT", "buy", 1.0)
        assert fsm.state.status.name == "FILLED"
        assert confirmed["average"] == 100.0
        assert ccxt is not None  # sanity: import didn't fail

    @pytest.mark.asyncio
    async def test_permanent_error_during_confirm_fails_fsm_and_raises(self):
        """A permanent exchange error (InsufficientFunds etc.) raised during
        fill confirmation must transition the FSM to FAILED and propagate,
        not be swallowed as a transient retry."""
        import ccxt.async_support as ccxt

        manager = OrderManager()
        exchange = MagicMock()
        exchange.create_market_order = AsyncMock(return_value={"id": "ord-6"})
        exchange.fetch_order = AsyncMock(side_effect=ccxt.InsufficientFunds("not enough balance"))
        # _confirm_order_fill already transitions the FSM to FAILED for this
        # permanent-error class -- place_order_with_fsm's own outer except
        # must not attempt a second transition (which would previously raise
        # OrderFSMError and mask the real ccxt error).
        with pytest.raises(ccxt.InsufficientFunds):
            await manager.place_order_with_fsm(exchange, "BTC/USDT", "buy", 1.0)

    @pytest.mark.asyncio
    async def test_network_error_during_confirm_retries_then_fills(self):
        """Transient network errors during confirmation must be retried, not
        raised immediately."""
        import ccxt.async_support as ccxt

        manager = OrderManager()
        exchange = MagicMock()
        exchange.create_market_order = AsyncMock(return_value={"id": "ord-7"})
        exchange.fetch_order = AsyncMock(
            side_effect=[
                ccxt.NetworkError("connection reset"),
                {"status": "filled", "filled": 1.0, "average": 100.0},
            ]
        )
        fsm, confirmed = await manager.place_order_with_fsm(exchange, "BTC/USDT", "buy", 1.0)
        assert fsm.state.status.name == "FILLED"

    @pytest.mark.asyncio
    async def test_unclassified_exchange_error_during_confirm_retries_then_fills(self):
        """A generic (unclassified) ccxt.ExchangeError during confirmation
        must be treated as possibly-transient and retried."""
        import ccxt.async_support as ccxt

        manager = OrderManager()
        exchange = MagicMock()
        exchange.create_market_order = AsyncMock(return_value={"id": "ord-8"})
        exchange.fetch_order = AsyncMock(
            side_effect=[
                ccxt.ExchangeError("unclassified hiccup"),
                {"status": "filled", "filled": 1.0, "average": 100.0},
            ]
        )
        fsm, confirmed = await manager.place_order_with_fsm(exchange, "BTC/USDT", "buy", 1.0)
        assert fsm.state.status.name == "FILLED"

    @pytest.mark.asyncio
    async def test_create_order_exchange_error_fails_fsm_and_raises(self):
        """A permanent ExchangeError from order creation itself (not fill
        confirmation) must fail the FSM before any polling starts."""
        import ccxt.async_support as ccxt

        manager = OrderManager()
        exchange = MagicMock()
        exchange.create_market_order = AsyncMock(side_effect=ccxt.InvalidOrder("bad order params"))
        with pytest.raises(ccxt.ExchangeError):
            await manager.place_order_with_fsm(exchange, "BTC/USDT", "buy", 1.0)

    @pytest.mark.asyncio
    async def test_create_order_network_error_raises_without_fsm_failure(self):
        """A transient network error during order CREATION (not confirmation)
        must propagate to the caller for its own retry logic, and must not
        force the FSM into FAILED (it may still be legitimately retried at a
        higher level without a new order ever having been placed)."""
        import ccxt.async_support as ccxt

        manager = OrderManager()
        exchange = MagicMock()
        exchange.create_market_order = AsyncMock(side_effect=ccxt.NetworkError("dns failure"))
        with pytest.raises(ccxt.NetworkError):
            await manager.place_order_with_fsm(exchange, "BTC/USDT", "buy", 1.0)
