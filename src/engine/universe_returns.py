"""
Universe trailing returns — the feed the cross-sectional family never had.

``CrossSectionalMomentumStrategy`` ranks the traded universe by trailing
return and takes the top/bottom decile. It has been registered and inert
since it was written, and unlike the other silent families the cause was not
a missing plumbing line: this process genuinely had no universe. ``Settings``
carries a single ``primary_symbol``, storage bootstraps history for that one
symbol, and every feed in the system is symbol-scoped to it. A strategy that
needs *cross-sectional* data cannot be fed by a single-asset pipeline, so it
answered ``Signal(0, 0, 0)`` by way of its own ``_MIN_UNIVERSE_SIZE`` guard —
indistinguishable, before the portfolio runner existed, from a strategy that
had looked at a universe and found nothing.

This module supplies the missing dimension without disturbing the
single-symbol trading pipeline:

* **Read-only and out-of-band.** Universe bars are fetched straight from the
  exchange and never written to storage. The bars table is the trading
  record for one symbol; filling it with assets the bot does not trade would
  make every ``bar_count``/coverage/gap-fill check answer about a different
  population than the one being traded.

* **Cached on a slow cadence.** A 30-day trailing return does not move
  meaningfully between two 15-minute ticks. Refetching a whole universe per
  tick would multiply this bot's exchange request rate by the universe size
  for information that changes daily — and rate limits are a first-class
  constraint here, not an afterthought. The TTL is what makes the feature
  affordable at all.

* **Degrading per symbol, not per universe.** One delisted or rate-limited
  symbol drops out of the cross-section; it does not blank the ranking. The
  strategy's own minimum-universe guard then decides whether what survived
  is still a cross-section worth ranking.

* **Serving stale data in preference to none.** If a refresh fails outright,
  the previous snapshot is kept and marked stale rather than discarded. A
  momentum ranking an hour old is a far better input than an empty universe,
  which would silently return the family to the inert state this module
  exists to end.

Quotes come from OKX rather than Binance deliberately: Binance is the order
venue, and its rate-limit budget belongs to the trading path. A universe
refresh is background enrichment and must never be the reason an order
request gets throttled.
"""

from __future__ import annotations

import asyncio
import time
from typing import Protocol

import structlog

from src.config import Timeframe

log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)


# Universe bars come from the 4h stream: it is the longest timeframe the bot
# already speaks, so a 30-day lookback is ~180 bars — one request per symbol
# rather than the thousands a 15m lookback would need.
_UNIVERSE_TIMEFRAME: Timeframe = Timeframe.SWING
_UNIVERSE_TF_SECONDS: int = 4 * 3600

# Concurrent symbol fetches. Bounded well below the universe size: the point
# of refreshing off the tick path is to stay gentle on the exchange, and
# firing the whole universe at once would undo that in one burst.
_MAX_CONCURRENT_FETCHES: int = 4

# A trailing return needs both ends of the window. Two bars is the bare
# minimum; below it the symbol has no return, not a zero return.
_MIN_BARS_FOR_RETURN: int = 2

# Backoff after a refresh that resolved nothing. Without it a failing
# refresh never advances the success timestamp, so the TTL never suppresses
# the next attempt and every tick re-fires the whole universe — the heaviest
# possible request pattern, aimed at an exchange that has just demonstrated
# it is unhealthy. Doubles per consecutive failure up to the ceiling.
_FAILURE_BACKOFF_START_S: float = 30.0
_FAILURE_BACKOFF_MAX_S: float = 900.0


class _BarLike(Protocol):
    ts: int
    close: float


class _FetcherLike(Protocol):
    async def fetch_ohlcv_okx(
        self,
        symbol: str,
        timeframe: Timeframe,
        since_ms: int,
        limit: int = ...,
    ) -> list[_BarLike]: ...


class UniverseReturnsCache:
    """
    TTL-cached trailing returns across a symbol universe.

    Not thread-safe by design — it is driven from the orchestrator's event
    loop. Concurrent callers are serialised on an asyncio lock so a slow
    refresh cannot be started twice, which on a 20-symbol universe would
    double the request burst it exists to avoid.
    """

    def __init__(
        self,
        fetcher: _FetcherLike,
        symbols: tuple[str, ...],
        *,
        lookback_days: int = 30,
        ttl_seconds: float = 3600.0,
        max_concurrency: int = _MAX_CONCURRENT_FETCHES,
    ) -> None:
        if lookback_days <= 0:
            raise ValueError(f"lookback_days must be positive, got {lookback_days}")
        if ttl_seconds <= 0.0:
            raise ValueError(f"ttl_seconds must be positive, got {ttl_seconds}")
        if max_concurrency <= 0:
            raise ValueError(f"max_concurrency must be positive, got {max_concurrency}")
        self._fetcher = fetcher
        self._symbols = symbols
        self._lookback_days = lookback_days
        self._ttl = ttl_seconds
        self._sem = asyncio.Semaphore(max_concurrency)
        self._lock = asyncio.Lock()
        self._returns: dict[str, float] = {}
        self._closes: dict[str, tuple[float, ...]] = {}
        # None means "never", not 0.0. Under wall clock a 0.0 sentinel worked
        # by accident: now - 0.0 is astronomically large, so an unfetched
        # cache always read as stale. time.monotonic() has an arbitrary
        # epoch that can be small, so the same sentinel would make a
        # never-fetched cache look FRESH and never populate at all. The
        # sentinel has to be explicit rather than rely on the clock's origin.
        self._fetched_at: float | None = None
        # Attempt bookkeeping, kept separate from _fetched_at: the TTL is
        # about how old the DATA is, the backoff is about how recently we
        # last tried. Conflating them is what produced the retry storm.
        self._last_attempt_at: float | None = None
        self._consecutive_failures: int = 0

    @property
    def symbols(self) -> tuple[str, ...]:
        return self._symbols

    @property
    def fetched_at(self) -> float:
        """
        Monotonic timestamp of the last successful refresh; 0.0 before any.

        Not an epoch time and not comparable to one — it is read as an age by
        is_stale() and as a memoisation key by the orchestrator's pair
        cointegration cache. Wall clock would put both at the mercy of an NTP
        correction: a backward step makes a stale snapshot look fresh, and a
        forward one expires the universe and reopens the retry path.
        """
        return 0.0 if self._fetched_at is None else self._fetched_at

    def is_stale(self, now: float | None = None) -> bool:
        """True when the DATA is older than the TTL. Says nothing about
        whether a refresh should be attempted — see _may_attempt."""
        if self._fetched_at is None:
            return True
        clock = time.monotonic() if now is None else now
        return (clock - self._fetched_at) > self._ttl

    def _backoff_seconds(self) -> float:
        """Seconds to wait before retrying after consecutive failures."""
        if self._consecutive_failures <= 0:
            return 0.0
        return min(
            _FAILURE_BACKOFF_START_S * (2 ** (self._consecutive_failures - 1)),
            _FAILURE_BACKOFF_MAX_S,
        )

    def _may_attempt(self, now: float | None = None) -> bool:
        """
        Whether a refresh is allowed to run right now.

        Separate from is_stale() on purpose: stale data is a reason to WANT a
        refresh, not permission to fire one. After a failure the data stays
        stale by definition — nothing replaced it — so a check on staleness
        alone re-fires the entire universe on every tick, hardest exactly
        when the exchange is least able to serve it.
        """
        if self._consecutive_failures == 0 or self._last_attempt_at is None:
            return True
        clock = time.monotonic() if now is None else now
        return (clock - self._last_attempt_at) >= self._backoff_seconds()

    def snapshot(self) -> dict[str, float]:
        """Last known trailing returns. Empty before the first refresh."""
        return dict(self._returns)

    def close_series(self, symbol: str) -> tuple[float, ...] | None:
        """
        The close series the last refresh fetched for *symbol*, oldest first.

        Retained because the pairs family needs the series itself, not the
        summary statistic: a spread z-score cannot be reconstructed from two
        trailing returns. Serving it from the same snapshot means the pair
        and the cross-section are computed from identical bars — pricing
        them from separate fetches would let the two families disagree about
        what the market did.
        """
        return self._closes.get(symbol)

    async def _trailing_return(self, symbol: str) -> tuple[str, float, tuple[float, ...]] | None:
        """
        One symbol's lookback return, or None when it cannot be computed.

        None rather than 0.0: a symbol with no data has no return, and a zero
        would rank it mid-universe — right where a percentile-based decile
        strategy is least likely to notice something is wrong.
        """
        bars_needed = int(self._lookback_days * 86_400 / _UNIVERSE_TF_SECONDS) + 1
        # Deliberately wall clock: this is an exchange API parameter and must
        # be a real epoch time. Everything in this class that measures a
        # DURATION uses time.monotonic(); this one measures a point in
        # history, which is the distinction that decides between them.
        since_ms = int((time.time() - self._lookback_days * 86_400) * 1000)
        try:
            async with self._sem:
                bars = await self._fetcher.fetch_ohlcv_okx(
                    symbol,
                    _UNIVERSE_TIMEFRAME,
                    since_ms,
                    bars_needed,
                )
        except Exception as exc:
            log.warning("universe.symbol_fetch_failed", symbol=symbol, error=str(exc))
            return None

        if len(bars) < _MIN_BARS_FOR_RETURN:
            log.debug("universe.symbol_insufficient_bars", symbol=symbol, n_bars=len(bars))
            return None

        ordered = sorted(bars, key=lambda b: b.ts)
        closes = tuple(float(b.close) for b in ordered)
        first, last = closes[0], closes[-1]
        if first <= 0.0:
            # A non-positive close is corrupt data, not a 100% loss.
            log.warning("universe.symbol_bad_close", symbol=symbol, first_close=first)
            return None
        return symbol, (last - first) / first, closes

    async def refresh(self) -> dict[str, float]:
        """
        Refetch the universe. Returns the new snapshot, or keeps the old one.

        A refresh that resolves no symbols at all leaves the previous
        snapshot in place: an exchange-wide outage should cost freshness,
        not the entire cross-section.
        """
        self._last_attempt_at = time.monotonic()
        results = await asyncio.gather(
            *(self._trailing_return(s) for s in self._symbols),
            return_exceptions=True,
        )
        resolved: dict[str, float] = {}
        closes: dict[str, tuple[float, ...]] = {}
        for res in results:
            if isinstance(res, BaseException) or res is None:
                continue
            symbol, value, series = res
            resolved[symbol] = value
            closes[symbol] = series

        if not resolved:
            self._consecutive_failures += 1
            log.warning(
                "universe.refresh_empty",
                universe_size=len(self._symbols),
                retained=len(self._returns),
                consecutive_failures=self._consecutive_failures,
                retry_after_s=round(self._backoff_seconds(), 1),
            )
            return dict(self._returns)

        self._consecutive_failures = 0
        self._returns = resolved
        self._closes = closes
        self._fetched_at = time.monotonic()
        log.info(
            "universe.refreshed",
            resolved=len(resolved),
            universe_size=len(self._symbols),
            dropped=len(self._symbols) - len(resolved),
        )
        return dict(resolved)

    async def trailing_returns(self) -> dict[str, float]:
        """
        Current trailing returns, refreshing only when the TTL has expired.

        The lock makes a concurrent second caller wait for the in-flight
        refresh rather than starting its own; on re-entry the TTL check has
        already been satisfied by the first, so it returns immediately.
        """
        if not self._symbols:
            return {}
        if not self.is_stale():
            return dict(self._returns)
        if not self._may_attempt():
            # In backoff: serve what we have. A momentum ranking an hour old
            # beats both an empty universe and a request storm.
            return dict(self._returns)
        async with self._lock:
            if not self.is_stale() or not self._may_attempt():
                return dict(self._returns)
            return await self.refresh()


__all__ = ["UniverseReturnsCache"]
