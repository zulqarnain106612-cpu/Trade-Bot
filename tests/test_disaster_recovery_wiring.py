"""
Wiring tests for v8 startup reconciliation.

disaster_recovery.py had a pure, tested comparison and no caller, while
LiveExecutor.initialize() restored equity but not positions — so after a
crash the process treated a live book as flat. These cover the reconcile
call, the entry block it raises, and the explicit acknowledgement.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.config import TradingMode
from src.diagnostics.disaster_recovery import DiscrepancyType


def _make_executor(holdings, *, local=None, fetch_raises=False):
    """LiveExecutor with a stubbed exchange snapshot and local book."""
    from src.execution.live import LiveExecutor

    storage = MagicMock()
    storage.latest_equity = AsyncMock(return_value=None)
    fetcher = MagicMock()
    if fetch_raises:
        fetcher.fetch_exchange_holdings = AsyncMock(side_effect=RuntimeError("exchange down"))
    else:
        fetcher.fetch_exchange_holdings = AsyncMock(return_value=holdings)

    cfg = MagicMock()
    cfg.trading_mode = TradingMode.LIVE
    cfg.primary_symbol = "BTC/USDT"
    cfg.starting_capital_usd = 1000.0
    cfg.risk.notional_limit_usd = 100.0
    cfg.order_throttle.rate = 5.0
    cfg.order_throttle.burst = 5
    with patch("src.execution.live.get_settings", return_value=cfg):
        executor = LiveExecutor(storage, fetcher, starting_capital=1000.0)
    if local:
        for position in local:
            executor._positions[position.trade_id] = position
    return executor


def _local_position(symbol: str, direction: int, quantity: float):
    from src.execution.live import LivePosition

    return LivePosition(
        trade_id=f"t-{symbol}-{direction}",
        exchange_order_id="o-1",
        symbol=symbol,
        timeframe="15m",
        direction=direction,
        entry_price=100.0,
        quantity=quantity,
        notional_usd=quantity * 100.0,
        entry_ts=0,
        kelly_fraction=0.1,
        regime_at_entry=0,
        meta_label_prob=0.6,
        raw_signal=0.5,
        approved_by="auto",
        execution_mode="automatic",
        fee_usd=0.0,
    )


class TestReconcileOnInitialize:
    @pytest.mark.asyncio
    async def test_matching_books_leave_no_block(self) -> None:
        executor = _make_executor({"BTC/USDT": 0.5}, local=[_local_position("BTC/USDT", 1, 0.5)])
        await executor.initialize()
        assert executor.recovery_discrepancies == []

    @pytest.mark.asyncio
    async def test_both_books_empty_is_consistent(self) -> None:
        executor = _make_executor({"BTC/USDT": 0.0})
        await executor.initialize()
        assert executor.recovery_discrepancies == []

    @pytest.mark.asyncio
    async def test_exchange_position_unknown_locally_is_flagged(self) -> None:
        """The crash case: initialize() restores equity but not positions."""
        executor = _make_executor({"BTC/USDT": 0.5})
        await executor.initialize()
        found = executor.recovery_discrepancies
        assert len(found) == 1
        assert found[0].discrepancy_type is DiscrepancyType.MISSING_LOCALLY
        assert found[0].reference_quantity == pytest.approx(0.5)

    @pytest.mark.asyncio
    async def test_local_position_absent_on_exchange_is_flagged(self) -> None:
        executor = _make_executor(
            {"BTC/USDT": 0.0, "ETH/USDT": 0.0}, local=[_local_position("ETH/USDT", 1, 2.0)]
        )
        await executor.initialize()
        found = executor.recovery_discrepancies
        assert len(found) == 1
        assert found[0].discrepancy_type is DiscrepancyType.MISSING_ON_EXCHANGE

    @pytest.mark.asyncio
    async def test_quantity_mismatch_is_flagged(self) -> None:
        executor = _make_executor({"BTC/USDT": 0.3}, local=[_local_position("BTC/USDT", 1, 0.5)])
        await executor.initialize()
        assert executor.recovery_discrepancies[0].discrepancy_type is (
            DiscrepancyType.QUANTITY_MISMATCH
        )

    @pytest.mark.asyncio
    async def test_a_local_short_cannot_match_a_spot_balance(self) -> None:
        """A spot venue holds no shorts, so a local short is a real disagreement."""
        executor = _make_executor({"BTC/USDT": 0.5}, local=[_local_position("BTC/USDT", -1, 0.5)])
        await executor.initialize()
        # A spot venue cannot be short, so a local short against a positive
        # balance is a real disagreement, not a sign convention artefact.
        assert executor.recovery_discrepancies != []

    @pytest.mark.asyncio
    async def test_a_local_short_against_a_balance_is_a_mismatch(self) -> None:
        executor = _make_executor({"BTC/USDT": 0.5}, local=[_local_position("BTC/USDT", -1, 0.5)])
        await executor.initialize()
        assert executor.recovery_discrepancies != []

    @pytest.mark.asyncio
    async def test_unavailable_snapshot_blocks_rather_than_reporting_clean(self) -> None:
        """Failing to look is not the same as looking and finding nothing."""
        executor = _make_executor(None)
        await executor.initialize()
        assert executor.recovery_discrepancies != []

    @pytest.mark.asyncio
    async def test_fetch_exception_blocks_too(self) -> None:
        executor = _make_executor(None, fetch_raises=True)
        await executor.initialize()
        assert executor.recovery_discrepancies != []

    @pytest.mark.asyncio
    async def test_initialize_still_completes_when_blocked(self) -> None:
        """A block must not leave the executor half-initialized."""
        executor = _make_executor(None)
        await executor.initialize()
        assert executor._initialized is True


class TestEntryBlock:
    @pytest.mark.asyncio
    async def test_submit_signal_is_rejected_while_unreconciled(self) -> None:
        executor = _make_executor({"BTC/USDT": 0.5})
        await executor.initialize()
        trade_id, outcome = await executor.submit_signal(
            symbol="BTC/USDT",
            timeframe="15m",
            direction=1,
            kelly_result=MagicMock(notional_usd=50.0, quantity=0.001),
            regime_state=0,
            meta_label_prob=0.6,
            raw_signal=0.5,
            current_price=60_000.0,
        )
        assert trade_id is None
        assert outcome == "rejected"

    @pytest.mark.asyncio
    async def test_acknowledgement_lifts_the_block(self) -> None:
        executor = _make_executor({"BTC/USDT": 0.5})
        await executor.initialize()
        cleared = executor.acknowledge_recovery("ops")
        assert cleared == 1
        assert executor.recovery_discrepancies == []

    @pytest.mark.asyncio
    async def test_acknowledgement_is_explicit_only(self) -> None:
        """Nothing re-runs reconciliation and silently clears the block."""
        executor = _make_executor({"BTC/USDT": 0.5})
        await executor.initialize()
        await executor._reconcile_with_exchange()
        assert executor.recovery_discrepancies != []


class TestFetchExchangeHoldings:
    def _fetcher(self, balance, *, raises=None):
        from src.data.fetcher import MarketDataFetcher

        fetcher = MarketDataFetcher.__new__(MarketDataFetcher)
        exchange = MagicMock()
        exchange.fetch_balance = AsyncMock(
            side_effect=raises if raises else None,
            return_value=balance,
        )
        fetcher.get_order_exchange = lambda: exchange  # type: ignore[method-assign]
        return fetcher

    @pytest.mark.asyncio
    async def test_maps_the_base_asset_balance_per_symbol(self) -> None:
        fetcher = self._fetcher({"total": {"BTC": 0.5, "ETH": 2.0, "USDT": 900.0}})
        got = await fetcher.fetch_exchange_holdings(["BTC/USDT", "ETH/USDT"])
        assert got == {"BTC/USDT": 0.5, "ETH/USDT": 2.0}

    @pytest.mark.asyncio
    async def test_uses_total_so_resting_orders_still_count(self) -> None:
        """Locked quantity is still owned; free alone under-reports the book."""
        fetcher = self._fetcher({"free": {"BTC": 0.1}, "total": {"BTC": 0.5}})
        assert await fetcher.fetch_exchange_holdings(["BTC/USDT"]) == {"BTC/USDT": 0.5}

    @pytest.mark.asyncio
    async def test_an_unheld_asset_reports_zero_not_missing(self) -> None:
        fetcher = self._fetcher({"total": {"USDT": 900.0}})
        assert await fetcher.fetch_exchange_holdings(["BTC/USDT"]) == {"BTC/USDT": 0.0}

    @pytest.mark.asyncio
    async def test_perp_style_symbols_resolve_to_the_same_base(self) -> None:
        fetcher = self._fetcher({"total": {"BTC": 0.5}})
        assert await fetcher.fetch_exchange_holdings(["BTC/USDT:USDT"]) == {"BTC/USDT:USDT": 0.5}

    @pytest.mark.asyncio
    async def test_unparseable_balance_entries_read_as_zero(self) -> None:
        fetcher = self._fetcher({"total": {"BTC": "not-a-number"}})
        assert await fetcher.fetch_exchange_holdings(["BTC/USDT"]) == {"BTC/USDT": 0.0}

    @pytest.mark.asyncio
    async def test_no_symbols_requested_needs_no_call(self) -> None:
        fetcher = self._fetcher({"total": {}})
        assert await fetcher.fetch_exchange_holdings([]) == {}

    @pytest.mark.asyncio
    async def test_failure_returns_none_not_an_empty_book(self) -> None:
        """{} asserts the exchange holds nothing; None says we could not tell."""
        fetcher = self._fetcher(None, raises=RuntimeError("boom"))
        assert await fetcher.fetch_exchange_holdings(["BTC/USDT"]) is None
