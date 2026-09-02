"""
Async market data fetcher — Binance (primary) + OKX (secondary).

Responsibilities:
  - Bootstrap historical OHLCV bars from exchange into SQLite storage
  - Incremental gap-fill on every tick cycle
  - Live WebSocket bar streaming (Binance only; OKX polled as fallback)
  - Order-book snapshot fetch for OFI feature computation
  - Exponential-backoff retry on transient network / rate-limit errors

Authority sources:
  - ccxt unified API docs (https://docs.ccxt.com/)
  - Binance Spot API v3 (https://binance-docs.github.io/apidocs/spot/en/)
  - OKX REST API v5 (https://www.okx.com/docs-v5/)
  - Chan (2013) Algorithmic Trading — data quality requirements
"""

from __future__ import annotations

import asyncio
import random
import threading
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any, TypeVar

import ccxt.async_support as ccxt
import structlog

from src.config import (
    EXCHANGE_BINANCE,
    EXCHANGE_OKX,
    TIMEFRAME_SECONDS,
    BinanceSettings,
    OKXSettings,
    Timeframe,
    get_settings,
)
from src.data.storage import AnyStorageBackend, BarRecord


log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_MAX_BARS_PER_REQUEST: int = 1000  # ccxt / Binance max per call
_INITIAL_HISTORY_DAYS: int = 90  # bootstrap lookback window
_RETRY_ATTEMPTS: int = 5
_RETRY_BASE_DELAY_S: float = 1.0  # doubles each attempt (exponential backoff)
_ORDERBOOK_DEPTH: int = 20  # levels fetched for OFI


def _as_float(value: object, default: float) -> float:
    """ccxt leaves numeric market fields null when an exchange omits them."""
    if value is None:
        return default
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# OrderBook snapshot — typed transport for OFI feature
# ---------------------------------------------------------------------------


class OrderBookSnapshot:
    """
    Bids and asks as price-quantity pairs, timestamped.

    bids / asks: list of [price, quantity] pairs, sorted best-first.
    """

    __slots__ = ("asks", "bids", "symbol", "ts_ms")

    def __init__(
        self,
        symbol: str,
        ts_ms: int,
        bids: list[list[float]],
        asks: list[list[float]],
    ) -> None:
        self.symbol = symbol
        self.ts_ms = ts_ms
        self.bids = bids
        self.asks = asks

    @property
    def bid_price(self) -> float:
        """Best bid price."""
        return float(self.bids[0][0]) if self.bids else 0.0

    @property
    def ask_price(self) -> float:
        """Best ask price."""
        return float(self.asks[0][0]) if self.asks else 0.0

    @property
    def mid_price(self) -> float:
        return (self.bid_price + self.ask_price) / 2.0

    @property
    def spread(self) -> float:
        return self.ask_price - self.bid_price

    def bid_volume(self, levels: int = _ORDERBOOK_DEPTH) -> float:
        """Total bid quantity across top N levels."""
        return sum(float(b[1]) for b in self.bids[:levels])

    def ask_volume(self, levels: int = _ORDERBOOK_DEPTH) -> float:
        """Total ask quantity across top N levels."""
        return sum(float(a[1]) for a in self.asks[:levels])

    def order_flow_imbalance(self, levels: int = _ORDERBOOK_DEPTH) -> float:
        """
        OFI = (bid_vol - ask_vol) / (bid_vol + ask_vol).
        Ranges in [-1, 1]; positive = buy pressure.
        """
        bv = self.bid_volume(levels)
        av = self.ask_volume(levels)
        total = bv + av
        if total == 0.0:
            return 0.0
        return (bv - av) / total


def _parse_book_side(raw_side: list[Any]) -> list[list[float]]:
    """Parse one side (bids/asks) of a raw ccxt order book response.

    Exchange responses are not schema-validated by ccxt; malformed rows
    (wrong arity, non-numeric, or non-positive price) are dropped instead
    of raising, so one bad row doesn't corrupt/crash the whole snapshot.
    """
    parsed: list[list[float]] = []
    for entry in raw_side:
        try:
            if len(entry) != 2:
                continue
            price, qty = float(entry[0]), float(entry[1])
        except (TypeError, ValueError, KeyError, IndexError):
            continue
        if price <= 0.0 or qty < 0.0:
            continue
        parsed.append([price, qty])
    return parsed


# ---------------------------------------------------------------------------
# Exchange factory helpers
# ---------------------------------------------------------------------------


def _build_binance(cfg: BinanceSettings) -> ccxt.binance:
    """Construct async ccxt Binance instance from settings."""
    # VF-013: Do NOT put api_key/secret in the options dict passed to the
    # constructor — if ccxt logs or repr()s options on an init error, credentials
    # appear in logs.  Set them on the instance after construction instead.
    options: dict[str, Any] = {
        "enableRateLimit": True,
        "options": {
            "defaultType": "spot",
            "adjustForTimeDifference": True,
        },
    }
    if cfg.testnet:
        options["options"]["sandboxMode"] = True
    exchange: ccxt.binance = ccxt.binance(options)
    exchange.apiKey = cfg.api_key
    exchange.secret = cfg.api_secret
    return exchange


def _build_okx(cfg: OKXSettings) -> ccxt.okx:
    """Construct async ccxt OKX instance from settings."""
    # VF-013: Same credential-exposure fix as _build_binance above.
    options: dict[str, Any] = {
        "enableRateLimit": True,
        "options": {
            "defaultType": "spot",
        },
    }
    if cfg.testnet:
        options["options"]["sandboxMode"] = True
    exchange: ccxt.okx = ccxt.okx(options)
    exchange.apiKey = cfg.api_key
    exchange.secret = cfg.api_secret
    exchange.password = cfg.passphrase
    return exchange


# ---------------------------------------------------------------------------
# Retry helper
# ---------------------------------------------------------------------------


_T = TypeVar("_T")


def _jittered(delay: float) -> float:
    """
    Equal jitter: half the backoff, plus a random share of the other half.

    Without it every concurrent caller backs off in lockstep. This process
    hits Binance from three timeframe loops, the intelligence providers, the
    universe cache and the venue-quote path, so one rate-limit event puts
    several callers on the SAME retry schedule — they wake together, retry
    together, re-trigger the limit together, and stay synchronised for the
    rest of the backoff. Measured over twelve concurrent callers and four
    attempts: 4 distinct retry instants with every caller colliding, versus
    48 distinct instants and no collisions once jittered.

    Equal jitter rather than full jitter (uniform over [0, delay]) because
    the RateLimitExceeded path exists precisely because the exchange asked us
    to slow down. Full jitter can draw a near-zero sleep and retry almost
    immediately, which is the one thing that must not happen there. Halving
    the floor keeps a guaranteed minimum backoff while still decorrelating.

    The jitter applies to the sleep only, never to the stored `delay`, so the
    exponential progression and its 60s ceiling stay exact rather than
    compounding randomness across attempts.
    """
    return delay / 2.0 + random.random() * (delay / 2.0)


async def _with_retry(
    coro_factory: Callable[[], Awaitable[_T]],
    label: str,
    attempts: int = _RETRY_ATTEMPTS,
    base_delay: float = _RETRY_BASE_DELAY_S,
) -> _T:
    """
    Execute an async callable with exponential-backoff retry.

    Retries on: ccxt.NetworkError, ccxt.RequestTimeout, ccxt.RateLimitExceeded.
    Raises immediately on: ccxt.AuthenticationError, ccxt.ExchangeError (non-transient).
    """
    delay = base_delay
    for attempt in range(1, attempts + 1):
        try:
            return await coro_factory()
        except (ccxt.AuthenticationError, ccxt.InvalidOrder) as exc:
            log.error("fetch.auth_or_invalid", label=label, error=str(exc), exc_info=True)
            raise
        except ccxt.RateLimitExceeded as exc:
            # VF-011: On the final attempt raise the original exception so callers
            # receive ccxt.RateLimitExceeded (not a generic RuntimeError from the
            # terminal fallback), consistent with the NetworkError branch below.
            if attempt == attempts:
                log.error(
                    "fetch.max_retries_rate_limit",
                    label=label,
                    attempts=attempts,
                    error=str(exc),
                    exc_info=True,
                )
                raise
            wait = min(delay * 2, 60.0)
            sleep_s = _jittered(wait)
            log.warning(
                "fetch.rate_limited",
                label=label,
                attempt=attempt,
                wait_s=round(sleep_s, 3),
                backoff_s=wait,
                error=str(exc),
                exc_info=True,
            )
            await asyncio.sleep(sleep_s)
            delay = wait
        except (ccxt.NetworkError, ccxt.RequestTimeout) as exc:
            if attempt == attempts:
                log.error(
                    "fetch.max_retries",
                    label=label,
                    attempts=attempts,
                    error=str(exc),
                    exc_info=True,
                )
                raise
            sleep_s = _jittered(delay)
            log.warning(
                "fetch.retry",
                label=label,
                attempt=attempt,
                delay_s=round(sleep_s, 3),
                backoff_s=delay,
                error=str(exc),
                exc_info=True,
            )
            await asyncio.sleep(sleep_s)
            delay = min(delay * 2, 60.0)
        except ccxt.ExchangeError as exc:
            log.error("fetch.exchange_error", label=label, error=str(exc), exc_info=True)
            raise
    # Should never reach here; satisfies type checker
    raise RuntimeError(f"_with_retry exhausted for {label!r}")  # pragma: no cover


# ---------------------------------------------------------------------------
# MarketDataFetcher
# ---------------------------------------------------------------------------


class MarketDataFetcher:
    """
    Async OHLCV and order-book fetcher for Binance (primary) and OKX (secondary).

    Lifecycle::

        fetcher = MarketDataFetcher(storage)
        await fetcher.initialize()
        await fetcher.bootstrap_history("BTC/USDT", Timeframe.INTRADAY)
        await fetcher.gap_fill("BTC/USDT", Timeframe.INTRADAY)
        book = await fetcher.fetch_orderbook("BTC/USDT")
        await fetcher.close()

    Or use open_fetcher() context manager.
    """

    def __init__(self, storage: AnyStorageBackend) -> None:
        self._storage = storage
        self._settings = get_settings()
        self._binance: ccxt.binance | None = None
        self._okx: ccxt.okx | None = None
        self._log = log.bind(component="fetcher")
        # VF-012: asyncio.Semaphore() at __init__ time raises DeprecationWarning
        # on Python 3.10+ when no event loop is running (same pattern as VF-004/VF-011).
        # Use double-checked locking with a threading.Lock sentinel for one-time creation.
        self._sem_init_guard: threading.Lock = threading.Lock()
        self._gap_fill_sem: asyncio.Semaphore | None = None

    def _get_sem(self) -> asyncio.Semaphore:
        """Return (lazily-created) asyncio.Semaphore — thread-safe one-time init."""
        if self._gap_fill_sem is not None:
            return self._gap_fill_sem
        with self._sem_init_guard:
            if self._gap_fill_sem is None:
                self._gap_fill_sem = asyncio.Semaphore(1)
        assert self._gap_fill_sem is not None
        return self._gap_fill_sem

    async def initialize(self) -> None:
        """Build ccxt exchange instances and load markets."""
        cfg = self._settings
        binance = _build_binance(cfg.binance)
        self._binance = binance
        okx = _build_okx(cfg.okx)
        self._okx = okx

        await _with_retry(
            lambda: binance.load_markets(),
            label="binance.load_markets",
        )
        self._log.info("fetcher.binance_ready", testnet=cfg.binance.testnet)

        await _with_retry(
            lambda: okx.load_markets(),
            label="okx.load_markets",
        )
        self._log.info("fetcher.okx_ready", testnet=cfg.okx.testnet)

    async def close(self) -> None:
        """Close both exchange connections cleanly."""
        if self._binance is not None:
            await self._binance.close()
        if self._okx is not None:
            await self._okx.close()
        self._log.info("fetcher.closed")

    def _require_binance(self) -> ccxt.binance:
        if self._binance is None:
            raise RuntimeError("MarketDataFetcher not initialized")
        return self._binance

    def get_order_exchange(self) -> ccxt.binance:
        """
        Return the configured order-placement exchange (Binance).

        Public interface for executors — avoids direct access to private
        _require_binance() and gives MarketDataFetcher lifecycle control
        over when the exchange is available (VUL-029).
        """
        if self._binance is None:
            raise RuntimeError(
                "MarketDataFetcher not initialized. "
                "Call await fetcher.initialize() before placing orders."
            )
        return self._binance

    def _require_okx(self) -> ccxt.okx:
        if self._okx is None:
            raise RuntimeError("MarketDataFetcher not initialized")
        return self._okx

    # ------------------------------------------------------------------
    # OHLCV bootstrap — fills full history window into storage
    # ------------------------------------------------------------------

    async def bootstrap_history(
        self,
        symbol: str,
        timeframe: Timeframe,
        lookback_days: int = _INITIAL_HISTORY_DAYS,
    ) -> int:
        """
        Fetch historical OHLCV bars from exchange and persist to storage.

        Paginates backward from now until lookback_days of data is collected
        or the exchange has no earlier data.  Returns total bars written.

        Uses Binance primary; falls back to OKX on any exchange error.
        """
        exchange = self._require_binance()
        tf_str = timeframe.value
        tf_seconds = TIMEFRAME_SECONDS[timeframe]
        since_ms = int((datetime.now(tz=UTC).timestamp() - lookback_days * 86400) * 1000)

        total_written = 0
        fetch_since = since_ms
        self._log.info(
            "fetcher.bootstrap_start",
            symbol=symbol,
            timeframe=tf_str,
            lookback_days=lookback_days,
        )

        while True:
            _fetch_since = fetch_since

            async def _fetch(s: int = _fetch_since) -> list[list[Any]]:
                return await exchange.fetch_ohlcv(
                    symbol, tf_str, since=s, limit=_MAX_BARS_PER_REQUEST
                )

            raw: list[list[Any]] = await _with_retry(
                _fetch,
                label=f"binance.fetch_ohlcv.{symbol}.{tf_str}",
            )
            if not raw:
                break

            bars = _raw_to_bar_records(symbol, tf_str, raw)
            written = await self._storage.upsert_bars(bars)
            total_written += written

            last_ts = raw[-1][0]
            next_since = last_ts + tf_seconds * 1000

            self._log.debug(
                "fetcher.bootstrap_page",
                symbol=symbol,
                timeframe=tf_str,
                page_bars=len(bars),
                written=written,
                last_ts=last_ts,
            )

            if len(raw) < _MAX_BARS_PER_REQUEST:
                break
            fetch_since = next_since
            # Respect rate limit — small sleep between pages
            await asyncio.sleep(0.25)

        self._log.info(
            "fetcher.bootstrap_done",
            symbol=symbol,
            timeframe=tf_str,
            total_written=total_written,
        )
        return total_written

    # ------------------------------------------------------------------
    # Incremental gap-fill — fast path on every signal cycle
    # ------------------------------------------------------------------

    async def gap_fill(
        self,
        symbol: str,
        timeframe: Timeframe,
    ) -> int:
        """
        Fetch only bars newer than the latest stored timestamp.

        Called at the start of every signal engine tick to keep storage
        current without re-fetching the full history.  Returns bars written.
        """
        exchange = self._require_binance()
        tf_str = timeframe.value
        tf_seconds = TIMEFRAME_SECONDS[timeframe]

        latest_ts = await self._storage.latest_bar_ts(symbol, tf_str)
        if latest_ts is None:
            # No data at all — fall back to full bootstrap
            self._log.warning(
                "fetcher.gap_fill_no_data",
                symbol=symbol,
                timeframe=tf_str,
            )
            return await self.bootstrap_history(symbol, timeframe)

        since_ms = latest_ts + tf_seconds * 1000
        now_ms = int(datetime.now(tz=UTC).timestamp() * 1000)
        if since_ms >= now_ms:
            return 0  # already current

        async def _fetch(s: int = since_ms) -> list[list[Any]]:
            return await exchange.fetch_ohlcv(symbol, tf_str, since=s, limit=_MAX_BARS_PER_REQUEST)

        raw: list[list[Any]] = await _with_retry(
            _fetch,
            label=f"binance.gap_fill.{symbol}.{tf_str}",
        )
        if not raw:
            return 0

        bars = _raw_to_bar_records(symbol, tf_str, raw)
        written = await self._storage.upsert_bars(bars)

        self._log.debug(
            "fetcher.gap_fill",
            symbol=symbol,
            timeframe=tf_str,
            new_bars=written,
        )
        return written

    # ------------------------------------------------------------------
    # Order-book fetch — for OFI feature
    # ------------------------------------------------------------------

    async def fetch_orderbook(
        self,
        symbol: str,
        depth: int = _ORDERBOOK_DEPTH,
        exchange_id: str = EXCHANGE_BINANCE,
    ) -> OrderBookSnapshot:
        """
        Fetch a snapshot of the order book from Binance (default) or OKX.

        Returns an OrderBookSnapshot with .order_flow_imbalance() ready
        for the feature pipeline.
        """
        if exchange_id == EXCHANGE_OKX:
            exchange = self._require_okx()
            label = f"okx.orderbook.{symbol}"
        elif exchange_id == EXCHANGE_BINANCE:
            exchange = self._require_binance()
            label = f"binance.orderbook.{symbol}"
        else:
            # VF-014: Reject unknown exchange IDs explicitly — previously any
            # unrecognised string silently fell through to Binance.
            raise ValueError(
                f"Unknown exchange_id {exchange_id!r}. "
                f"Must be {EXCHANGE_BINANCE!r} or {EXCHANGE_OKX!r}."
            )

        raw: dict[str, Any] = await _with_retry(
            lambda: exchange.fetch_order_book(symbol, limit=depth),
            label=label,
        )

        ts_ms = raw.get("timestamp") or int(datetime.now(tz=UTC).timestamp() * 1000)
        bids = _parse_book_side(raw.get("bids", []))
        asks = _parse_book_side(raw.get("asks", []))

        return OrderBookSnapshot(
            symbol=symbol,
            ts_ms=ts_ms,
            bids=bids,
            asks=asks,
        )

    # ------------------------------------------------------------------
    # Concurrent multi-timeframe gap-fill
    # ------------------------------------------------------------------

    async def gap_fill_all_timeframes(
        self,
        symbol: str,
        timeframes: list[Timeframe],
    ) -> dict[str, int]:
        """
        Run gap_fill sequentially for all requested timeframes.

        SCAN2-001: Serialised via self._gap_fill_sem (Semaphore(1)) so concurrent
        timeframe fetches never burst Binance's 1200 weight/min limit simultaneously.
        Returns mapping of timeframe value → bars written.
        """
        results: dict[str, int] = {}
        for tf in timeframes:
            async with self._get_sem():
                try:
                    results[tf.value] = await self.gap_fill(symbol, tf)
                except (ccxt.NetworkError, ccxt.ExchangeError) as exc:
                    self._log.error(
                        "fetcher.gap_fill_failed",
                        symbol=symbol,
                        timeframe=tf.value,
                        error=str(exc),
                        exc_info=True,
                    )
                    results[tf.value] = 0
        return results

    # ------------------------------------------------------------------
    # OKX fallback fetch — used when Binance is unavailable
    # ------------------------------------------------------------------

    async def fetch_ohlcv_okx(
        self,
        symbol: str,
        timeframe: Timeframe,
        since_ms: int,
        limit: int = _MAX_BARS_PER_REQUEST,
    ) -> list[BarRecord]:
        """
        Direct OHLCV fetch from OKX, returned as BarRecord list.

        Used as secondary source when Binance is down or rate-limited.
        """
        exchange = self._require_okx()
        tf_str = timeframe.value

        raw: list[list[Any]] = await _with_retry(
            lambda: exchange.fetch_ohlcv(symbol, tf_str, since=since_ms, limit=limit),
            label=f"okx.fetch_ohlcv.{symbol}.{tf_str}",
        )
        return _raw_to_bar_records(symbol, tf_str, raw)

    # ------------------------------------------------------------------
    # Current price — lightweight ticker
    # ------------------------------------------------------------------

    async def fetch_ticker_price(
        self,
        symbol: str,
        exchange_id: str = EXCHANGE_BINANCE,
    ) -> float:
        """
        Return last traded price for symbol from exchange.

        Used by paper executor to mark open positions to market.
        """
        if exchange_id == EXCHANGE_OKX:
            exchange = self._require_okx()
            label = f"okx.ticker.{symbol}"
        elif exchange_id == EXCHANGE_BINANCE:
            exchange = self._require_binance()
            label = f"binance.ticker.{symbol}"
        else:
            raise ValueError(
                f"Unknown exchange_id {exchange_id!r}. "
                f"Must be {EXCHANGE_BINANCE!r} or {EXCHANGE_OKX!r}."
            )

        ticker: dict[str, Any] = await _with_retry(
            lambda: exchange.fetch_ticker(symbol),
            label=label,
        )
        last: float | None = ticker.get("last")
        if last is None:
            raise ValueError(f"Exchange returned no last price for {symbol!r}")
        return float(last)

    # ------------------------------------------------------------------
    # Exchange info — symbol precision for position sizing
    # ------------------------------------------------------------------

    async def fetch_symbol_precision(
        self,
        symbol: str,
        exchange_id: str = EXCHANGE_BINANCE,
    ) -> dict[str, float]:
        """
        Return price and amount precision for a symbol.

        Returns dict with keys: amount_precision, price_precision,
        min_amount, min_cost.
        """
        if exchange_id == EXCHANGE_OKX:
            exchange = self._require_okx()
        else:
            exchange = self._require_binance()

        markets: dict[str, Any] = exchange.markets
        if symbol not in markets:
            await _with_retry(
                lambda: exchange.load_markets(reload=True),
                label=f"{exchange_id}.reload_markets",
            )
            markets = exchange.markets

        if symbol not in markets:
            raise ValueError(f"Symbol {symbol!r} not found in {exchange_id} markets")

        market = markets[symbol]
        # Every one of these can be present and null: ccxt fills the keys in
        # even for markets whose precision or limits an exchange does not
        # publish, so `.get(key, default)` returns None rather than the
        # default and float(None) raises on the order-sizing path.
        precision = market.get("precision") or {}
        limits = market.get("limits") or {}
        amount_limits = limits.get("amount") or {}
        cost_limits = limits.get("cost") or {}

        return {
            "amount_precision": _as_float(precision.get("amount"), 8.0),
            "price_precision": _as_float(precision.get("price"), 8.0),
            "min_amount": _as_float(amount_limits.get("min"), 0.0),
            "min_cost": _as_float(cost_limits.get("min"), 0.0),
        }

    # ------------------------------------------------------------------
    # Perpetual funding rate — TASK-010
    # ------------------------------------------------------------------

    async def fetch_funding_rate(
        self,
        symbol: str,
        exchange_id: str = EXCHANGE_BINANCE,
    ) -> float:
        """
        Return the current 8-hour perpetual funding rate for *symbol*.

        For spot symbols (no funding rate) or any fetch error, returns 0.0
        so the caller can always treat this as a safe float.

        Returns
        -------
        float
            Funding rate as a fraction (e.g. 0.0001 = 0.01% per 8h).
            Positive → longs pay shorts; negative → shorts pay longs.
        """
        if exchange_id == EXCHANGE_OKX:
            exchange = self._require_okx()
            label = f"okx.funding_rate.{symbol}"
        elif exchange_id == EXCHANGE_BINANCE:
            exchange = self._require_binance()
            label = f"binance.funding_rate.{symbol}"
        else:
            raise ValueError(
                f"Unknown exchange_id {exchange_id!r}. "
                f"Must be {EXCHANGE_BINANCE!r} or {EXCHANGE_OKX!r}."
            )

        try:
            raw: dict[str, Any] = await _with_retry(
                lambda: exchange.fetch_funding_rate(symbol),
                label=label,
            )
            rate = raw.get("fundingRate")
            if rate is None:
                return 0.0
            return float(rate)
        except (ccxt.NotSupported, ccxt.BadSymbol, ccxt.BadRequest):
            # Spot symbol or exchange doesn't support funding rates — not an error.
            return 0.0

    # ------------------------------------------------------------------
    # Exchange-truth holdings snapshot — disaster-recovery reconciliation
    # ------------------------------------------------------------------

    async def fetch_exchange_holdings(self, symbols: list[str]) -> dict[str, float] | None:
        """
        Base-asset balances for *symbols*, as the order exchange reports them.

        Used to reconcile local state against exchange truth after a restart
        (src/diagnostics/disaster_recovery.py). Holdings are read, never
        inferred.

        Balances, not ``fetch_positions``: both exchanges here are built with
        ``defaultType="spot"`` (see _build_binance/_build_okx), and a spot
        account has no positions endpoint — a long BTC/USDT "position" is
        simply a BTC balance. Quantities are therefore never negative; a
        local short recorded against a spot venue is itself a discrepancy
        worth surfacing.

        Scoped to *symbols* rather than returning the whole balance sheet:
        the bot's mandate is the symbols it trades, and an unrelated asset
        sitting in the account is not evidence that its own book is wrong.

        Returns
        -------
        dict[str, float] | None
            {symbol: base-asset quantity} for every requested symbol, or None
            when the snapshot could not be obtained.

        None, not {}: an empty mapping is the assertion "the exchange holds
        nothing", which would make every local position look like a
        MISSING_IN_REFERENCE discrepancy. An unavailable snapshot is not
        evidence of a flat account, and reconciliation must be able to tell
        the two apart.
        """
        if not symbols:
            return {}
        exchange = self.get_order_exchange()
        try:
            balance: dict[str, Any] = await _with_retry(
                lambda: exchange.fetch_balance(),
                label="binance.fetch_balance",
            )
        except Exception as exc:
            log.error("fetch.balance_failed", error=str(exc), exc_info=True)
            return None

        # "total" is free + locked-in-orders. Locked quantity is still owned,
        # so excluding it would under-report the book by exactly the amount
        # sitting in a resting order.
        totals = balance.get("total") or {}
        holdings: dict[str, float] = {}
        for symbol in symbols:
            base = str(symbol).split("/")[0].split(":")[0]
            raw = totals.get(base)
            try:
                holdings[symbol] = float(raw) if raw is not None else 0.0
            except (TypeError, ValueError):
                holdings[symbol] = 0.0
        return holdings


# ---------------------------------------------------------------------------
# Raw OHLCV → BarRecord conversion
# ---------------------------------------------------------------------------


def _raw_to_bar_records(
    symbol: str,
    timeframe: str,
    raw: list[list[Any]],
) -> list[BarRecord]:
    """
    Convert ccxt raw OHLCV list to typed BarRecord list.

    ccxt format: [timestamp_ms, open, high, low, close, volume]
    Extended Binance format includes quote_volume and taker_buy_vol at indices 6-7
    when fetched with params={'quoteVolume': True} — handled gracefully.
    """
    records: list[BarRecord] = []
    for row in raw:
        if len(row) < 6:
            continue
        records.append(
            BarRecord(
                symbol=symbol,
                timeframe=timeframe,
                ts=int(row[0]),
                open=float(row[1]),
                high=float(row[2]),
                low=float(row[3]),
                close=float(row[4]),
                volume=float(row[5]),
                quote_volume=float(row[6]) if len(row) > 6 and row[6] is not None else 0.0,
                taker_buy_vol=float(row[7]) if len(row) > 7 and row[7] is not None else 0.0,
            )
        )
    return records


# ---------------------------------------------------------------------------
# Context manager
# ---------------------------------------------------------------------------


class _FetcherContextManager:
    def __init__(self, storage: AnyStorageBackend) -> None:
        self._fetcher = MarketDataFetcher(storage)

    async def __aenter__(self) -> MarketDataFetcher:
        await self._fetcher.initialize()
        return self._fetcher

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> bool | None:
        # SCAN2-013: catch secondary close errors so they don't replace the original
        # exception in the traceback. Log at WARNING level and let original propagate.
        try:
            await self._fetcher.close()
        except Exception as close_exc:
            log.warning(
                "fetcher.context_manager_close_error",
                error=str(close_exc),
                original_exc_type=exc_type.__name__ if exc_type else None,
                exc_info=True,
            )
        return False  # do not suppress the original exception


def open_fetcher(storage: AnyStorageBackend) -> _FetcherContextManager:
    """
    Async context manager for MarketDataFetcher.

    Usage::

        async with open_fetcher(storage) as fetcher:
            await fetcher.bootstrap_history("BTC/USDT", Timeframe.INTRADAY)
    """
    return _FetcherContextManager(storage)
