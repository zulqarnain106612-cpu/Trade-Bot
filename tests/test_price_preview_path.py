"""
INV-033 -- the sub-second display path reads prices and never advances risk
state.

The streamed mid arrives roughly twenty times as often as the 5s REST poll it
sits beside. Every value the exit logic consults is a running extreme --
``PaperPosition.peak_unrealized_pct`` behind the trailing stop,
``PaperExecutor._peak_equity`` and the drawdown tracker behind
``check_daily_drawdown`` -- so a refresh that went through ``mark``/
``mark_to_market`` would raise those peaks against highs no exit rule ever
evaluated, and gates would start tripping on spikes the slow path never saw.
The display therefore gets its own read-only pair, ``unrealized_at`` and
``preview_marked_equity``, and its own loop.

What is pinned here: the preview's arithmetic matches ``mark``'s, the preview
mutates nothing, the preview loop publishes only what it read, and neither
loop can die on a bad tick -- a price feed that ends silently is the failure
a latency fix is most likely to introduce.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from src.engine.orchestrator import Orchestrator
from src.eventbus import EventBus
from src.execution.paper import PaperExecutor, PaperPosition

# ---------------------------------------------------------------------------
# Fixtures and doubles
# ---------------------------------------------------------------------------


class _Clock:
    """
    Stands in for ``asyncio.sleep`` so the loops run at full speed.

    Each loop is ``while self._running`` around a sleep, so the clock is also
    the only termination lever a test has: it stops the orchestrator after
    ``stop_after`` sleeps instead of waiting for one.
    """

    def __init__(self, orch: Orchestrator, stop_after: int = 1) -> None:
        self.orch = orch
        self.stop_after = stop_after
        self.delays: list[float] = []

    async def sleep(self, delay: float) -> None:
        self.delays.append(delay)
        if len(self.delays) >= self.stop_after:
            self.orch._running = False


def _orchestrator(monkeypatch, tmp_path: Path, *, stop_after: int = 1) -> tuple[Any, _Clock]:
    """An Orchestrator with only what the two price-path loops touch."""
    orch = object.__new__(Orchestrator)
    orch._log = MagicMock()
    orch._running = True
    orch._symbol = "BTC/USDT"
    orch._orderbook_stream = None
    cfg = MagicMock()
    cfg.storage.db_path = tmp_path / "trade.db"
    orch._cfg = cfg
    clock = _Clock(orch, stop_after=stop_after)
    monkeypatch.setattr(asyncio, "sleep", clock.sleep)
    return orch, clock


@pytest.fixture
def bus(monkeypatch) -> EventBus:
    """
    A private bus in place of the cached process-wide one.

    monkeypatch on the lookup rather than ``cache_clear()``: clearing would
    leak a fresh global into whatever ran next, which REG-0005 exists to stop.
    """
    replacement = EventBus()
    monkeypatch.setattr("src.engine.orchestrator.get_event_bus", lambda: replacement)
    return replacement


class _Stream:
    """An OrderbookStream stand-in: records construction, scripts start()."""

    instances: list[_Stream] = []

    def __init__(self, *, symbol: str, data_root: Path) -> None:
        self.symbol = symbol
        self.data_root = data_root
        self.starts = 0
        self.stopped = 0
        self.raises: BaseException | None = None
        _Stream.instances.append(self)

    async def start(self) -> None:
        self.starts += 1
        if self.raises is not None:
            raise self.raises

    def stop(self) -> None:
        self.stopped += 1


@pytest.fixture
def stream_cls(monkeypatch) -> type[_Stream]:
    _Stream.instances = []
    monkeypatch.setattr("src.engine.orchestrator.OrderbookStream", _Stream)
    return _Stream


def _position(direction: int = 1, *, entry: float = 100.0, qty: float = 2.0) -> PaperPosition:
    return PaperPosition(
        trade_id="t1",
        symbol="BTC/USDT",
        timeframe="5m",
        direction=direction,
        entry_price=entry,
        quantity=qty,
        notional_usd=entry * qty,
        entry_ts=1,
        kelly_fraction=0.1,
        regime_at_entry=1,
        meta_label_prob=0.6,
        raw_signal=0.5,
        approved_by="test",
        execution_mode="automatic",
        fee_usd=0.0,
    )


def _executor(*, cash: float = 10_000.0, positions: list[PaperPosition] | None = None) -> Any:
    """
    A PaperExecutor without ``__init__``: no settings load, no storage, no DB.

    ``preview_marked_equity`` reads exactly these four attributes, and the
    point of several tests below is that it writes none of them.
    """
    ex = object.__new__(PaperExecutor)
    ex._lock = asyncio.Lock()
    ex._cash = cash
    ex._peak_equity = cash
    ex._positions = {p.trade_id: p for p in (positions or [])}
    return ex


# ---------------------------------------------------------------------------
# The read-only arithmetic
# ---------------------------------------------------------------------------


class TestUnrealizedAt:
    @pytest.mark.parametrize(
        ("direction", "price", "expected"),
        [(1, 110.0, 20.0), (1, 90.0, -20.0), (0, 90.0, 20.0), (0, 110.0, -20.0)],
    )
    def test_it_matches_mark_for_both_directions(
        self, direction: int, price: float, expected: float
    ) -> None:
        """Same arithmetic as ``mark``; a display that disagrees is a bug report."""
        assert _position(direction).unrealized_at(price) == pytest.approx(expected)

    def test_it_does_not_advance_the_trailing_stop_input(self) -> None:
        """
        The whole reason this method exists. ``mark`` raises
        ``peak_unrealized_pct``, which the trailing stop reads; a price a
        dashboard merely displayed must not be able to close a position.
        """
        pos = _position()

        pos.unrealized_at(1_000.0)

        assert pos.peak_unrealized_pct == 0.0
        assert pos.unrealized_pnl == 0.0
        assert pos.current_price == 0.0


class TestPreviewMarkedEquity:
    async def test_flat_returns_none_so_nothing_is_published(self) -> None:
        """None rather than a zero snapshot: the caller skips the publish entirely."""
        assert await _executor().preview_marked_equity({"BTC/USDT": 100.0}) is None

    async def test_it_marks_at_the_supplied_price(self) -> None:
        ex = _executor(cash=9_800.0, positions=[_position()])

        snapshot = await ex.preview_marked_equity({"BTC/USDT": 110.0})

        assert snapshot == {
            "equity_usd": 9_820.0,
            "cash_usd": 9_800.0,
            "unrealized_pnl_usd": 20.0,
        }

    @pytest.mark.parametrize("prices", [{}, {"BTC/USDT": 0.0}, {"BTC/USDT": None}])
    async def test_an_unusable_price_falls_back_to_the_last_mark(self, prices: dict) -> None:
        """
        A symbol the stream does not carry, or a zero that means "no data",
        leaves that position reading at whatever the 5s path last marked --
        the preview never invents a price of its own.
        """
        pos = _position()
        pos.unrealized_pnl = 7.5
        ex = _executor(cash=9_800.0, positions=[pos])

        snapshot = await ex.preview_marked_equity(prices)

        assert snapshot is not None
        assert snapshot["unrealized_pnl_usd"] == pytest.approx(7.5)

    async def test_it_leaves_every_risk_input_where_it_found_it(self) -> None:
        """
        INV-033 at the executor: a preview at a new high moves neither
        ``_peak_equity`` nor the position's peak, so ``check_daily_drawdown``
        and the trailing stop see exactly what the slow path gave them.
        """
        pos = _position()
        ex = _executor(cash=9_800.0, positions=[pos])

        await ex.preview_marked_equity({"BTC/USDT": 10_000.0})

        assert ex._peak_equity == 9_800.0
        assert ex._cash == 9_800.0
        assert pos.peak_unrealized_pct == 0.0
        assert pos.unrealized_pnl == 0.0


# ---------------------------------------------------------------------------
# The stream owner
# ---------------------------------------------------------------------------


class TestOrderbookStreamLoop:
    async def test_it_builds_the_stream_for_the_primary_symbol(
        self, monkeypatch, tmp_path: Path, stream_cls: type[_Stream]
    ) -> None:
        """
        Binance's websocket wants ``btcusdt``, not ``BTC/USDT``. The data root
        is the storage directory, not the database file.
        """
        orch, _clock = _orchestrator(monkeypatch, tmp_path)

        await orch._orderbook_stream_loop()

        built = stream_cls.instances[0]
        assert built.symbol == "btcusdt"
        assert built.data_root == tmp_path
        assert orch._orderbook_stream is built

    async def test_a_dropped_socket_is_retried_rather_than_abandoned(
        self, monkeypatch, tmp_path: Path, stream_cls: type[_Stream]
    ) -> None:
        """
        The failure this prevents: ``start()`` returns on error, so a loop
        that merely logged would end the price feed for the life of the
        process and leave mark-to-market silently back on the REST poll.

        30s rather than 5: on a host with no outbound network the retry never
        succeeds, and a 5s cadence buries the operator's log in warnings.
        """
        orch, clock = _orchestrator(monkeypatch, tmp_path, stop_after=2)
        stream_cls.instances.clear()

        def _factory(*, symbol: str, data_root: Path) -> _Stream:
            built = _Stream(symbol=symbol, data_root=data_root)
            built.raises = RuntimeError("socket gone")
            return built

        monkeypatch.setattr("src.engine.orchestrator.OrderbookStream", _factory)

        await orch._orderbook_stream_loop()

        built = stream_cls.instances[0]
        assert built.starts == 2
        assert clock.delays == [30.0, 30.0]

    async def test_an_error_is_logged_and_does_not_escape(
        self, monkeypatch, tmp_path: Path, stream_cls: type[_Stream]
    ) -> None:
        orch, _clock = _orchestrator(monkeypatch, tmp_path)
        stream_cls.instances.clear()

        def _factory(*, symbol: str, data_root: Path) -> _Stream:
            built = _Stream(symbol=symbol, data_root=data_root)
            built.raises = RuntimeError("socket gone")
            return built

        monkeypatch.setattr("src.engine.orchestrator.OrderbookStream", _factory)

        await orch._orderbook_stream_loop()

        assert orch._log.warning.call_args[0][0] == "orchestrator.orderbook_stream_error"

    async def test_cancellation_stops_the_stream_and_propagates(
        self, monkeypatch, tmp_path: Path, stream_cls: type[_Stream]
    ) -> None:
        """
        Shutdown cancels this task. Swallowing the cancellation would hang
        the worker's 10s join; not closing the stream would leak two sockets.
        """
        orch, _clock = _orchestrator(monkeypatch, tmp_path)
        stream_cls.instances.clear()

        def _factory(*, symbol: str, data_root: Path) -> _Stream:
            built = _Stream(symbol=symbol, data_root=data_root)
            built.raises = asyncio.CancelledError()
            return built

        monkeypatch.setattr("src.engine.orchestrator.OrderbookStream", _factory)

        with pytest.raises(asyncio.CancelledError):
            await orch._orderbook_stream_loop()

        assert stream_cls.instances[0].stopped == 1


# ---------------------------------------------------------------------------
# The display loop
# ---------------------------------------------------------------------------


class _Mid:
    """A stream whose mid is scripted and whose mark path must stay unused."""

    def __init__(self, mid: float | None) -> None:
        self._mid = mid
        self.calls = 0

    def latest_mid(self) -> float | None:
        self.calls += 1
        return self._mid


class TestPricePreviewLoop:
    async def test_it_publishes_what_it_read_marked_not_authoritative(
        self, monkeypatch, tmp_path: Path, bus: EventBus
    ) -> None:
        """
        ``authoritative: False`` is the contract with the dashboard: this
        number came from the fast path and the 5s mark is still the record.
        """
        orch, _clock = _orchestrator(monkeypatch, tmp_path)
        orch._orderbook_stream = _Mid(110.0)
        executor = _executor(cash=9_800.0, positions=[_position()])
        orch._all_executors = lambda: [executor]
        subscription = bus.subscribe(["equity"])

        await orch._price_preview_loop()

        assert len(subscription._queue) == 1
        event = subscription._queue[0]
        assert event.data["equity_usd"] == 9_820.0
        assert event.data["mark_price"] == 110.0
        assert event.data["authoritative"] is False

    async def test_no_stream_yet_publishes_nothing(
        self, monkeypatch, tmp_path: Path, bus: EventBus
    ) -> None:
        """The loop starts before its sibling has built the stream."""
        orch, _clock = _orchestrator(monkeypatch, tmp_path)
        orch._all_executors = lambda: [_executor(positions=[_position()])]
        subscription = bus.subscribe(["equity"])

        await orch._price_preview_loop()

        assert not subscription._queue

    async def test_a_stale_mid_publishes_nothing(
        self, monkeypatch, tmp_path: Path, bus: EventBus
    ) -> None:
        """
        ``latest_mid()`` withholds a mid older than its bound. Publishing the
        last known price instead would show a frozen market as a live one.
        """
        orch, _clock = _orchestrator(monkeypatch, tmp_path)
        orch._orderbook_stream = _Mid(None)
        orch._all_executors = lambda: [_executor(positions=[_position()])]
        subscription = bus.subscribe(["equity"])

        await orch._price_preview_loop()

        assert not subscription._queue

    async def test_an_executor_without_a_preview_is_skipped(
        self, monkeypatch, tmp_path: Path, bus: EventBus
    ) -> None:
        """
        Live and shadow executors sit in the same list and only the paper one
        implements the read-only preview; the loop must not demand it.
        """
        orch, _clock = _orchestrator(monkeypatch, tmp_path)
        orch._orderbook_stream = _Mid(110.0)
        orch._all_executors = lambda: [object()]
        subscription = bus.subscribe(["equity"])

        await orch._price_preview_loop()

        assert not subscription._queue
        assert not orch._log.warning.called

    async def test_a_flat_executor_publishes_nothing(
        self, monkeypatch, tmp_path: Path, bus: EventBus
    ) -> None:
        orch, _clock = _orchestrator(monkeypatch, tmp_path)
        orch._orderbook_stream = _Mid(110.0)
        orch._all_executors = lambda: [_executor()]
        subscription = bus.subscribe(["equity"])

        await orch._price_preview_loop()

        assert not subscription._queue

    async def test_a_failing_preview_is_logged_and_the_loop_survives(
        self, monkeypatch, tmp_path: Path, bus: EventBus
    ) -> None:
        """
        One bad tick must not end the display feed: the loop keeps going and
        the next good tick publishes.
        """
        orch, clock = _orchestrator(monkeypatch, tmp_path, stop_after=2)
        orch._orderbook_stream = _Mid(110.0)
        calls: list[int] = []

        class _Broken:
            async def preview_marked_equity(self, prices: dict[str, float]) -> None:
                calls.append(1)
                raise RuntimeError("snapshot failed")

        orch._all_executors = lambda: [_Broken()]
        subscription = bus.subscribe(["equity"])

        await orch._price_preview_loop()

        assert len(calls) == 2
        assert clock.delays == [1.0, 1.0]
        assert not subscription._queue
        assert orch._log.warning.call_args[0][0] == "orchestrator.price_preview_error"

    async def test_cancellation_propagates(
        self, monkeypatch, tmp_path: Path, bus: EventBus
    ) -> None:
        """Shutdown must not have to wait out a swallowed cancellation."""
        orch, _clock = _orchestrator(monkeypatch, tmp_path)
        orch._orderbook_stream = _Mid(110.0)

        class _Cancelled:
            async def preview_marked_equity(self, prices: dict[str, float]) -> None:
                raise asyncio.CancelledError()

        orch._all_executors = lambda: [_Cancelled()]

        with pytest.raises(asyncio.CancelledError):
            await orch._price_preview_loop()

    async def test_the_display_path_never_calls_mark_to_market(
        self, monkeypatch, tmp_path: Path, bus: EventBus
    ) -> None:
        """
        INV-033 at the loop. ``mark_to_market`` advances ``_peak_equity``, the
        drawdown tracker and each position's peak -- inputs to
        ``check_daily_drawdown`` and the trailing stop. Running it at the
        stream's cadence would move live thresholds, so the preview loop is
        only ever allowed to read.
        """
        orch, _clock = _orchestrator(monkeypatch, tmp_path)
        orch._orderbook_stream = _Mid(110.0)
        executor = _executor(cash=9_800.0, positions=[_position()])
        executor.mark_to_market = MagicMock()
        orch._all_executors = lambda: [executor]

        await orch._price_preview_loop()

        assert not executor.mark_to_market.called
        assert executor._peak_equity == 9_800.0
