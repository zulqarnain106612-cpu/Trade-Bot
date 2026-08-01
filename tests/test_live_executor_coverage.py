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
import time
from collections import OrderedDict
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.config import ExecutionMode, OrderThrottleSettings
from src.execution.live import LiveExecutor, LivePosition
from src.execution.order_throttler import OrderThrottler, ThrottleResult
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

    import structlog

    from src.execution.order_manager import OrderManager
    from src.risk.gates import DrawdownTracker

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
    # v8 startup reconciliation: initialize() compares local positions with
    # exchange truth, and an unavailable snapshot deliberately blocks new
    # entries. A bare MagicMock would make every test look like a crashed
    # process with an unknown book.
    executor._fetcher.fetch_exchange_holdings = AsyncMock(return_value={})
    executor._recovery_discrepancies = []
    executor._cfg = MagicMock()
    executor._risk_cfg = MagicMock(
        notional_limit_usd=10_000.0,
        approval_timeout_s=30.0,
    )
    executor._throttle_cfg = OrderThrottleSettings()
    executor._throttler = OrderThrottler(
        rate=executor._throttle_cfg.rate,
        burst=executor._throttle_cfg.burst,
    )
    # submit_signal refuses to trade while startup reconciliation is unresolved.
    # __init__ seeds this empty, and this factory bypasses __init__ on purpose.
    executor._recovery_discrepancies = []
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
                symbol="BTC/USDT",
                timeframe="1h",
                direction=1,
                kelly_result=_kelly(),
                regime_state=0,
                meta_label_prob=0.7,
                raw_signal=1.0,
                current_price=50_000.0,
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
                symbol="ETH/USDT",
                timeframe="1h",
                direction=1,
                kelly_result=_kelly(notional=3_000.0),
                regime_state=0,
                meta_label_prob=0.6,
                raw_signal=1.0,
                current_price=3_000.0,
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
                symbol="BTC/USDT",
                timeframe="1h",
                direction=1,
                kelly_result=_kelly(notional=15_000.0),
                regime_state=0,
                meta_label_prob=0.8,
                raw_signal=1.0,
                current_price=50_000.0,
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
                symbol="BTC/USDT",
                timeframe="1h",
                direction=-1,
                kelly_result=_kelly(),
                regime_state=1,
                meta_label_prob=0.55,
                raw_signal=-1.0,
                current_price=50_000.0,
            )
        ex._submit_signal_with_approval.assert_awaited_once()


class TestPlaceAndRecordGuards:
    @pytest.mark.asyncio
    async def test_insufficient_cash_returns_none(self):
        ex = _make_executor(cash=10.0)
        result = await ex._place_and_record(
            symbol="BTC/USDT",
            timeframe="1h",
            direction=1,
            kelly_result=_kelly(notional=5_000.0),
            regime_state=0,
            meta_label_prob=0.7,
            raw_signal=1.0,
            approved_by="auto",
        )
        assert result is None
        assert ex._cash == pytest.approx(10.0)

    @pytest.mark.asyncio
    async def test_bad_fill_price_returns_none_restores_cash(self):
        ex = _make_executor(cash=100_000.0)
        bad_order = {
            "id": "ord-bad",
            "status": "closed",
            "filled": 0.1,
            "amount": 0.1,
            "average": 0.0,
            "fees": [],
        }
        ex._place_market_order = AsyncMock(return_value=bad_order)
        ex._storage.insert_trade = AsyncMock()
        result = await ex._place_and_record(
            symbol="BTC/USDT",
            timeframe="1h",
            direction=1,
            kelly_result=_kelly(notional=5_000.0, qty=0.1, price=50_000.0),
            regime_state=0,
            meta_label_prob=0.7,
            raw_signal=1.0,
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
            symbol="BTC/USDT",
            timeframe="1h",
            direction=1,
            kelly_result=_kelly(notional=5_000.0),
            regime_state=0,
            meta_label_prob=0.7,
            raw_signal=1.0,
            approved_by="auto",
        )
        assert result is None
        assert ex._cash == pytest.approx(100_000.0)

    @pytest.mark.asyncio
    async def test_successful_fill_creates_position(self):
        ex = _make_executor(cash=100_000.0)
        ex._place_market_order = AsyncMock(
            return_value=_filled_order(price=50_000.0, qty=0.1, fee_usd=2.5)
        )
        ex._storage.insert_trade = AsyncMock()
        ex._storage.insert_equity = AsyncMock()
        trade_id = await ex._place_and_record(
            symbol="BTC/USDT",
            timeframe="1h",
            direction=1,
            kelly_result=_kelly(notional=5_000.0, qty=0.1, price=50_000.0),
            regime_state=0,
            meta_label_prob=0.7,
            raw_signal=1.0,
            approved_by="auto",
        )
        assert trade_id is not None
        assert trade_id in ex._positions
        ex._storage.insert_trade.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_post_reconcile_cash_insufficient_returns_none(self):
        # Estimated notional (5_000) is affordable, but the actual fill
        # (price*qty=50_000*0.2=10_000) costs more than the reserved estimate —
        # after undoing the pre-check reserve, cash is short for the real cost.
        ex = _make_executor(cash=5_100.0)
        ex._place_market_order = AsyncMock(
            return_value=_filled_order(price=50_000.0, qty=0.2, fee_usd=1.0)
        )
        result = await ex._place_and_record(
            symbol="BTC/USDT",
            timeframe="1h",
            direction=1,
            kelly_result=_kelly(notional=5_000.0, qty=0.2, price=50_000.0),
            regime_state=0,
            meta_label_prob=0.7,
            raw_signal=1.0,
            approved_by="auto",
        )
        assert result is None
        assert "BTC/USDT" not in ex._positions


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


class TestLivePosition:
    def test_peak_unrealized_pct_default_zero(self):
        pos = _make_position()
        assert pos.peak_unrealized_pct == 0.0

    def test_peak_unrealized_pct_increases_on_profit(self):
        pos = _make_position(entry_price=50_000.0, quantity=0.1, notional_usd=5_000.0)
        pos.mark(55_000.0)  # unrealized = +500 / 5000 = +10%
        assert pos.peak_unrealized_pct == pytest.approx(10.0)

    def test_peak_unrealized_pct_does_not_decrease(self):
        pos = _make_position(entry_price=50_000.0, quantity=0.1, notional_usd=5_000.0)
        pos.mark(55_000.0)  # peak at 10%
        pos.mark(52_000.0)  # drops to 4%
        assert pos.peak_unrealized_pct == pytest.approx(10.0)

    def test_peak_unrealized_pct_loss_does_not_update(self):
        pos = _make_position(entry_price=50_000.0, quantity=0.1, notional_usd=5_000.0)
        pos.mark(47_000.0)  # loss → no update
        assert pos.peak_unrealized_pct == 0.0


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
            trade_id="trade-1",
            entry_price=50_000.0,
            quantity=0.1,
            notional_usd=5_000.0,
            direction=1,
        )
        ex._positions["trade-1"] = pos
        ex._place_market_order = AsyncMock(
            return_value=_filled_order(price=51_000.0, qty=0.1, fee_usd=2.55)
        )
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
            trade_id="trade-s",
            entry_price=50_000.0,
            quantity=0.1,
            notional_usd=5_000.0,
            direction=-1,
        )
        ex._positions["trade-s"] = pos
        ex._place_market_order = AsyncMock(
            return_value=_filled_order(price=49_000.0, qty=0.1, fee_usd=2.45)
        )
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


# ─────────────────────────────────────────────────────────────
# __init__() — TRADING_MODE gate
# ─────────────────────────────────────────────────────────────


class TestInit:
    def test_raises_when_not_live_mode(self):
        from src.config import TradingMode

        storage = AsyncMock()
        fetcher = MagicMock()
        cfg = MagicMock(trading_mode=TradingMode.PAPER)
        cfg.order_throttle = OrderThrottleSettings()
        with patch("src.execution.live.get_settings", return_value=cfg):
            with pytest.raises(RuntimeError, match="TRADING_MODE=live"):
                LiveExecutor(storage, fetcher)

    def test_constructs_when_live_mode(self):
        from src.config import TradingMode

        storage = AsyncMock()
        fetcher = MagicMock()
        cfg = MagicMock(trading_mode=TradingMode.LIVE, starting_capital_usd=50_000.0)
        cfg.risk = MagicMock()
        cfg.order_throttle = OrderThrottleSettings()
        with patch("src.execution.live.get_settings", return_value=cfg):
            ex = LiveExecutor(storage, fetcher)
        assert ex._starting_capital == 50_000.0
        assert ex._cash == 50_000.0
        assert ex._initialized is False
        assert ex._positions == {}
        assert ex._approval_queue == {}

    def test_explicit_starting_capital_overrides_settings(self):
        from src.config import TradingMode

        storage = AsyncMock()
        fetcher = MagicMock()
        cfg = MagicMock(trading_mode=TradingMode.LIVE, starting_capital_usd=50_000.0)
        cfg.risk = MagicMock()
        cfg.order_throttle = OrderThrottleSettings()
        with patch("src.execution.live.get_settings", return_value=cfg):
            ex = LiveExecutor(storage, fetcher, starting_capital=200_000.0)
        assert ex._starting_capital == 200_000.0
        assert ex._peak_equity == 200_000.0


# ─────────────────────────────────────────────────────────────
# _submit_signal_auto / _submit_signal_with_approval / _enqueue_approval /
# _await_approval — internal routing (not exercised by TestSubmitSignalRouting,
# which mocks these methods away)
# ─────────────────────────────────────────────────────────────


class TestSubmitSignalAuto:
    @pytest.mark.asyncio
    async def test_delegates_to_place_and_record(self):
        ex = _make_executor()
        ex._place_and_record = AsyncMock(return_value="trade-auto-1")
        trade_id, outcome = await ex._submit_signal_auto(
            symbol="BTC/USDT",
            timeframe="1h",
            direction=1,
            kelly_result=_kelly(),
            regime_state=0,
            meta_label_prob=0.7,
            raw_signal=1.0,
            approved_by="system",
        )
        assert trade_id == "trade-auto-1"
        assert outcome == "opened"
        ex._place_and_record.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_rejected_when_place_and_record_returns_none(self):
        ex = _make_executor()
        ex._place_and_record = AsyncMock(return_value=None)
        trade_id, outcome = await ex._submit_signal_auto(
            symbol="BTC/USDT",
            timeframe="1h",
            direction=1,
            kelly_result=_kelly(),
            regime_state=0,
            meta_label_prob=0.7,
            raw_signal=1.0,
            approved_by="system",
        )
        assert trade_id is None
        assert outcome == "rejected"


class TestEnqueueApproval:
    @pytest.mark.asyncio
    async def test_adds_request_to_queue(self):
        ex = _make_executor()
        req_id = await ex._enqueue_approval(
            symbol="BTC/USDT",
            timeframe="1h",
            direction=1,
            kelly_result=_kelly(notional=1_234.0),
            regime_state=2,
            meta_label_prob=0.65,
            raw_signal=0.5,
        )
        assert req_id in ex._approval_queue
        req = ex._approval_queue[req_id]
        assert req.symbol == "BTC/USDT"
        assert req.notional_usd == pytest.approx(1_234.0)
        assert req.resolved is False


class TestPendingApprovalsSafe:
    @pytest.mark.asyncio
    async def test_returns_unresolved_and_prunes_stale_resolved(self):
        ex = _make_executor()
        req_id = await ex._enqueue_approval(
            symbol="BTC/USDT",
            timeframe="1h",
            direction=1,
            kelly_result=_kelly(),
            regime_state=0,
            meta_label_prob=0.7,
            raw_signal=1.0,
        )
        stale_id = await ex._enqueue_approval(
            symbol="ETH/USDT",
            timeframe="1h",
            direction=1,
            kelly_result=_kelly(),
            regime_state=0,
            meta_label_prob=0.7,
            raw_signal=1.0,
        )
        stale_req = ex._approval_queue[stale_id]
        stale_req.resolved = True
        stale_req.created_at = time.monotonic() - 7200.0

        pending = await ex.pending_approvals_safe()
        assert stale_id not in ex._approval_queue
        assert any(p["request_id"] == req_id for p in pending)


class TestAwaitApproval:
    @pytest.mark.asyncio
    async def test_missing_request_returns_false(self):
        ex = _make_executor()
        approved, operator = await ex._await_approval("nonexistent", timeout_s=1.0)
        assert approved is False
        assert operator == ""

    @pytest.mark.asyncio
    async def test_resolved_approval_returns_true(self):
        ex = _make_executor()
        req_id = await ex._enqueue_approval(
            symbol="BTC/USDT",
            timeframe="1h",
            direction=1,
            kelly_result=_kelly(),
            regime_state=0,
            meta_label_prob=0.7,
            raw_signal=1.0,
        )
        await ex.resolve_approval(req_id, approved=True, operator="alice")
        approved, operator = await ex._await_approval(req_id, timeout_s=1.0)
        assert approved is True
        assert operator == "alice"

    @pytest.mark.asyncio
    async def test_timeout_auto_denies(self):
        ex = _make_executor()
        req_id = await ex._enqueue_approval(
            symbol="BTC/USDT",
            timeframe="1h",
            direction=1,
            kelly_result=_kelly(),
            regime_state=0,
            meta_label_prob=0.7,
            raw_signal=1.0,
        )
        approved, operator = await ex._await_approval(req_id, timeout_s=0.01)
        assert approved is False
        assert operator == "auto_timeout"
        assert req_id not in ex._approval_queue


class TestSubmitSignalWithApproval:
    @pytest.mark.asyncio
    async def test_denied_returns_denied_outcome(self):
        ex = _make_executor()
        ex._await_approval = AsyncMock(return_value=(False, "auto_timeout"))
        trade_id, outcome = await ex._submit_signal_with_approval(
            symbol="BTC/USDT",
            timeframe="1h",
            direction=1,
            kelly_result=_kelly(),
            regime_state=0,
            meta_label_prob=0.7,
            raw_signal=1.0,
            timeout_s=1.0,
            denied_outcome="skipped",
        )
        assert trade_id is None
        assert outcome == "skipped"

    @pytest.mark.asyncio
    async def test_approved_delegates_to_auto_path(self):
        ex = _make_executor()
        ex._await_approval = AsyncMock(return_value=(True, "alice"))
        ex._submit_signal_auto = AsyncMock(return_value=("trade-2", "opened"))
        trade_id, outcome = await ex._submit_signal_with_approval(
            symbol="BTC/USDT",
            timeframe="1h",
            direction=1,
            kelly_result=_kelly(),
            regime_state=0,
            meta_label_prob=0.7,
            raw_signal=1.0,
            timeout_s=1.0,
            denied_outcome="skipped",
        )
        assert trade_id == "trade-2"
        assert outcome == "opened"
        _, kwargs = ex._submit_signal_auto.call_args
        assert kwargs["approved_by"] == "alice"


# ─────────────────────────────────────────────────────────────
# _place_market_order — FSM success, timeout, exchange-error paths
# ─────────────────────────────────────────────────────────────


class TestPlaceMarketOrder:
    @pytest.mark.asyncio
    async def test_success_registers_fsm(self):
        ex = _make_executor()
        fsm = MagicMock()
        fsm.state.order_id = "ord-42"
        fsm.state.filled_qty = 0.1
        fsm.state.average_fill_price = 50_000.0
        fsm.state.retry_count = 1
        confirmed = _filled_order(order_id="ord-42")
        ex._order_manager.place_order_with_fsm = AsyncMock(return_value=(fsm, confirmed))
        result = await ex._place_market_order("BTC/USDT", "buy", 0.1)
        assert result == confirmed
        assert ex._order_fsm_registry["ord-42"] is fsm.state

    @pytest.mark.asyncio
    async def test_timeout_raises_exchange_error(self):
        import ccxt

        ex = _make_executor()
        ex._order_manager.place_order_with_fsm = AsyncMock(side_effect=TimeoutError("no fill"))
        with pytest.raises(ccxt.ExchangeError, match="did not confirm"):
            await ex._place_market_order("BTC/USDT", "buy", 0.1)

    @pytest.mark.asyncio
    async def test_exchange_error_propagates(self):
        import ccxt

        ex = _make_executor()
        ex._order_manager.place_order_with_fsm = AsyncMock(
            side_effect=ccxt.ExchangeError("rejected")
        )
        with pytest.raises(ccxt.ExchangeError, match="rejected"):
            await ex._place_market_order("BTC/USDT", "buy", 0.1)


# ─────────────────────────────────────────────────────────────
# _register_order_fsm / get_order_fsm_state
# ─────────────────────────────────────────────────────────────


class TestOrderFsmRegistry:
    def test_register_and_lookup(self):
        ex = _make_executor()
        fsm = MagicMock()
        fsm.state.order_id = "ord-99"
        ex._register_order_fsm(fsm)
        assert ex._order_fsm_registry["ord-99"] is fsm.state

    @pytest.mark.asyncio
    async def test_get_order_fsm_state_found(self):
        ex = _make_executor()
        fsm = MagicMock()
        fsm.state.order_id = "ord-100"
        ex._register_order_fsm(fsm)
        result = await ex.get_order_fsm_state("ord-100")
        assert result is fsm.state

    @pytest.mark.asyncio
    async def test_get_order_fsm_state_not_found(self):
        ex = _make_executor()
        result = await ex.get_order_fsm_state("nonexistent")
        assert result is None

    def test_registry_evicts_oldest_when_over_capacity(self):
        from src.execution.live import _ORDER_FSM_REGISTRY_MAX_SIZE

        ex = _make_executor()
        for i in range(_ORDER_FSM_REGISTRY_MAX_SIZE + 5):
            fsm = MagicMock()
            fsm.state.order_id = f"ord-{i}"
            ex._register_order_fsm(fsm)
        assert len(ex._order_fsm_registry) == _ORDER_FSM_REGISTRY_MAX_SIZE
        assert "ord-0" not in ex._order_fsm_registry
        assert f"ord-{_ORDER_FSM_REGISTRY_MAX_SIZE + 4}" in ex._order_fsm_registry


# ─────────────────────────────────────────────────────────────
# _extract_fee — fee parsing fallbacks
# ─────────────────────────────────────────────────────────────


class TestExtractFee:
    def test_uses_fees_list_cost(self):
        ex = _make_executor()
        order = {"fees": [{"currency": "USDT", "cost": 5.0}]}
        fee = ex._extract_fee(order, price=50_000.0, qty=0.1)
        assert fee == pytest.approx(5.0)

    def test_falls_back_to_single_fee_dict(self):
        ex = _make_executor()
        order = {"fee": {"currency": "USDC", "cost": 3.5}}
        fee = ex._extract_fee(order, price=50_000.0, qty=0.1)
        assert fee == pytest.approx(3.5)

    def test_ignores_non_quote_currency_and_falls_back(self):
        ex = _make_executor()
        order = {"fees": [{"currency": "BTC", "cost": 0.0001}]}
        fee = ex._extract_fee(order, price=50_000.0, qty=0.1)
        # BTC-denominated fee not summed → falls back to flat-rate estimate
        assert fee > 0.0
        assert fee != pytest.approx(0.0001)

    def test_unparseable_cost_falls_back(self):
        ex = _make_executor()
        order = {"fees": [{"currency": "USDT", "cost": "not-a-number"}]}
        fee = ex._extract_fee(order, price=50_000.0, qty=0.1)
        assert fee > 0.0

    def test_no_fee_info_uses_flat_rate_estimate(self):
        ex = _make_executor()
        order = {}
        fee = ex._extract_fee(order, price=50_000.0, qty=0.1)
        assert fee > 0.0


# ─────────────────────────────────────────────────────────────
# _require_initialized
# ─────────────────────────────────────────────────────────────


class TestRequireInitialized:
    def test_raises_when_not_initialized(self):
        ex = _make_executor()
        ex._initialized = False
        with pytest.raises(RuntimeError, match="not initialized"):
            ex._require_initialized()

    def test_passes_when_initialized(self):
        ex = _make_executor()
        ex._initialized = True
        ex._require_initialized()  # no raise


# ─────────────────────────────────────────────────────────────
# _await_throttle_token — exchange rate-limit gate on order placement
# ─────────────────────────────────────────────────────────────


class TestAwaitThrottleToken:
    @pytest.mark.asyncio
    async def test_allowed_when_bucket_has_tokens(self):
        ex = _make_executor()
        await ex._await_throttle_token("binance")
        assert ex._throttler.tokens_remaining("binance") == pytest.approx(
            ex._throttle_cfg.burst - 1, abs=0.1
        )

    @pytest.mark.asyncio
    async def test_disabled_does_not_consume_tokens(self):
        ex = _make_executor()
        ex._throttle_cfg = OrderThrottleSettings(enabled=False)
        await ex._await_throttle_token("binance")
        assert ex._throttler.tokens_remaining("binance") == pytest.approx(
            ex._throttle_cfg.burst, abs=0.1
        )

    @pytest.mark.asyncio
    async def test_short_backlog_waits_then_proceeds(self):
        # rate=100/s => a drained bucket refills a token in ~10ms, well under
        # max_wait_s, so the order should be held rather than refused.
        ex = _make_executor()
        ex._throttle_cfg = OrderThrottleSettings(rate=100.0, burst=1, max_wait_s=1.0)
        ex._throttler = OrderThrottler(rate=100.0, burst=1)
        await ex._await_throttle_token("binance")  # drains the single token
        await ex._await_throttle_token("binance")  # must wait, not raise

    @pytest.mark.asyncio
    async def test_long_backlog_refuses_order(self):
        import ccxt

        # rate=0.1/s => 10s to refill one token, far past max_wait_s: the
        # entry price would be stale, so the order must be refused.
        ex = _make_executor()
        ex._throttle_cfg = OrderThrottleSettings(rate=0.1, burst=1, max_wait_s=0.5)
        ex._throttler = OrderThrottler(rate=0.1, burst=1)
        await ex._await_throttle_token("binance")
        with pytest.raises(ccxt.ExchangeError, match="exceeds max_wait_s"):
            await ex._await_throttle_token("binance")

    @pytest.mark.asyncio
    async def test_retry_failure_after_wait_refuses_order(self):
        import ccxt

        ex = _make_executor()
        ex._throttle_cfg = OrderThrottleSettings(rate=100.0, burst=1, max_wait_s=1.0)
        ex._throttler = MagicMock()
        ex._throttler.acquire = MagicMock(
            side_effect=[
                ThrottleResult(
                    allowed=False,
                    exchange="binance",
                    tokens_remaining=0.0,
                    wait_s=0.01,
                    reject_reason="rate_limit",
                ),
                ThrottleResult(
                    allowed=False,
                    exchange="binance",
                    tokens_remaining=0.0,
                    wait_s=0.01,
                    reject_reason="rate_limit",
                ),
            ]
        )
        with pytest.raises(ccxt.ExchangeError, match="still unavailable"):
            await ex._await_throttle_token("binance")

    @pytest.mark.asyncio
    async def test_exit_order_is_never_refused(self):
        # Refusing an exit leaves real unhedged exposure open, and unlike an
        # entry there is no "skip it, wait for the next signal" fallback.
        ex = _make_executor()
        ex._throttle_cfg = OrderThrottleSettings(rate=0.1, burst=1, max_wait_s=0.5)
        ex._throttler = OrderThrottler(rate=0.1, burst=1)
        await ex._await_throttle_token("binance")  # drain
        await ex._await_throttle_token("binance", is_exit=True)  # must not raise

    @pytest.mark.asyncio
    async def test_close_position_exits_through_a_drained_bucket(self):
        ex = _make_executor()
        ex._throttle_cfg = OrderThrottleSettings(rate=0.1, burst=1, max_wait_s=0.5)
        ex._throttler = OrderThrottler(rate=0.1, burst=1)
        ex._fetcher.get_order_exchange = MagicMock(return_value=MagicMock(id="binance"))
        fsm = MagicMock()
        fsm.state.order_id = "ord-1"
        ex._order_manager.place_order_with_fsm = AsyncMock(
            return_value=(fsm, _filled_order(order_id="ord-1"))
        )
        await ex._place_market_order("BTC/USDT", "buy", 0.1)  # drains the bucket
        await ex._place_market_order("BTC/USDT", "sell", 0.1, is_exit=True)
        assert ex._order_manager.place_order_with_fsm.await_count == 2

    @pytest.mark.asyncio
    async def test_place_market_order_refused_when_throttled(self):
        import ccxt

        ex = _make_executor()
        ex._throttle_cfg = OrderThrottleSettings(rate=0.1, burst=1, max_wait_s=0.5)
        ex._throttler = OrderThrottler(rate=0.1, burst=1)
        ex._fetcher.get_order_exchange = MagicMock(return_value=MagicMock(id="binance"))
        fsm = MagicMock()
        fsm.state.order_id = "ord-1"
        ex._order_manager.place_order_with_fsm = AsyncMock(
            return_value=(fsm, _filled_order(order_id="ord-1"))
        )
        await ex._place_market_order("BTC/USDT", "buy", 0.1)
        with pytest.raises(ccxt.ExchangeError, match="Order rate limit"):
            await ex._place_market_order("BTC/USDT", "buy", 0.1)
        # The refused order must never reach the exchange.
        assert ex._order_manager.place_order_with_fsm.await_count == 1
