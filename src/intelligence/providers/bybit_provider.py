"""
Bybit public REST provider for intelligence metrics.

All endpoints used here are unauthenticated Bybit public API (via ccxt's
unified interface) — zero cost, no API key required.

DATA COVERAGE (free via Bybit public REST):
  ✅ bybit_funding_rate_pct          — fetchFundingRateHistory (latest)
  ✅ futures_oi_change_pct           — fetchOpenInterestHistory
  ✅ cross_exchange_basis_spread_bps — (perp_last - spot_last) / spot_last * 10_000
  ✅ whale_buy_sell_ratio            — taker buy/sell volume from fetchTrades
  ✅ liquidation_pressure_24h_zscore — funding rate z-score proxy
  ✅ liquidation_cascade_risk_usd    — OI_value * stress_score (estimate)
  ✅ exchange_stress_score           — composite: basis + funding_zscore + OI_chg

DATA NOT AVAILABLE FREE (set to neutral, confidence reduced):
  ❌ exchange_netflow_7d_zscore   → 0.0 (on-chain, no free Bybit source)
  ❌ exchange_reserve_ratio       → 0.5 (on-chain, no free Bybit source)
  ❌ miner_netflow_signal         → 0.0 (on-chain only)
  ❌ staking_unlock_risk          → 0.0 (no free source)
  ❌ entity_exchange_imbalance    → 0.0 (on-chain only)
  ❌ btc_dominance_regime         → 0.0 (cross-market; aggregated by CoinGecko provider)
  ❌ stablecoin_reserve_ratio     → 0.5 (cross-market; aggregated by CoinGecko provider)
  ❌ network_activity_score       → 0.0 (on-chain; blockchain provider)

Mirrors OKXIntelligenceProvider's structure exactly (same base contract,
same neutral-degradation policy) so IntelligenceAggregator treats every
exchange provider uniformly. The whale/taker ratio uses ccxt's unified
fetch_trades() (side-tagged trade volume) rather than an exchange-specific
raw endpoint, since Bybit's implicit API surface for long/short-account
ratio is not part of ccxt's unified contract and guessing at undocumented
raw method names would risk silently wrong data instead of graceful
degradation.

Authority:
  Bybit V5 API docs: https://bybit-exchange.github.io/docs/v5/intro
  ccxt unified API design: https://docs.ccxt.com/
  López de Prado (2018) AFML Ch.4 — taker-flow imbalance as microstructure feature
"""

from __future__ import annotations

import asyncio
import statistics
import time
from typing import Any, Final

import ccxt.async_support as ccxt
import structlog

from src.intelligence.providers.base import ExchangeIntelligenceProvider


log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

# Funding history window: 90 x 8h periods = 30 days
_FR_HISTORY_PERIODS: Final[int] = 90
# OI hourly bars for 24h delta
_OI_HISTORY_HOURS: Final[int] = 25
# Recent-trades window for taker-flow ratio
_TRADES_LIMIT: Final[int] = 200
# Stress score weights (must sum to 1.0)
_W_BASIS: Final[float] = 0.35
_W_FR_Z: Final[float] = 0.40
_W_OI: Final[float] = 0.25
# Confidence handling
_CONFIDENCE_PENALTY: Final[float] = 0.05
_MISSING_PAID_FIELDS: Final[int] = 8


class BybitIntelligenceProvider(ExchangeIntelligenceProvider):
    """
    Fetches IntelligenceMetrics-compatible data from Bybit public APIs.

    Mirrors BinanceIntelligenceProvider / OKXIntelligenceProvider interfaces
    exactly so IntelligenceAggregator can treat all three uniformly.

    Bybit uses ccxt unified symbols: "BTC/USDT" (spot), "BTC/USDT:USDT" (linear perp).

    Usage::

        provider = BybitIntelligenceProvider(symbol="BTC/USDT")
        await provider.initialize()
        metrics = await provider.fetch_metrics()
        await provider.close()
    """

    def __init__(
        self,
        symbol: str = "BTC/USDT",
        perp_symbol: str = "BTC/USDT:USDT",
        cache_ttl_s: int = 300,
    ) -> None:
        self._symbol = symbol
        self._perp_symbol = perp_symbol
        self._cache_ttl = cache_ttl_s

        self._spot = ccxt.bybit({"options": {"defaultType": "spot"}})
        self._perp = ccxt.bybit({"options": {"defaultType": "linear"}})

        self._cache: dict[str, tuple[float, Any]] = {}
        self._log = log.bind(
            component="bybit_intelligence",
            symbol=symbol,
            perp_symbol=perp_symbol,
        )

    @property
    def exchange_id(self) -> str:
        return "bybit"

    async def initialize(self) -> None:
        """Load ccxt market definitions (required before API calls)."""
        await asyncio.gather(
            self._spot.load_markets(),
            self._perp.load_markets(),
        )
        self._log.info("bybit_intelligence.initialized")

    async def close(self) -> None:
        """
        Close underlying ccxt sessions.

        return_exceptions so one failing close cannot cancel the other and
        leak its aiohttp session for the life of the process.
        """
        await asyncio.gather(
            self._spot.close(),
            self._perp.close(),
            return_exceptions=True,
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def fetch_metrics(self) -> dict[str, float]:
        """
        Fetch all available free intelligence metrics from Bybit.

        Returns a dict with every IntelligenceMetrics field populated.
        Never raises — on any error the field degrades to its neutral default.
        """
        ts = int(time.time())
        confidence = 1.0 - (_MISSING_PAID_FIELDS * _CONFIDENCE_PENALTY)

        (
            funding_result,
            oi_result,
            basis_result,
            whale_result,
        ) = await asyncio.gather(
            self._fetch_funding_data(),
            self._fetch_oi_data(),
            self._fetch_basis_data(),
            self._fetch_whale_taker_ratio(),
            return_exceptions=True,
        )

        funding_rate_pct: float = 0.0
        funding_zscore: float = 0.0
        if isinstance(funding_result, dict):
            funding_rate_pct = funding_result.get("rate_pct", 0.0)
            funding_zscore = funding_result.get("zscore", 0.0)
        else:
            self._log.warning("bybit_intelligence.funding_failed", error=str(funding_result))
            confidence -= 0.05

        oi_change_pct: float = 0.0
        oi_value_usd: float = 0.0
        if isinstance(oi_result, dict):
            oi_change_pct = oi_result.get("change_pct", 0.0)
            oi_value_usd = oi_result.get("value_usd", 0.0)
        else:
            self._log.warning("bybit_intelligence.oi_failed", error=str(oi_result))
            confidence -= 0.05

        basis_bps: float = 0.0
        if isinstance(basis_result, float):
            basis_bps = basis_result
        else:
            self._log.warning("bybit_intelligence.basis_failed", error=str(basis_result))
            confidence -= 0.05

        whale_ratio: float = 1.0
        if isinstance(whale_result, float):
            whale_ratio = whale_result
        else:
            self._log.warning("bybit_intelligence.whale_failed", error=str(whale_result))
            confidence -= 0.02

        stress_score = self._compute_stress_score(basis_bps, funding_zscore, oi_change_pct)
        liquidation_cascade_usd = oi_value_usd * stress_score
        liquidation_pressure_zscore = funding_zscore

        confidence = max(0.0, min(1.0, confidence))

        return {
            # FREE: computed from Bybit public API
            "binance_funding_rate_pct": funding_rate_pct,  # same field; cross-exchange average done by aggregator
            "futures_oi_change_pct": oi_change_pct,
            "cross_exchange_basis_spread_bps": basis_bps,
            "whale_buy_sell_ratio": whale_ratio,
            "liquidation_pressure_24h_zscore": liquidation_pressure_zscore,
            "liquidation_cascade_risk_usd": liquidation_cascade_usd,
            "exchange_stress_score": stress_score,
            # PAID-SOURCE ONLY: neutral defaults
            "exchange_netflow_7d_zscore": 0.0,
            "exchange_reserve_ratio": 0.5,
            "miner_netflow_signal": 0.0,
            "staking_unlock_risk": 0.0,
            "entity_exchange_imbalance": 0.0,
            "btc_dominance_regime": 0.0,
            "stablecoin_reserve_ratio": 0.5,
            "network_activity_score": 0.0,
            # on-chain fields — neutral defaults for exchange provider
            "defi_tvl_7d_change_pct": 0.0,
            "mvrv_z_score": 0.0,
            "sopr": 0.0,
            # Metadata
            "confidence": confidence,
            "timestamp": ts,
        }

    # ------------------------------------------------------------------
    # Private fetch helpers
    # ------------------------------------------------------------------

    async def _fetch_funding_data(self) -> dict[str, float]:
        cache_key = f"funding:{self._perp_symbol}"
        cached = self._get_cache(cache_key)
        if cached is not None:
            return cached

        history = await self._perp.fetch_funding_rate_history(
            self._perp_symbol, limit=_FR_HISTORY_PERIODS
        )
        if not history:
            return {"rate_pct": 0.0, "zscore": 0.0}

        rates = [r["fundingRate"] for r in history if r.get("fundingRate") is not None]
        if len(rates) < 2:
            return {"rate_pct": rates[-1] if rates else 0.0, "zscore": 0.0}

        current = rates[-1]
        mu = statistics.mean(rates)
        sigma = statistics.stdev(rates)
        zscore = (current - mu) / sigma if sigma > 1e-12 else 0.0

        result = {
            "rate_pct": current * 100.0,
            "zscore": zscore,
        }
        self._set_cache(cache_key, result)
        return result

    async def _fetch_oi_data(self) -> dict[str, float]:
        cache_key = f"oi:{self._perp_symbol}"
        cached = self._get_cache(cache_key)
        if cached is not None:
            return cached

        history = await self._perp.fetch_open_interest_history(
            self._perp_symbol, "1h", limit=_OI_HISTORY_HOURS
        )
        if not history or len(history) < 2:
            return {"change_pct": 0.0, "value_usd": 0.0}

        oi_now = history[-1]["openInterestAmount"]
        oi_24h = history[0]["openInterestAmount"]
        oi_val = history[-1].get("openInterestValue") or 0.0

        change_pct = ((oi_now - oi_24h) / oi_24h * 100.0) if oi_24h > 0 else 0.0

        result = {"change_pct": change_pct, "value_usd": float(oi_val)}
        self._set_cache(cache_key, result)
        return result

    async def _fetch_basis_data(self) -> float:
        """
        Compute Bybit perp-spot basis in bps.

        Positive → perp premium (leveraged longs dominate).
        Negative → perp discount (de-risking / short pressure).
        """
        cache_key = f"basis:{self._symbol}"
        cached = self._get_cache(cache_key)
        if cached is not None:
            return cached

        spot_ticker, perp_ticker = await asyncio.gather(
            self._spot.fetch_ticker(self._symbol),
            self._perp.fetch_ticker(self._perp_symbol),
        )

        spot_last = spot_ticker.get("last") or spot_ticker.get("close")
        perp_last = perp_ticker.get("last") or perp_ticker.get("close")

        if not spot_last or not perp_last or spot_last <= 0:
            return 0.0

        basis_bps = ((perp_last - spot_last) / spot_last) * 10_000.0
        basis_bps = max(-500.0, min(500.0, basis_bps))
        self._set_cache(cache_key, basis_bps)
        return basis_bps

    async def _fetch_whale_taker_ratio(self) -> float:
        """
        Taker buy/sell volume ratio from Bybit's unified recent-trades feed.

        Uses ccxt's fetch_trades() (side-tagged) rather than an exchange-
        specific raw endpoint, so behavior is verified against ccxt's
        unified contract instead of an assumed Bybit-only method name.
        """
        cache_key = f"whale:{self._perp_symbol}"
        cached = self._get_cache(cache_key)
        if cached is not None:
            return cached

        trades = await self._perp.fetch_trades(self._perp_symbol, limit=_TRADES_LIMIT)
        if not trades:
            self._set_cache(cache_key, 1.0)
            return 1.0

        buy_vol = sum(float(t.get("amount") or 0.0) for t in trades if t.get("side") == "buy")
        sell_vol = sum(float(t.get("amount") or 0.0) for t in trades if t.get("side") == "sell")

        if sell_vol < 1e-9:
            ratio = 10.0 if buy_vol > 1e-9 else 1.0
        else:
            ratio = max(0.0, min(buy_vol / sell_vol, 10.0))

        self._set_cache(cache_key, ratio)
        return ratio

    @staticmethod
    def _compute_stress_score(
        basis_bps: float,
        funding_zscore: float,
        oi_change_pct: float,
    ) -> float:
        """Exchange stress composite [0, 1]. Identical formula to Binance/OKX providers."""
        basis_stress = min(abs(basis_bps) / 100.0, 1.0)
        funding_stress = min(abs(funding_zscore) / 3.0, 1.0)
        oi_stress = min(max(-oi_change_pct, 0.0) / 5.0, 1.0)
        score = _W_BASIS * basis_stress + _W_FR_Z * funding_stress + _W_OI * oi_stress
        return round(min(max(score, 0.0), 1.0), 4)


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_provider: BybitIntelligenceProvider | None = None


def get_bybit_intelligence_provider(
    symbol: str = "BTC/USDT",
    perp_symbol: str = "BTC/USDT:USDT",
) -> BybitIntelligenceProvider:
    """
    Return the module-level BybitIntelligenceProvider singleton.

    The symbol/perp_symbol arguments are only used on first call.
    """
    global _provider
    if _provider is None:
        _provider = BybitIntelligenceProvider(
            symbol=symbol,
            perp_symbol=perp_symbol,
        )
    return _provider
