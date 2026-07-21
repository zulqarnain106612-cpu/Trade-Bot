"""Tests for src/data/fetcher.py — target 80%+ coverage."""

from __future__ import annotations

import asyncio
import threading
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import ccxt.async_support as ccxt_async
import pytest

from src.config import EXCHANGE_BINANCE, EXCHANGE_OKX, Timeframe
from src.data.fetcher import (
    MarketDataFetcher,
    OrderBookSnapshot,
    _build_binance,
    _build_okx,
    _parse_book_side,
    _raw_to_bar_records,
    _with_retry,
    open_fetcher,
)
from src.data.storage import BarRecord


# ---------------------------------------------------------------------------
# OrderBookSnapshot
# ---------------------------------------------------------------------------


class TestOrderBookSnapshot:
    def _snap(self, bids=None, asks=None) -> OrderBookSnapshot:
        return OrderBookSnapshot(
            symbol="BTC/USDT",
            ts_ms=1_700_000_000_000,
            bids=bids if bids is not None else [[100.0, 1.0], [99.0, 2.0]],
            asks=asks if asks is not None else [[101.0, 1.5], [102.0, 2.5]],
        )

    def test_bid_price_best_bid(self):
        assert self._snap().bid_price == 100.0

    def test_bid_price_empty_bids_returns_zero(self):
        assert self._snap(bids=[]).bid_price == 0.0

    def test_ask_price_best_ask(self):
        assert self._snap().ask_price == 101.0

    def test_ask_price_empty_asks_returns_zero(self):
        assert self._snap(asks=[]).ask_price == 0.0

    def test_mid_price(self):
        assert self._snap().mid_price == pytest.approx(100.5)

    def test_spread(self):
        assert self._snap().spread == pytest.approx(1.0)

    def test_bid_volume_sums_top_levels(self):
        assert self._snap().bid_volume(levels=2) == pytest.approx(3.0)

    def test_bid_volume_respects_level_limit(self):
        assert self._snap().bid_volume(levels=1) == pytest.approx(1.0)

    def test_ask_volume_sums_top_levels(self):
        assert self._snap().ask_volume(levels=2) == pytest.approx(4.0)

    def test_order_flow_imbalance_positive_when_bid_heavy(self):
        snap = self._snap(bids=[[100.0, 10.0]], asks=[[101.0, 1.0]])
        assert snap.order_flow_imbalance() > 0.0

    def test_order_flow_imbalance_negative_when_ask_heavy(self):
        snap = self._snap(bids=[[100.0, 1.0]], asks=[[101.0, 10.0]])
        assert snap.order_flow_imbalance() < 0.0

    def test_order_flow_imbalance_zero_when_book_empty(self):
        snap = self._snap(bids=[], asks=[])
        assert snap.order_flow_imbalance() == 0.0


# ---------------------------------------------------------------------------
# _parse_book_side
# ---------------------------------------------------------------------------


class TestParseBookSide:
    def test_parses_valid_rows(self):
        result = _parse_book_side([[100.0, 1.0], [99.5, 2.0]])
        assert result == [[100.0, 1.0], [99.5, 2.0]]

    def test_drops_wrong_arity_rows(self):
        result = _parse_book_side([[100.0, 1.0, 5.0], [99.5, 2.0]])
        assert result == [[99.5, 2.0]]

    def test_drops_non_numeric_rows(self):
        result = _parse_book_side([["bad", "row"], [99.5, 2.0]])
        assert result == [[99.5, 2.0]]

    def test_drops_non_positive_price(self):
        result = _parse_book_side([[0.0, 1.0], [-5.0, 1.0], [10.0, 1.0]])
        assert result == [[10.0, 1.0]]

    def test_drops_negative_quantity(self):
        result = _parse_book_side([[10.0, -1.0], [10.0, 1.0]])
        assert result == [[10.0, 1.0]]

    def test_empty_input_returns_empty(self):
        assert _parse_book_side([]) == []


# ---------------------------------------------------------------------------
# _raw_to_bar_records
# ---------------------------------------------------------------------------


def test_raw_to_bar_records_basic():
    raw = [[1_700_000_000_000, 30000.0, 30100.0, 29900.0, 30050.0, 1.5]]
    records = _raw_to_bar_records("BTC/USDT", "1h", raw)
    assert len(records) == 1
    r = records[0]
    assert r.symbol == "BTC/USDT"
    assert r.timeframe == "1h"
    assert r.open == 30000.0
    assert r.volume == 1.5
    assert r.quote_volume == 0.0
    assert r.taker_buy_vol == 0.0


def test_raw_to_bar_records_with_extended_fields():
    raw = [[1_700_000_000_000, 1.0, 2.0, 0.5, 1.5, 100.0, 200.0, 50.0]]
    records = _raw_to_bar_records("ETH/USDT", "15m", raw)
    assert records[0].quote_volume == 200.0
    assert records[0].taker_buy_vol == 50.0


def test_raw_to_bar_records_skips_short_rows():
    raw = [[1_700_000_000_000, 1.0, 2.0, 0.5]]  # only 4 fields
    records = _raw_to_bar_records("BTC/USDT", "1h", raw)
    assert records == []


def test_raw_to_bar_records_empty():
    records = _raw_to_bar_records("BTC/USDT", "1h", [])
    assert records == []


def test_raw_to_bar_records_none_extended():
    raw = [[1_700_000_000_000, 1.0, 2.0, 0.5, 1.5, 100.0, None, None]]
    records = _raw_to_bar_records("BTC/USDT", "1h", raw)
    assert records[0].quote_volume == 0.0
    assert records[0].taker_buy_vol == 0.0


# ---------------------------------------------------------------------------
# _with_retry
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_with_retry_success_on_first():
    async def _ok():
        return 42

    result = await _with_retry(lambda: _ok(), label="test")
    assert result == 42


@pytest.mark.asyncio
async def test_with_retry_success_after_network_error():
    calls = []

    async def _flaky():
        calls.append(1)
        if len(calls) < 3:
            raise ccxt_async.NetworkError("timeout")
        return "ok"

    with patch("asyncio.sleep", new=AsyncMock()):
        result = await _with_retry(lambda: _flaky(), label="test", attempts=5, base_delay=0.01)
    assert result == "ok"
    assert len(calls) == 3


@pytest.mark.asyncio
async def test_with_retry_raises_auth_error_immediately():
    async def _auth_fail():
        raise ccxt_async.AuthenticationError("bad key")

    with pytest.raises(ccxt_async.AuthenticationError):
        await _with_retry(lambda: _auth_fail(), label="test", attempts=3)


@pytest.mark.asyncio
async def test_with_retry_raises_exchange_error_immediately():
    async def _exc():
        raise ccxt_async.ExchangeError("exchange down")

    with pytest.raises(ccxt_async.ExchangeError):
        await _with_retry(lambda: _exc(), label="test", attempts=3)


@pytest.mark.asyncio
async def test_with_retry_raises_after_max_network_attempts():
    async def _always_fail():
        raise ccxt_async.NetworkError("network")

    with patch("asyncio.sleep", new=AsyncMock()):
        with pytest.raises(ccxt_async.NetworkError):
            await _with_retry(lambda: _always_fail(), label="test", attempts=3, base_delay=0.01)


@pytest.mark.asyncio
async def test_with_retry_raises_rate_limit_after_max_attempts():
    async def _rate_limit():
        raise ccxt_async.RateLimitExceeded("slow down")

    with patch("asyncio.sleep", new=AsyncMock()):
        with pytest.raises(ccxt_async.RateLimitExceeded):
            await _with_retry(lambda: _rate_limit(), label="test", attempts=3, base_delay=0.01)


@pytest.mark.asyncio
async def test_with_retry_rate_limit_retries():
    calls = []

    async def _rate_limit():
        calls.append(1)
        if len(calls) < 2:
            raise ccxt_async.RateLimitExceeded("slow")
        return "done"

    with patch("asyncio.sleep", new=AsyncMock()):
        result = await _with_retry(lambda: _rate_limit(), label="test", attempts=5, base_delay=0.01)
    assert result == "done"


@pytest.mark.asyncio
async def test_with_retry_request_timeout_retries():
    calls = []

    async def _timeout():
        calls.append(1)
        if len(calls) < 2:
            raise ccxt_async.RequestTimeout("timed out")
        return "ok"

    with patch("asyncio.sleep", new=AsyncMock()):
        result = await _with_retry(lambda: _timeout(), label="test", attempts=5, base_delay=0.01)
    assert result == "ok"


@pytest.mark.asyncio
async def test_with_retry_raises_invalid_order_immediately():
    async def _bad():
        raise ccxt_async.InvalidOrder("bad order")

    with pytest.raises(ccxt_async.InvalidOrder):
        await _with_retry(lambda: _bad(), label="test")


# ---------------------------------------------------------------------------
# _build_binance / _build_okx
# ---------------------------------------------------------------------------


def test_build_binance_sets_credentials():
    from src.config import BinanceSettings

    cfg = BinanceSettings(
        api_key="key123",  # pragma: allowlist secret
        api_secret="secret456",  # pragma: allowlist secret
        testnet=False,
    )
    exchange = _build_binance(cfg)
    assert exchange.apiKey == "key123"  # pragma: allowlist secret
    assert exchange.secret == "secret456"  # pragma: allowlist secret


def test_build_binance_testnet():
    from src.config import BinanceSettings

    cfg = BinanceSettings(api_key="k", api_secret="s", testnet=True)
    exchange = _build_binance(cfg)
    assert exchange.options.get("sandboxMode") is True


def test_build_okx_sets_credentials():
    from src.config import OKXSettings

    cfg = OKXSettings(api_key="k", api_secret="s", passphrase="pp", testnet=False)
    exchange = _build_okx(cfg)
    assert exchange.apiKey == "k"
    assert exchange.secret == "s"
    assert exchange.password == "pp"  # pragma: allowlist secret


def test_build_okx_testnet():
    from src.config import OKXSettings

    cfg = OKXSettings(api_key="k", api_secret="s", passphrase="p", testnet=True)
    exchange = _build_okx(cfg)
    assert exchange.options.get("sandboxMode") is True


# ---------------------------------------------------------------------------
# MarketDataFetcher lifecycle
# ---------------------------------------------------------------------------


def _make_storage() -> MagicMock:
    storage = MagicMock()
    storage.upsert_bars = AsyncMock(return_value=5)
    storage.latest_bar_ts = AsyncMock(return_value=None)
    return storage


def _make_fetcher() -> MarketDataFetcher:
    return MarketDataFetcher(_make_storage())


def test_fetcher_init():
    f = _make_fetcher()
    assert f._binance is None
    assert f._okx is None


def test_require_binance_raises_before_init():
    f = _make_fetcher()
    with pytest.raises(RuntimeError, match="not initialized"):
        f._require_binance()


def test_require_okx_raises_before_init():
    f = _make_fetcher()
    with pytest.raises(RuntimeError, match="not initialized"):
        f._require_okx()


def test_get_order_exchange_raises_before_init():
    f = _make_fetcher()
    with pytest.raises(RuntimeError, match="not initialized"):
        f.get_order_exchange()


def test_get_sem_lazy_creates():
    f = _make_fetcher()
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(asyncio.coroutine(lambda: f._get_sem())())
    except Exception:
        # run_until_complete approach won't work with new_event_loop outside context
        pass
    finally:
        loop.close()


@pytest.mark.asyncio
async def test_get_sem_returns_semaphore():
    f = _make_fetcher()
    sem = f._get_sem()
    assert isinstance(sem, asyncio.Semaphore)


@pytest.mark.asyncio
async def test_get_sem_idempotent():
    f = _make_fetcher()
    s1 = f._get_sem()
    s2 = f._get_sem()
    assert s1 is s2


@pytest.mark.asyncio
async def test_get_sem_double_checked_lock_race_reuses_winner():
    """VF-012: if a second caller acquires the guard lock after a first
    caller already set _gap_fill_sem, the inner `if self._gap_fill_sem is
    None` re-check must skip creating a second Semaphore and reuse the
    winner -- simulated here with a lock stand-in that sets the sentinel
    from inside __enter__, exactly as a genuinely concurrent second thread
    would (threading.Lock is a C type and can't be monkeypatched directly)."""
    f = _make_fetcher()
    winner = asyncio.Semaphore(1)
    real_lock = threading.Lock()

    class _RacingLock:
        def __enter__(self):
            real_lock.__enter__()
            f._gap_fill_sem = winner  # another "thread" wins the race first
            return self

        def __exit__(self, *exc_info):
            return real_lock.__exit__(*exc_info)

    f._sem_init_guard = _RacingLock()  # type: ignore[assignment]
    sem = f._get_sem()
    assert sem is winner


@pytest.mark.asyncio
async def test_close_without_initialize():
    f = _make_fetcher()
    await f.close()  # should not raise


@pytest.mark.asyncio
async def test_initialize_and_close():
    f = _make_fetcher()
    mock_binance = MagicMock()
    mock_binance.load_markets = AsyncMock(return_value={})
    mock_binance.close = AsyncMock()
    mock_okx = MagicMock()
    mock_okx.load_markets = AsyncMock(return_value={})
    mock_okx.close = AsyncMock()

    with (
        patch("src.data.fetcher._build_binance", return_value=mock_binance),
        patch("src.data.fetcher._build_okx", return_value=mock_okx),
    ):
        await f.initialize()
        assert f._binance is mock_binance
        assert f._okx is mock_okx
        await f.close()
        mock_binance.close.assert_awaited_once()
        mock_okx.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_get_order_exchange_after_init():
    f = _make_fetcher()
    mock_binance = MagicMock()
    mock_binance.load_markets = AsyncMock(return_value={})
    mock_okx = MagicMock()
    mock_okx.load_markets = AsyncMock(return_value={})

    with (
        patch("src.data.fetcher._build_binance", return_value=mock_binance),
        patch("src.data.fetcher._build_okx", return_value=mock_okx),
    ):
        await f.initialize()
        assert f.get_order_exchange() is mock_binance


# ---------------------------------------------------------------------------
# bootstrap_history
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bootstrap_history_basic():
    storage = _make_storage()
    storage.upsert_bars = AsyncMock(return_value=10)
    f = MarketDataFetcher(storage)

    raw_page = [
        [int(datetime.now(UTC).timestamp() * 1000) - i * 3600_000, 1.0, 2.0, 0.5, 1.5, 100.0]
        for i in range(5)
    ]

    mock_exchange = MagicMock()
    mock_exchange.fetch_ohlcv = AsyncMock(side_effect=[raw_page, []])  # second call returns empty
    f._binance = mock_exchange

    count = await f.bootstrap_history("BTC/USDT", Timeframe.INTRADAY, lookback_days=1)
    assert count == 10


@pytest.mark.asyncio
async def test_bootstrap_history_stops_on_empty():
    storage = _make_storage()
    f = MarketDataFetcher(storage)

    mock_exchange = MagicMock()
    mock_exchange.fetch_ohlcv = AsyncMock(return_value=[])
    f._binance = mock_exchange

    count = await f.bootstrap_history("BTC/USDT", Timeframe.INTRADAY, lookback_days=1)
    assert count == 0


@pytest.mark.asyncio
async def test_bootstrap_history_stops_below_max_bars():
    storage = _make_storage()
    storage.upsert_bars = AsyncMock(return_value=3)
    f = MarketDataFetcher(storage)

    # Return fewer than _MAX_BARS_PER_REQUEST bars → stop
    raw = [
        [int(datetime.now(UTC).timestamp() * 1000) - i * 3600_000, 1.0, 2.0, 0.5, 1.5, 100.0]
        for i in range(3)
    ]
    mock_exchange = MagicMock()
    mock_exchange.fetch_ohlcv = AsyncMock(return_value=raw)
    f._binance = mock_exchange

    count = await f.bootstrap_history("BTC/USDT", Timeframe.INTRADAY, lookback_days=1)
    assert count == 3


@pytest.mark.asyncio
async def test_bootstrap_history_paginates_on_full_page():
    """A full page (== _MAX_BARS_PER_REQUEST) must trigger another fetch
    instead of stopping -- exercises the pagination continue branch."""
    from src.data.fetcher import _MAX_BARS_PER_REQUEST

    storage = _make_storage()
    storage.upsert_bars = AsyncMock(return_value=1)
    f = MarketDataFetcher(storage)

    full_page = [
        [int(datetime.now(UTC).timestamp() * 1000) - i * 3600_000, 1.0, 2.0, 0.5, 1.5, 1.0]
        for i in range(_MAX_BARS_PER_REQUEST)
    ]
    mock_exchange = MagicMock()
    mock_exchange.fetch_ohlcv = AsyncMock(side_effect=[full_page, []])
    f._binance = mock_exchange

    with patch("asyncio.sleep", new=AsyncMock(return_value=None)):
        count = await f.bootstrap_history("BTC/USDT", Timeframe.INTRADAY, lookback_days=1)
    assert count == 1
    assert mock_exchange.fetch_ohlcv.await_count == 2


# ---------------------------------------------------------------------------
# gap_fill
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gap_fill_no_stored_data_bootstraps():
    storage = _make_storage()
    storage.latest_bar_ts = AsyncMock(return_value=None)
    f = MarketDataFetcher(storage)

    mock_exchange = MagicMock()
    mock_exchange.fetch_ohlcv = AsyncMock(return_value=[])
    f._binance = mock_exchange

    count = await f.gap_fill("BTC/USDT", Timeframe.INTRADAY)
    assert count == 0  # bootstrap returns 0 (no bars)


@pytest.mark.asyncio
async def test_gap_fill_already_current():
    storage = _make_storage()
    future_ts = int(datetime.now(UTC).timestamp() * 1000) + 3_600_000
    storage.latest_bar_ts = AsyncMock(return_value=future_ts)
    f = MarketDataFetcher(storage)
    f._binance = MagicMock()

    count = await f.gap_fill("BTC/USDT", Timeframe.INTRADAY)
    assert count == 0


@pytest.mark.asyncio
async def test_gap_fill_fetches_new_bars():
    storage = _make_storage()
    past_ts = int(datetime.now(UTC).timestamp() * 1000) - 2 * 3_600_000
    storage.latest_bar_ts = AsyncMock(return_value=past_ts)
    storage.upsert_bars = AsyncMock(return_value=2)
    f = MarketDataFetcher(storage)

    raw = [
        [int(datetime.now(UTC).timestamp() * 1000) - i * 1800_000, 1.0, 2.0, 0.5, 1.5, 10.0]
        for i in range(2)
    ]
    mock_exchange = MagicMock()
    mock_exchange.fetch_ohlcv = AsyncMock(return_value=raw)
    f._binance = mock_exchange

    count = await f.gap_fill("BTC/USDT", Timeframe.INTRADAY)
    assert count == 2


@pytest.mark.asyncio
async def test_gap_fill_empty_response():
    storage = _make_storage()
    past_ts = int(datetime.now(UTC).timestamp() * 1000) - 2 * 3_600_000
    storage.latest_bar_ts = AsyncMock(return_value=past_ts)
    f = MarketDataFetcher(storage)

    mock_exchange = MagicMock()
    mock_exchange.fetch_ohlcv = AsyncMock(return_value=[])
    f._binance = mock_exchange

    count = await f.gap_fill("BTC/USDT", Timeframe.INTRADAY)
    assert count == 0


# ---------------------------------------------------------------------------
# gap_fill_all_timeframes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gap_fill_all_timeframes():
    storage = _make_storage()
    past_ts = int(datetime.now(UTC).timestamp() * 1000) - 2 * 3_600_000
    storage.latest_bar_ts = AsyncMock(return_value=past_ts)
    storage.upsert_bars = AsyncMock(return_value=1)
    f = MarketDataFetcher(storage)

    raw = [[int(datetime.now(UTC).timestamp() * 1000) - 3600_000, 1.0, 2.0, 0.5, 1.5, 10.0]]
    mock_exchange = MagicMock()
    mock_exchange.fetch_ohlcv = AsyncMock(return_value=raw)
    f._binance = mock_exchange

    result = await f.gap_fill_all_timeframes("BTC/USDT", [Timeframe.INTRADAY])
    assert "15m" in result


@pytest.mark.asyncio
async def test_gap_fill_all_timeframes_handles_exchange_error():
    storage = _make_storage()
    past_ts = int(datetime.now(UTC).timestamp() * 1000) - 2 * 3_600_000
    storage.latest_bar_ts = AsyncMock(return_value=past_ts)
    f = MarketDataFetcher(storage)

    mock_exchange = MagicMock()
    mock_exchange.fetch_ohlcv = AsyncMock(side_effect=ccxt_async.NetworkError("fail"))
    f._binance = mock_exchange

    with patch("asyncio.sleep", new=AsyncMock()):
        result = await f.gap_fill_all_timeframes("BTC/USDT", [Timeframe.INTRADAY])
    assert result.get("15m") == 0


# ---------------------------------------------------------------------------
# fetch_ohlcv_okx
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_ohlcv_okx():
    storage = _make_storage()
    f = MarketDataFetcher(storage)

    raw = [[int(datetime.now(UTC).timestamp() * 1000), 1.0, 2.0, 0.5, 1.5, 100.0]]
    mock_okx = MagicMock()
    mock_okx.fetch_ohlcv = AsyncMock(return_value=raw)
    f._okx = mock_okx

    bars = await f.fetch_ohlcv_okx("BTC/USDT", Timeframe.INTRADAY, since_ms=0)
    assert len(bars) == 1
    assert isinstance(bars[0], BarRecord)


# ---------------------------------------------------------------------------
# fetch_orderbook
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_orderbook_binance():
    storage = _make_storage()
    f = MarketDataFetcher(storage)

    raw = {
        "timestamp": 1_700_000_000_000,
        "bids": [[30000.0, 1.0], [29999.0, 2.0]],
        "asks": [[30001.0, 1.5]],
    }
    mock_exchange = MagicMock()
    mock_exchange.fetch_order_book = AsyncMock(return_value=raw)
    f._binance = mock_exchange

    snap = await f.fetch_orderbook("BTC/USDT", exchange_id=EXCHANGE_BINANCE)
    assert isinstance(snap, OrderBookSnapshot)
    assert len(snap.bids) == 2
    assert len(snap.asks) == 1


@pytest.mark.asyncio
async def test_fetch_orderbook_okx():
    storage = _make_storage()
    f = MarketDataFetcher(storage)

    raw = {"timestamp": None, "bids": [[1.0, 1.0]], "asks": [[2.0, 0.5]]}
    mock_okx = MagicMock()
    mock_okx.fetch_order_book = AsyncMock(return_value=raw)
    f._okx = mock_okx

    snap = await f.fetch_orderbook("BTC/USDT", exchange_id=EXCHANGE_OKX)
    assert isinstance(snap, OrderBookSnapshot)
    assert snap.ts_ms > 0


@pytest.mark.asyncio
async def test_fetch_orderbook_unknown_exchange_raises():
    f = _make_fetcher()
    f._binance = MagicMock()
    with pytest.raises(ValueError, match="Unknown exchange_id"):
        await f.fetch_orderbook("BTC/USDT", exchange_id="kraken")


# ---------------------------------------------------------------------------
# fetch_ticker_price
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_ticker_price_binance():
    storage = _make_storage()
    f = MarketDataFetcher(storage)

    mock_exchange = MagicMock()
    mock_exchange.fetch_ticker = AsyncMock(return_value={"last": 30000.0})
    f._binance = mock_exchange

    price = await f.fetch_ticker_price("BTC/USDT", EXCHANGE_BINANCE)
    assert price == 30000.0


@pytest.mark.asyncio
async def test_fetch_ticker_price_okx():
    storage = _make_storage()
    f = MarketDataFetcher(storage)

    mock_okx = MagicMock()
    mock_okx.fetch_ticker = AsyncMock(return_value={"last": 31000.0})
    f._okx = mock_okx

    price = await f.fetch_ticker_price("BTC/USDT", EXCHANGE_OKX)
    assert price == 31000.0


@pytest.mark.asyncio
async def test_fetch_ticker_price_no_last_raises():
    storage = _make_storage()
    f = MarketDataFetcher(storage)

    mock_exchange = MagicMock()
    mock_exchange.fetch_ticker = AsyncMock(return_value={"last": None})
    f._binance = mock_exchange

    with pytest.raises(ValueError, match="no last price"):
        await f.fetch_ticker_price("BTC/USDT", EXCHANGE_BINANCE)


@pytest.mark.asyncio
async def test_fetch_ticker_price_unknown_exchange():
    f = _make_fetcher()
    f._binance = MagicMock()
    with pytest.raises(ValueError, match="Unknown exchange_id"):
        await f.fetch_ticker_price("BTC/USDT", "kraken")


# ---------------------------------------------------------------------------
# fetch_symbol_precision
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_symbol_precision_found():
    storage = _make_storage()
    f = MarketDataFetcher(storage)

    mock_exchange = MagicMock()
    mock_exchange.markets = {
        "BTC/USDT": {
            "precision": {"amount": 0.001, "price": 0.01},
            "limits": {
                "amount": {"min": 0.001},
                "cost": {"min": 10.0},
            },
        }
    }
    f._binance = mock_exchange

    result = await f.fetch_symbol_precision("BTC/USDT")
    assert result["amount_precision"] == 0.001
    assert result["min_cost"] == 10.0


@pytest.mark.asyncio
async def test_fetch_symbol_precision_not_found_reloads():
    storage = _make_storage()
    f = MarketDataFetcher(storage)

    mock_exchange = MagicMock()
    mock_exchange.markets = {}
    mock_exchange.load_markets = AsyncMock(return_value={})
    f._binance = mock_exchange

    with pytest.raises(ValueError, match="not found"):
        await f.fetch_symbol_precision("ETH/USDT")


@pytest.mark.asyncio
async def test_fetch_symbol_precision_okx():
    storage = _make_storage()
    f = MarketDataFetcher(storage)

    mock_okx = MagicMock()
    mock_okx.markets = {
        "ETH/USDT": {
            "precision": {"amount": 0.01, "price": 0.001},
            "limits": {"amount": {"min": 0.01}, "cost": {"min": 5.0}},
        }
    }
    f._okx = mock_okx

    result = await f.fetch_symbol_precision("ETH/USDT", EXCHANGE_OKX)
    assert result["price_precision"] == 0.001


# ---------------------------------------------------------------------------
# fetch_funding_rate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_funding_rate_binance():
    storage = _make_storage()
    f = MarketDataFetcher(storage)

    mock_exchange = MagicMock()
    mock_exchange.fetch_funding_rate = AsyncMock(return_value={"fundingRate": 0.0001})
    f._binance = mock_exchange

    rate = await f.fetch_funding_rate("BTC/USDT:USDT", EXCHANGE_BINANCE)
    assert rate == 0.0001


@pytest.mark.asyncio
async def test_fetch_funding_rate_not_supported_returns_zero():
    storage = _make_storage()
    f = MarketDataFetcher(storage)

    mock_exchange = MagicMock()
    mock_exchange.fetch_funding_rate = AsyncMock(side_effect=ccxt_async.NotSupported("no perp"))
    f._binance = mock_exchange

    rate = await f.fetch_funding_rate("BTC/USDT", EXCHANGE_BINANCE)
    assert rate == 0.0


@pytest.mark.asyncio
async def test_fetch_funding_rate_bad_symbol_returns_zero():
    storage = _make_storage()
    f = MarketDataFetcher(storage)

    mock_exchange = MagicMock()
    mock_exchange.fetch_funding_rate = AsyncMock(side_effect=ccxt_async.BadSymbol("unknown"))
    f._binance = mock_exchange

    rate = await f.fetch_funding_rate("BTC/USDT", EXCHANGE_BINANCE)
    assert rate == 0.0


@pytest.mark.asyncio
async def test_fetch_funding_rate_no_rate_returns_zero():
    storage = _make_storage()
    f = MarketDataFetcher(storage)

    mock_exchange = MagicMock()
    mock_exchange.fetch_funding_rate = AsyncMock(return_value={"fundingRate": None})
    f._binance = mock_exchange

    rate = await f.fetch_funding_rate("BTC/USDT", EXCHANGE_BINANCE)
    assert rate == 0.0


@pytest.mark.asyncio
async def test_fetch_funding_rate_unknown_exchange_raises():
    f = _make_fetcher()
    f._binance = MagicMock()
    with pytest.raises(ValueError, match="Unknown exchange_id"):
        await f.fetch_funding_rate("BTC/USDT", "kraken")


@pytest.mark.asyncio
async def test_fetch_funding_rate_okx():
    storage = _make_storage()
    f = MarketDataFetcher(storage)

    mock_okx = MagicMock()
    mock_okx.fetch_funding_rate = AsyncMock(return_value={"fundingRate": 0.0002})
    f._okx = mock_okx

    rate = await f.fetch_funding_rate("ETH/USDT", EXCHANGE_OKX)
    assert rate == 0.0002


# ---------------------------------------------------------------------------
# open_fetcher context manager
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_open_fetcher_context_manager():
    storage = _make_storage()
    mock_binance = MagicMock()
    mock_binance.load_markets = AsyncMock(return_value={})
    mock_binance.close = AsyncMock()
    mock_okx = MagicMock()
    mock_okx.load_markets = AsyncMock(return_value={})
    mock_okx.close = AsyncMock()

    with (
        patch("src.data.fetcher._build_binance", return_value=mock_binance),
        patch("src.data.fetcher._build_okx", return_value=mock_okx),
    ):
        async with open_fetcher(storage) as f:
            assert isinstance(f, MarketDataFetcher)
        mock_binance.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_open_fetcher_swallows_close_error_and_does_not_suppress_original():
    """SCAN2-013: a secondary error from close() during __aexit__ must be
    logged, not raised in place of the original exception -- and the
    original exception must still propagate (return False)."""
    storage = _make_storage()
    mock_binance = MagicMock()
    mock_binance.load_markets = AsyncMock(return_value={})
    mock_binance.close = AsyncMock(side_effect=RuntimeError("close failed"))
    mock_okx = MagicMock()
    mock_okx.load_markets = AsyncMock(return_value={})
    mock_okx.close = AsyncMock(side_effect=RuntimeError("close failed"))

    with (
        patch("src.data.fetcher._build_binance", return_value=mock_binance),
        patch("src.data.fetcher._build_okx", return_value=mock_okx),
        pytest.raises(ValueError, match="original error"),
    ):
        async with open_fetcher(storage):
            raise ValueError("original error")
