"""Coverage for OrderbookStream's handling, buffering and flush paths.

The websocket loops are driven with a fake connection so the reconnect and
quality-rejection branches are exercised without a live Binance feed.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest
from websockets.exceptions import ConnectionClosed

from src.data.orderbook_stream import OrderbookSnapshot, OrderbookStream, TradeEvent


def _depth(best_bid: float = 100.0, best_ask: float = 100.1) -> dict:
    return {"bids": [[str(best_bid), "1.0"]], "asks": [[str(best_ask), "2.0"]]}


def _trade(price: float = 100.0, qty: float = 0.5, maker: bool = True) -> dict:
    return {"T": 1_700_000_000_000, "p": str(price), "q": str(qty), "m": maker}


def _stream(tmp_path: Path) -> OrderbookStream:
    return OrderbookStream(symbol="btcusdt", data_root=tmp_path)


class TestDepthHandling:
    def test_a_valid_update_becomes_a_snapshot(self, tmp_path: Path) -> None:
        stream = _stream(tmp_path)
        stream._handle_depth(_depth())
        snap = stream.latest_snapshot()
        assert snap is not None
        assert snap.mid == pytest.approx(100.05)
        assert json.loads(snap.bids_json) == [["100.0", "1.0"]]

    def test_a_wide_spread_is_rejected(self, tmp_path: Path) -> None:
        stream = _stream(tmp_path)
        stream._handle_depth(_depth(best_bid=100.0, best_ask=200.0))
        assert stream.latest_snapshot() is None

    def test_an_update_missing_a_side_is_ignored(self, tmp_path: Path) -> None:
        stream = _stream(tmp_path)
        stream._handle_depth({"bids": [], "asks": [["100.1", "1.0"]]})
        stream._handle_depth({"bids": [["100.0", "1.0"]], "asks": []})
        stream._handle_depth({})
        assert stream.latest_snapshot() is None

    def test_the_snapshot_is_published_to_the_shared_cache(self, tmp_path: Path) -> None:
        stream = _stream(tmp_path)
        cache = MagicMock()
        with patch("src.data.provider_cache.get_provider_cache", return_value=cache):
            stream._handle_depth(_depth())
        cache.set_orderbook.assert_called_once()
        assert isinstance(cache.set_orderbook.call_args[0][1], pd.DataFrame)

    def test_a_cache_fault_does_not_lose_the_snapshot(self, tmp_path: Path) -> None:
        stream = _stream(tmp_path)
        with patch("src.data.provider_cache.get_provider_cache", side_effect=RuntimeError("down")):
            stream._handle_depth(_depth())
        assert stream.latest_snapshot() is not None

    def test_the_buffer_flushes_at_a_thousand_snapshots(self, tmp_path: Path) -> None:
        stream = _stream(tmp_path)
        stream._snapshots = [_snapshot() for _ in range(999)]
        stream._handle_depth(_depth())
        assert stream._snapshots == []
        assert list((tmp_path / "orderbook" / "btcusdt").glob("*.parquet"))


def _snapshot() -> OrderbookSnapshot:
    from datetime import UTC, datetime

    return OrderbookSnapshot(
        timestamp_utc=datetime.now(UTC),
        bids_json="[]",
        asks_json="[]",
        mid=100.0,
        spread_bps=1.0,
    )


class TestSnapshotFrame:
    def test_the_cache_frame_is_capped_at_a_hundred_rows(self, tmp_path: Path) -> None:
        stream = _stream(tmp_path)
        stream._snapshots = [_snapshot() for _ in range(250)]
        assert len(stream._snapshots_as_df()) == 100

    def test_an_empty_buffer_gives_an_empty_frame(self, tmp_path: Path) -> None:
        assert _stream(tmp_path)._snapshots_as_df().empty


class TestOrderbookFlush:
    def test_an_empty_buffer_writes_nothing(self, tmp_path: Path) -> None:
        _stream(tmp_path)._flush_orderbook()
        assert not (tmp_path / "orderbook").exists()

    def test_a_flush_writes_and_clears(self, tmp_path: Path) -> None:
        stream = _stream(tmp_path)
        stream._snapshots = [_snapshot() for _ in range(3)]
        stream._flush_orderbook()
        [path] = list((tmp_path / "orderbook" / "btcusdt").glob("*.parquet"))
        assert len(pd.read_parquet(path)) == 3
        assert stream._snapshots == []

    def test_a_second_flush_appends_to_the_same_file(self, tmp_path: Path) -> None:
        stream = _stream(tmp_path)
        stream._snapshots = [_snapshot() for _ in range(2)]
        stream._flush_orderbook()
        stream._snapshots = [_snapshot() for _ in range(3)]
        stream._flush_orderbook()
        [path] = list((tmp_path / "orderbook" / "btcusdt").glob("*.parquet"))
        assert len(pd.read_parquet(path)) == 5


class TestTradeHandling:
    def test_a_trade_message_becomes_an_event(self, tmp_path: Path) -> None:
        stream = _stream(tmp_path)
        stream._handle_trade(_trade(price=101.5, qty=0.25, maker=False))
        [evt] = stream.recent_trades()
        assert (evt.price, evt.qty, evt.is_buyer_maker) == (101.5, 0.25, False)

    def test_recent_trades_returns_only_the_tail(self, tmp_path: Path) -> None:
        stream = _stream(tmp_path)
        for i in range(10):
            stream._handle_trade(_trade(price=float(i)))
        assert [t.price for t in stream.recent_trades(3)] == [7.0, 8.0, 9.0]

    def test_the_buffer_flushes_at_five_thousand_trades(self, tmp_path: Path) -> None:
        stream = _stream(tmp_path)
        stream._trades = [_event() for _ in range(4999)]
        stream._handle_trade(_trade())
        assert stream._trades == []
        assert list((tmp_path / "trades" / "btcusdt").glob("*.parquet"))


def _event() -> TradeEvent:
    from datetime import UTC, datetime

    return TradeEvent(timestamp_utc=datetime.now(UTC), price=100.0, qty=1.0, is_buyer_maker=True)


class TestTradeFlush:
    def test_an_empty_buffer_writes_nothing(self, tmp_path: Path) -> None:
        _stream(tmp_path)._flush_trades()
        assert not (tmp_path / "trades").exists()

    def test_a_flush_writes_and_clears(self, tmp_path: Path) -> None:
        stream = _stream(tmp_path)
        stream._trades = [_event() for _ in range(4)]
        stream._flush_trades()
        [path] = list((tmp_path / "trades" / "btcusdt").glob("*.parquet"))
        assert len(pd.read_parquet(path)) == 4
        assert stream._trades == []

    def test_a_second_flush_appends_to_the_same_file(self, tmp_path: Path) -> None:
        stream = _stream(tmp_path)
        stream._trades = [_event() for _ in range(2)]
        stream._flush_trades()
        stream._trades = [_event() for _ in range(2)]
        stream._flush_trades()
        [path] = list((tmp_path / "trades" / "btcusdt").glob("*.parquet"))
        assert len(pd.read_parquet(path)) == 4


class _FakeWS:
    """Async context manager yielding a fixed set of raw messages."""

    def __init__(self, messages: list[str], stream: OrderbookStream) -> None:
        self._messages = messages
        self._stream = stream

    async def __aenter__(self) -> _FakeWS:
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    async def __aiter__(self):
        for msg in self._messages:
            yield msg
        # Stop the outer reconnect loop once the messages are drained.
        self._stream.stop()


class TestStreamLoops:
    @pytest.mark.asyncio
    async def test_depth_messages_are_consumed(self, tmp_path: Path) -> None:
        stream = _stream(tmp_path)
        stream._running = True
        ws = _FakeWS([json.dumps(_depth())], stream)
        with patch("src.data.orderbook_stream.websockets.connect", return_value=ws):
            await stream._stream_depth()
        assert stream.latest_snapshot() is not None

    @pytest.mark.asyncio
    async def test_trade_messages_are_consumed(self, tmp_path: Path) -> None:
        stream = _stream(tmp_path)
        stream._running = True
        ws = _FakeWS([json.dumps(_trade())], stream)
        with patch("src.data.orderbook_stream.websockets.connect", return_value=ws):
            await stream._stream_trades()
        assert len(stream.recent_trades()) == 1

    @pytest.mark.asyncio
    async def test_a_closed_connection_is_retried(self, tmp_path: Path) -> None:
        stream = _stream(tmp_path)
        stream._running = True

        def _connect(*_a: object, **_k: object):
            stream.stop()
            raise ConnectionClosed(None, None)

        with (
            patch("src.data.orderbook_stream.websockets.connect", side_effect=_connect),
            patch("asyncio.sleep") as sleep,
        ):
            await stream._stream_depth()
        sleep.assert_awaited_once_with(1)

    @pytest.mark.asyncio
    async def test_an_unexpected_error_backs_off_further(self, tmp_path: Path) -> None:
        stream = _stream(tmp_path)
        stream._running = True

        def _connect(*_a: object, **_k: object):
            stream.stop()
            raise RuntimeError("dns failure")

        with (
            patch("src.data.orderbook_stream.websockets.connect", side_effect=_connect),
            patch("asyncio.sleep") as sleep,
        ):
            await stream._stream_trades()
        sleep.assert_awaited_once_with(2)

    @pytest.mark.asyncio
    async def test_stop_is_honoured_mid_stream(self, tmp_path: Path) -> None:
        stream = _stream(tmp_path)
        stream._running = False
        with patch("src.data.orderbook_stream.websockets.connect") as connect:
            await stream._stream_depth()
        connect.assert_not_called()

    @pytest.mark.asyncio
    async def test_start_runs_both_streams(self, tmp_path: Path) -> None:
        stream = _stream(tmp_path)
        with (
            patch.object(stream, "_stream_depth") as depth,
            patch.object(stream, "_stream_trades") as trades,
        ):
            await stream.start()
        assert stream._running is True
        depth.assert_called_once()
        trades.assert_called_once()
