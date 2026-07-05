"""
Coverage tests for LiveExecutor critical paths (Debt-009).

Uses object.__new__ to bypass TRADING_MODE=live gate (same pattern as
tests/test_order_fsm_registry.py — avoids lru_cache env var race).

Targets: initialize, submit_signal, _submit_signal_auto,
_submit_signal_with_approval, _place_and_record, close_position,
mark_to_market, reset_daily_equity, get_risk_snapshot, helper properties.
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from dataclasses import dataclass
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.config import ExecutionMode, TradingMode
from src.execution.live import LiveExecutor, LivePosition
from src.risk.kelly import KellyResult


# ─────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────

def _kelly(
    notional: float = 5000.0,
    qty: float = 0.1,
    price: float = 50000.0,
) -> KellyResult:
    return KellyResult(
        kelly_fraction=0.02,
        adjusted_fraction=0.01,
        capital_usd=100_000.0,
        entry_price=price,
        quantity=qty,
        notional_usd=notional,
        is_capped=False,
    )


def _make_executor(
    starting_capital: float = 100_000.0,
    cash: float | None = None,
) -> LiveExecutor:
    """Construct LiveExecutor without __init__ (no TRADING_MODE=live needed)."""
    import asyncio
    from collections import OrderedDict

    from src.risk.gates import DrawdownTracker
    from src.execution.order_manager import OrderManager
    import structlog

    executor = object.__new__(LiveExecutor)
    executor._starting_capital = starting_capital
    executor._cash = cash if cash is not None else starting_capital
    executor._peak_equity = starting_capital
    executor._positions: dict = {}
    executor._approval_queue: dict = {}
    executor._lock = asyncio.Lock()
    executor._trade_semaphore = asyncio.Semaphore(1)
    executor._drawdown_tracker = DrawdownTracker(starting_capital)
    executor._order_manager = OrderManager()
    executor._order_fsm_registry = OrderedDict()
    executor._initialized = True
    executor._storage = AsyncMock()
    executor._fetcher = MagicMock()
    executor._cfg = MagicMock()
    executor._risk_cfg = MagicMock(
        notional_limit_usd=10_000.0,
        approval_timeout_s=30.0,
    )
    executor._log = structlog.get_logger().bind(component="live_executor_test")
    return executor


def _filled_order(
    order_id: str = "ord-1",
    price: float = 50000.0,
    qty: float = 0.1,
    fee_usd: float = 2.5,
) -> dict:
    return {
        "id": order_id,
        "status": "closed",
        "filled": qty,
        "amount": qty,
        "average": price,
        "fees": [{"currency": "USDT", "cost": fee_usd}],
    }


# ─────────────────────────────────────────────────────────────
# initialize()
# ─────────────────────────────────────────────────────────────

class TestInitialize:
    @pytest.mark.asyncio
    async def test_initialize_fresh_no_prior_equity(self):
        ex = _make_executor()
        ex._initialized = False
        ex._storage.latest_equity = AsyncMock(return_value=None)
        await ex.initialize()
        assert ex._initialized is True

    @pytest.mark.asyncio
    async def test_initialize_restores_from_storage(self):
        ex = _make_executor(starting_capital=100_000.0)
        ex._initialized = False
        record = MagicMock(
            cash_usd=80_000.0,
            equity_usd=85_000.0,
            peak_equity_usd=100_000.0,
        )
        ex._storage.latest_equity = AsyncMock(return_value=record)
        await ex.initialize()
        assert ex._cash == 80_000.0
        assert ex._initialized is True


# ─────────────────────────────────────────────────────────────
# Properties
# ─────────────────────────────────────────────────────────────

class TestProperties:
    def test_cash_usd(self):
        ex = _make_executor(cash=75_000.0)
        assert ex.cash_usd == 75_000.0

    def test_starting_capital(self):
        ex = _make_executor(starting_capital=50_000.0)
        assert ex.starting_capital == 50_000.0

    def test_peak_equity(self):
        ex = _make_executor()
        ex._peak_equity = 110_000.0
        assert ex.peak_equity == 110_000.0

    def test_position_count_empty(self):
        ex = _make_executor()
        assert ex.position_count() == 0

    def test_position_count_with_positions(self):
        ex = _make_executor()
        ex._positions["BTC/USDT"] = MagicMock()
        assert ex.position_count() == 1

    def test_equity_usd_no_positions(self):
        ex = _make_executor(cash=80_000.0)
        assert ex.equity_usd == 80_000.0

    def test_open_positions_snapshot_empty(self):
        ex = _make_executor()
        snap = ex._open_positions_snapshot()
        assert snap == []

    @pytest.mark.asyncio
    async def test_open_positions_safe(self):
        ex = _make_executor()
        result = await ex.open_positions_safe()
        assert result == []

    @pytest.mark.asyncio
    async def test_pending_approvals_safe_empty(self):
        ex = _make_executor()
        result = await ex.pending_approvals_safe()
        assert result == []

    @pytest.mark.asyncio
    async def test_get_consecutive_losses(self):
        ex = _make_executor()
        ex._storage.count_consecutive_losses = AsyncMock(return_value=2)
        result = await ex.get_consecutive_losses("BTC/USDT")
        assert result == 2

    @pytest.mark.asyncio
    async def test_get_daily_pnl(self):
        ex = _make_executor()
        ex._storage.daily_pnl = AsyncMock(return_value=-150.0)
        result = await ex.get_daily_pnl("BTC/USDT")
        assert result == -150.0


def _make_position(
    trade_id: str = "trade-1",
    symbol: str = "BTC/USDT",
    direction: int = 1,
    entry_price: float = 50_000.0,
    quantity: float = 0.1,
    notional_usd: float = 5_000.0,
) -> LivePosition:
    return LivePosition(
        trade_id=trade_id,
        exchange_order_id="ord-1",
        symbol=symbol,
        timeframe="1h",
        direction=direction,
        entry_price=entry_price,
        quantity=quantity,
        notional_usd=notional_usd,
        entry_ts=1_700_000_000_000,
        kelly_fraction=0.02,
        regime_at_entry=0,
        meta_label_prob=0.7,
        raw_signal=1.0,
        approved_by="auto",
        execution_mode="automatic",
        fee_usd=2.5,
        unrealized_pnl=0.0,
        current_price=entry_price,
    )


class TestSubmitSignalRouting:
    @pytest.mark.asyncio
    async def test_automatic_mode_calls_auto_path(self):
        ex = _make_executor()
        with patch("src.execution.live.runtime_config") as mock_rc:
            mock_rc.get_execution_mode = AsyncMock(return_value=ExecutionMode.AUTOMATIC)
            ex._submit_signal_auto = AsyncMock(return_value=("trade-1", "opened"))
            result = await ex.submit_signal(
                symbol="BTC/USDT", timeframe="1h", direction=1,
                kelly_result=_kelly(), regime_state=0,
                meta_label_prob=0.7, raw_signal=1.0, current_price=50_000.0,
            )
        assert result == ("trade-1", "opened")
        ex._submit_signal_auto.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_restricted_below_limit_auto(self):
        ex = _make_executor()
        ex._risk_cfg.notional_limit_usd = 10_000.0
        with patch("src.execution.live.runtime_config") as mock_rc:
            mock_rc.get_execution_mode = AsyncMock(return_value=ExecutionMode.RESTRICTED)
            ex._submit_signal_auto = AsyncMock(return_value=("trade-2", "opened"))
            result = await ex.submit_signal(
                symbol="ETH/USDT", timeframe="1h", direction=1,
                kelly_result=_kelly(notional=3_000.0), regime_state=0,
                meta_label_prob=0.6, raw_signal=1.0, current_price=3_000.0,
            )
        assert result[1] == "opened"
        ex._submit_signal_auto.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_restricted_above_limit_queues_approval(self):
        ex = _make_executor()
        ex._risk_cfg.notional_limit_usd = 10_000.0
        with patch("src.execution.live.runtime_config") as mock_rc:
            mock_rc.get_execution_mode = AsyncMock(return_value=ExecutionMode.RESTRICTED)
            ex._submit_signal_with_approval = AsyncMock(return_value=(None, "skipped"))
            result = await ex.submit_signal(
                symbol="BTC/USDT", timeframe="1h", direction=1,
                kelly_result=_kelly(notional=15_000.0), regime_state=0,
                meta_label_prob=0.8, raw_signal=1.0, current_price=50_000.0,
            )
        assert result == (None, "skipped")
        ex._submit_signal_with_approval.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_manual_mode_always_queues_approval(self):
        ex = _make_executor()
        with patch("src.execution.live.runtime_config") as mock_rc:
            mock_rc.get_execution_mode = AsyncMock(return_value=ExecutionMode.MANUAL)
            ex._submit_signal_with_approval = AsyncMock(return_value=(None, "pending"))
            await ex.submit_signal(
                symbol="BTC/USDT", timeframe="1h", direction=-1,
                kelly_result=_kelly(), regime_state=1,
                meta_label_prob=0.55, raw_signal=-1.0, current_price=50_000.0,
            )
        ex._submit_signal_with_approval.assert_awaited_once()


class TestPlaceAndRecordGuards:
    @pytest.mark.asyncio
    async def test_insufficient_cash_returns_none(self):
        ex = _make_executor(cash=10.0)
        result = await ex._place_and_record(
            symbol="BTC/USDT", timeframe="1h", direction=1,
            kelly_result=_kelly(notional=5_000.0),
            regime_state=0, meta_label_prob=0.7, raw_signal=1.0,
            approved_by="auto",
        )
        assert result is None
        assert ex._cash == pytest.approx(10.0)

    @pytest.mark.asyncio
    async def test_bad_fill_price_returns_none_restores_cash(self):
        ex = _make_executor(cash=100_000.0)
        bad_order = {"id": "ord-bad", "status": "closed", "filled": 0.1,
                     "amount": 0.1, "average": 0.0, "fees": []}
        ex._place_market_order = AsyncMock(return_value=bad_order)
        ex._storage.insert_trade = AsyncMock()
        result = await ex._place_and_record(
            symbol="BTC/USDT", timeframe="1h", direction=1,
            kelly_result=_kelly(notional=5_000.0, qty=0.1, price=50_000.0),
            regime_state=0, meta_label_prob=0.7, raw_signal=1.0,
            approved_by="auto",
        )
        assert result is None
        assert ex._cash == pytest.approx(100_000.0)

    @pytest.mark.asyncio
    async def test_network_error_restores_cash(self):
        import ccxt
        ex = _make_executor(cash=100_000.0)
        ex._place_market_order = AsyncMock(side_effect=ccxt.NetworkError("timeout"))
        result = await ex._place_and_record(
            symbol="BTC/USDT", timeframe="1h", direction=1,
            kelly_result=_kelly(notional=5_000.0),
            regime_state=0, meta_label_prob=0.7, raw_signal=1.0,
            approved_by="auto",
        )
        assert result is None
        assert ex._cash == pytest.approx(100_000.0)

    @pytest.mark.asyncio
    async def test_successful_fill_creates_position(self):
        ex = _make_executor(cash=100_000.0)
        ex._place_market_order = AsyncMock(return_value=_filled_order(
            price=50_000.0, qty=0.1, fee_usd=2.5))
        ex._storage.insert_trade = AsyncMock()
        ex._storage.insert_equity = AsyncMock()
        trade_id = await ex._place_and_record(
            symbol="BTC/USDT", timeframe="1h", direction=1,
            kelly_result=_kelly(notional=5_000.0, qty=0.1, price=50_000.0),
            regime_state=0, meta_label_prob=0.7, raw_signal=1.0,
            approved_by="auto",
        )
        assert trade_id is not None
        assert trade_id in ex._positions
        ex._storage.insert_trade.assert_awaited_once()


class TestMarkToMarket:
    @pytest.mark.asyncio
    async def test_no_positions_returns_zero(self):
        ex = _make_executor(cash=100_000.0)
        ex._storage.insert_equity = AsyncMock()
        result = await ex.mark_to_market({"BTC/USDT": 51_000.0})
        assert result == pytest.approx(0.0)
        ex._storage.insert_equity.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_updates_unrealized_pnl_long(self):
        ex = _make_executor(cash=95_000.0)
        pos = _make_position(entry_price=50_000.0, quantity=0.1)
        ex._positions["BTC/USDT"] = pos
        ex._storage.insert_equity = AsyncMock()
        total = await ex.mark_to_market({"BTC/USDT": 51_000.0})
        assert total == pytest.approx(100.0, rel=1e-3)
        assert pos.current_price == pytest.approx(51_000.0)

    @pytest.mark.asyncio
    async def test_missing_price_preserves_existing_unrealized(self):
        ex = _make_executor(cash=95_000.0)
        pos = _make_position(entry_price=50_000.0, quantity=0.1)
        pos.unrealized_pnl = 50.0
        ex._positions["BTC/USDT"] = pos
        ex._storage.insert_equity = AsyncMock()
        total = await ex.mark_to_market({})
        assert total == pytest.approx(50.0)

    @pytest.mark.asyncio
    async def test_zero_price_skips_update(self):
        ex = _make_executor(cash=95_000.0)
        pos = _make_position(entry_price=50_000.0, quantity=0.1)
        pos.unrealized_pnl = 25.0
        ex._positions["BTC/USDT"] = pos
        ex._storage.insert_equity = AsyncMock()
        total = await ex.mark_to_market({"BTC/USDT": 0.0})
        assert total == pytest.approx(25.0)


class TestClosePosition:
    @pytest.mark.asyncio
    async def test_unknown_trade_id_raises(self):
        ex = _make_executor()
        with pytest.raises(KeyError, match="trade-unknown"):
            await ex.close_position("trade-unknown", exit_price=51_000.0, exit_reason="tp")

    @pytest.mark.asyncio
    async def test_close_long_profitable(self):
        ex = _make_executor(cash=95_000.0)
        pos = _make_position(
            trade_id="trade-1", entry_price=50_000.0,
            quantity=0.1, notional_usd=5_000.0, direction=1,
        )
        ex._positions["trade-1"] = pos
        ex._place_market_order = AsyncMock(return_value=_filled_order(
            price=51_000.0, qty=0.1, fee_usd=2.55))
        ex._storage.update_trade_exit = AsyncMock()
        ex._storage.insert_equity = AsyncMock()
        net_pnl = await ex.close_position("trade-1", exit_price=51_000.0, exit_reason="tp")
        assert net_pnl == pytest.approx(97.45, rel=1e-3)
        assert "trade-1" not in ex._positions
        ex._storage.update_trade_exit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_close_short_profitable(self):
        ex = _make_executor(cash=95_000.0)
        pos = _make_position(
            trade_id="trade-s", entry_price=50_000.0,
            quantity=0.1, notional_usd=5_000.0, direction=-1,
        )
        ex._positions["trade-s"] = pos
        ex._place_market_order = AsyncMock(return_value=_filled_order(
            price=49_000.0, qty=0.1, fee_usd=2.45))
        ex._storage.update_trade_exit = AsyncMock()
        ex._storage.insert_equity = AsyncMock()
        net_pnl = await ex.close_position("trade-s", exit_price=49_000.0, exit_reason="tp")
        assert net_pnl == pytest.approx(97.55, rel=1e-3)
        assert "trade-s" not in ex._positions

    @pytest.mark.asyncio
    async def test_exchange_error_propagates_position_intact(self):
        import ccxt
        ex = _make_executor(cash=95_000.0)
        ex._positions["trade-err"] = _make_position(trade_id="trade-err")
        ex._place_market_order = AsyncMock(side_effect=ccxt.ExchangeError("rejected"))
        with pytest.raises(ccxt.ExchangeError):
            await ex.close_position("trade-err", exit_price=50_000.0, exit_reason="sl")
        assert "trade-err" in ex._positions


class TestEquityAccounting:
    def test_equity_with_positive_unrealized(self):
        ex = _make_executor(cash=90_000.0)
        pos = _make_position()
        pos.unrealized_pnl = 500.0
        ex._positions["BTC/USDT"] = pos
        assert ex._equity_usd() == pytest.approx(90_500.0)

    def test_equity_with_negative_unrealized(self):
        ex = _make_executor(cash=90_000.0)
        pos = _make_position()
        pos.unrealized_pnl = -300.0
        ex._positions["BTC/USDT"] = pos
        assert ex._equity_usd() == pytest.approx(89_700.0)

    def test_equity_usd_property_matches_internal(self):
        ex = _make_executor(cash=95_000.0)
        pos = _make_position()
        pos.unrealized_pnl = 200.0
        ex._positions["BTC/USDT"] = pos
        assert ex.equity_usd == pytest.approx(ex._equity_usd())
