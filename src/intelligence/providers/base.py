"""
Abstract base for exchange intelligence providers.

Every exchange-specific provider (Binance, OKX, future exchanges) must
implement this protocol so IntelligenceAggregator can treat them uniformly.

Design principles:
  - Protocol/ABC (not dataclass inheritance) so providers can use any
    underlying transport (ccxt, aiohttp, websocket) without constraint.
  - fetch_metrics() always succeeds — providers degrade gracefully and
    signal partial data via 'confidence' in the returned dict.
  - exchange_id is a stable lowercase string matching ccxt convention
    ("binance", "okx") — used as a key in the aggregator's provider map.

Authority: ccxt unified API design (https://docs.ccxt.com/)
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from typing import Any


# A spot/perp basis is only meaningful if both legs were observed at
# roughly the same instant. Two minutes is loose enough for venues that
# stamp tickers lazily and tight enough that a genuinely stalled feed is
# caught before its price is differenced against a live one.
MAX_TICKER_SKEW_MS: int = 120_000


def tickers_are_synchronous(
    spot_ticker: dict[str, Any],
    perp_ticker: dict[str, Any],
    max_skew_ms: int = MAX_TICKER_SKEW_MS,
) -> bool:
    """
    True when two tickers were observed close enough together to difference.

    Basis is a difference between venues, so a stale leg does not produce a
    slightly wrong number -- it produces the price move that happened while
    the feed was down, reported as a dislocation that never existed. The
    ±500bps clamp downstream makes that worse by rendering an absurd value
    plausible.

    A missing timestamp on either side returns True: ccxt does not populate
    it for every venue, and refusing to compute a basis wherever the field
    is absent would disable the signal on those venues entirely. This checks
    what can be checked.
    """
    spot_ts = spot_ticker.get("timestamp")
    perp_ts = perp_ticker.get("timestamp")
    if spot_ts is None or perp_ts is None:
        return True
    try:
        skew = abs(float(spot_ts) - float(perp_ts))
    except (TypeError, ValueError):
        return True
    return skew <= max_skew_ms


class ExchangeIntelligenceProvider(ABC):
    """
    Abstract base class for per-exchange intelligence data providers.

    Subclasses must implement:
      - exchange_id   property  → stable lowercase exchange name ("binance", "okx")
      - initialize()  async     → load markets / warm caches
      - close()       async     → close all underlying connections
      - fetch_metrics() async   → return flat dict matching IntelligenceMetrics fields

    fetch_metrics() contract:
      - NEVER raises.  On any error, log a warning and return neutral values.
      - Always includes keys: "confidence" (float, 0-1) and "timestamp" (int, unix-s).
      - Fields unavailable on this exchange are set to their neutral value:
          0.0 for signed/z-score fields, 0.5 for ratio fields bounded in [0,1].
      - "confidence" must be reduced by _CONFIDENCE_PENALTY for each field
        that returned a neutral default due to an error or missing data source.
    """

    @property
    @abstractmethod
    def exchange_id(self) -> str:
        """Stable lowercase exchange identifier (e.g. 'binance', 'okx')."""

    @abstractmethod
    async def initialize(self) -> None:
        """Load market data and warm any caches. Called once at startup."""

    @abstractmethod
    async def close(self) -> None:
        """Close underlying network sessions. Safe to call multiple times."""

    @abstractmethod
    async def fetch_metrics(self) -> dict[str, float]:
        """
        Return a flat dict of intelligence metric values for this exchange.

        Keys must be a subset of IntelligenceMetrics field names plus:
          "confidence"  float in [0, 1]
          "timestamp"   int   unix seconds

        This method must never raise.
        """

    # ------------------------------------------------------------------
    # Shared TTL cache helpers — subclasses set self._cache and
    # self._cache_ttl in __init__ then call these methods.
    # ------------------------------------------------------------------

    def _get_cache(self, key: str) -> Any:
        """Return cached value or None if missing / expired."""
        entry: tuple[float, Any] | None = getattr(self, "_cache", {}).get(key)
        if entry is None:
            return None
        ts, value = entry
        # Stamped and compared on the monotonic clock. Wall clock made the
        # TTL hostage to NTP: a backward correction keeps stale provider data
        # alive past its window, a forward one expires the whole cache at
        # once and stampedes every provider simultaneously.
        if time.monotonic() - ts > getattr(self, "_cache_ttl", 0.0):
            return None
        return value

    def _set_cache(self, key: str, value: Any) -> None:
        """Store value in the per-instance TTL cache."""
        if not hasattr(self, "_cache"):
            self._cache: dict[str, tuple[float, Any]] = {}
        self._cache[key] = (time.monotonic(), value)
