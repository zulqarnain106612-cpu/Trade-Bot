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

from abc import ABC, abstractmethod


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
      - Always includes keys: "confidence" (float, 0–1) and "timestamp" (int, unix-s).
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
