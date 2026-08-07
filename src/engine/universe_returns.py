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
        self._fetched_at: float = 0.0

    @property
    def symbols(self) -> tuple[str, ...]:
        return self._symbols

    @property
    def fetched_at(self) -> float:
        """Epoch seconds of the last successful refresh; 0.0 before any."""
        return self._fetched_at

    def is_stale(self, now: float | None = None) -> bool:
        clock = time.time() if now is None else now
        return (clock - self._fetched_at) > self._ttl

    def snapshot(self) -> dict[str, float]:
        """Last known trailing returns. Empty before the first refresh."""
        return dict(self._returns)

    async def _trailing_return(self, symbol: str) -> tuple[str, float] | None:
        """
        One symbol's lookback return, or None when it cannot be computed.

        None rather than 0.0: a symbol with no data has no return, and a zero
        would rank it mid-universe — right where a percentile-based decile
        strategy is least likely to notice something is wrong.
        """
        bars_needed = int(self._lookback_days * 86_400 / _UNIVERSE_TF_SECONDS) + 1
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
        first = float(ordered[0].close)
        last = float(ordered[-1].close)
        if first <= 0.0:
            # A non-positive close is corrupt data, not a 100% loss.
            log.warning("universe.symbol_bad_close", symbol=symbol, first_close=first)
            return None
        return symbol, (last - first) / first

    async def refresh(self) -> dict[str, float]:
        """
        Refetch the universe. Returns the new snapshot, or keeps the old one.

        A refresh that resolves no symbols at all leaves the previous
        snapshot in place: an exchange-wide outage should cost freshness,
        not the entire cross-section.
        """
        results = await asyncio.gather(
            *(self._trailing_return(s) for s in self._symbols),
            return_exceptions=True,
        )
        resolved: dict[str, float] = {}
        for res in results:
            if isinstance(res, BaseException) or res is None:
                continue
            symbol, value = res
            resolved[symbol] = value

        if not resolved:
            log.warning(
                "universe.refresh_empty",
                universe_size=len(self._symbols),
                retained=len(self._returns),
            )
            return dict(self._returns)

        self._returns = resolved
        self._fetched_at = time.time()
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
        async with self._lock:
            if not self.is_stale():
                return dict(self._returns)
            return await self.refresh()


__all__ = ["UniverseReturnsCache"]
