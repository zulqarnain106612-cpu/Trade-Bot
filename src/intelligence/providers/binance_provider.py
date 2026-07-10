"""
Binance public REST provider for intelligence metrics.

All endpoints used here are unauthenticated Binance Futures public API —
zero cost, no API key required.

DATA COVERAGE (free via Binance public REST):
  ✅ binance_funding_rate_pct     — fapi/v1/fundingRate (latest)
  ✅ funding_rate_zscore          — rolling 30-period z-score from history
  ✅ futures_oi_change_pct        — fapi/v1/openInterestHist 24h delta
  ✅ cross_exchange_basis_spread_bps — (perp_last - spot_last) / spot_last * 10_000
  ✅ whale_buy_sell_ratio         — taker_buy_vol / (total_vol - taker_buy_vol) from klines
  ✅ liquidation_pressure_24h_zscore — funding rate z-score proxy (best free signal)
  ✅ exchange_stress_score        — composite: basis + funding_zscore + OI_chg
  ✅ liquidation_cascade_risk_usd — OI_value * stress_score (order-of-magnitude estimate)

DATA NOT AVAILABLE FREE (set to neutral, confidence reduced):
  ❌ exchange_netflow_7d_zscore   → 0.0   (Glassnode paid)
  ❌ exchange_reserve_ratio       → 0.5   (Glassnode paid)
  ❌ miner_netflow_signal         → 0.0   (Glassnode paid)
  ❌ staking_unlock_risk          → 0.0   (no free source)
  ❌ entity_exchange_imbalance    → 0.0   (Glassnode paid)
  ❌ btc_dominance_regime         → 0.0   (CoinGecko free but 1-min delay / rate-limited)
  ❌ stablecoin_reserve_ratio     → 0.5   (no free real-time source)
  ❌ network_activity_score       → 0.0   (Glassnode paid)

When these 8 fields are unavailable, confidence is reduced from 1.0 by
CONFIDENCE_PENALTY_PER_MISSING (0.05) per field → floor 0.6 (still useful).
Gates that act on confidence can weight their decisions accordingly.

Authority:
  Binance Futures REST API public docs:
    https://binance-docs.github.io/apidocs/futures/en/
  Almgren & Chriss (2001) — basis spread as execution-cost signal
  López de Prado (2018) AFML Ch.4 — taker-flow imbalance as microstructure feature
"""

from __future__ import annotations

import asyncio
import statistics
import time
from typing import Final

import ccxt.async_support as ccxt
import structlog

from src.intelligence.providers.base import ExchangeIntelligenceProvider


log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

# How many funding periods to fetch for z-score baseline (each period = 8h)
_FR_HISTORY_PERIODS: Final[int] = 90  # 30 days of 8h periods
# OI history window for 24h change (hourly bars)
_OI_HISTORY_HOURS: Final[int] = 25  # 25 → delta between [0] and [-1]
# Kline window for taker-flow whale ratio
_KLINE_LIMIT: Final[int] = 48  # 48 x 15m = 12h window
# Stress score weights (must sum to 1.0)
_W_BASIS: Final[float] = 0.35
_W_FR_Z: Final[float] = 0.40
_W_OI: Final[float] = 0.25
# Confidence penalty per missing paid-source field
_CONFIDENCE_PENALTY: Final[float] = 0.05
_MISSING_PAID_FIELDS: Final[int] = 8  # fields that need Glassnode/CQ


class BinanceIntelligenceProvider(ExchangeIntelligenceProvider):
    """
    Fetches IntelligenceMetrics-compatible data from Binance public APIs.

    One instance is expected to be long-lived (shared via singleton) and
    reused across ticks.  All methods are async and safe for concurrent calls.

    Usage::

        provider = BinanceIntelligenceProvider(symbol="BTC/USDT")
        await provider.initialize()
        metrics = await provider.fetch_metrics()
        await provider.close()
    """

    @property
    def exchange_id(self) -> str:
        """Stable lowercase exchange name — required by MultiProviderIntelligenceAggregator."""
        return "binance"

    def __init__(
        self,
        symbol: str = "BTC/USDT",
        perp_symbol: str = "BTC/USDT:USDT",
        kline_timeframe: str = "15m",
        cache_ttl_s: int = 300,  # 5-min cache; funding updates every 8h
    ) -> None:
        self._symbol = symbol
        self._perp_symbol = perp_symbol
        self._kline_tf = kline_timeframe
        self._cache_ttl = cache_ttl_s

        # Two ccxt instances: spot (for basis) + futures (for everything else)
        self._spot = ccxt.binance({"options": {"defaultType": "spot"}})
        self._perp = ccxt.binance({"options": {"defaultType": "future"}})

        # In-memory cache: key → (timestamp_s, value)
        self._cache: dict[str, tuple[float, object]] = {}
        self._log = log.bind(
            component="binance_intelligence",
            symbol=symbol,
            perp_symbol=perp_symbol,
        )

    async def initialize(self) -> None:
        """Load ccxt market definitions (required before API calls)."""
        await asyncio.gather(
            self._spot.load_markets(),
            self._perp.load_markets(),
        )
        self._log.info("binance_intelligence.initialized")

    async def close(self) -> None:
        """Close underlying ccxt sessions."""
        await asyncio.gather(
            self._spot.close(),
            self._perp.close(),
        )

    # ------------------------------------------------------------------
    # Public API — returns a flat dict matching IntelligenceMetrics fields
    # ------------------------------------------------------------------

    async def fetch_metrics(self) -> dict[str, float]:
        """
        Fetch all available free intelligence metrics.

        Returns a dict with every IntelligenceMetrics field populated:
          - Free fields: real computed values
          - Paid-only fields: neutral defaults (0.0 / 0.5) with confidence reduced

        Never raises — on any error the field degrades gracefully to its
        neutral default and confidence is further reduced.

        Returns
        -------
        dict matching IntelligenceMetrics field names + "confidence" + "timestamp"
        """
        ts = int(time.time())
        confidence = 1.0 - (_MISSING_PAID_FIELDS * _CONFIDENCE_PENALTY)  # 0.60 floor

        # Run all free fetches concurrently; each fails independently
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

        # Unpack funding
        funding_rate_pct: float = 0.0
        funding_zscore: float = 0.0
        if isinstance(funding_result, dict):
            funding_rate_pct = funding_result.get("rate_pct", 0.0)
            funding_zscore = funding_result.get("zscore", 0.0)
        else:
            self._log.warning("binance_intelligence.funding_failed", error=str(funding_result))
            confidence -= 0.05

        # Unpack OI
        oi_change_pct: float = 0.0
        oi_value_usd: float = 0.0
        if isinstance(oi_result, dict):
            oi_change_pct = oi_result.get("change_pct", 0.0)
            oi_value_usd = oi_result.get("value_usd", 0.0)
        else:
            self._log.warning("binance_intelligence.oi_failed", error=str(oi_result))
            confidence -= 0.05

        # Unpack basis
        basis_bps: float = 0.0
        if isinstance(basis_result, float):
            basis_bps = basis_result
        else:
            self._log.warning("binance_intelligence.basis_failed", error=str(basis_result))
            confidence -= 0.05

        # Unpack whale ratio
        whale_ratio: float = 1.0
        if isinstance(whale_result, float):
            whale_ratio = whale_result
        else:
            self._log.warning("binance_intelligence.whale_failed", error=str(whale_result))
            confidence -= 0.02

        # Composite exchange stress score [0, 1]
        stress_score = self._compute_stress_score(basis_bps, funding_zscore, oi_change_pct)

        # Estimated cascade liquidation risk (OI x stress, rough order-of-magnitude)
        liquidation_cascade_usd = oi_value_usd * stress_score

        # Liquidation pressure z-score proxy: funding rate z-score is the
        # best freely available signal for near-term liquidation pressure.
        liquidation_pressure_zscore = funding_zscore

        confidence = max(0.0, min(1.0, confidence))

        metrics = {
            # ── FREE: computed from Binance public API ─────────────────────
            "binance_funding_rate_pct": funding_rate_pct,
            "futures_oi_change_pct": oi_change_pct,
            "cross_exchange_basis_spread_bps": basis_bps,
            "whale_buy_sell_ratio": whale_ratio,
            "liquidation_pressure_24h_zscore": liquidation_pressure_zscore,
            "liquidation_cascade_risk_usd": liquidation_cascade_usd,
            "exchange_stress_score": stress_score,
            # ── PAID-SOURCE ONLY: neutral defaults ─────────────────────────
            # These require Glassnode or CryptoQuant paid tiers.
            # Set to neutral (neither bullish nor bearish) so downstream
            # gates treat them as "no signal" rather than false signal.
            "exchange_netflow_7d_zscore": 0.0,  # Glassnode exchange/netflow
            "exchange_reserve_ratio": 0.5,  # Glassnode exchange/reserve
            "miner_netflow_signal": 0.0,  # Glassnode mining/miner_flow
            "staking_unlock_risk": 0.0,  # no free equivalent
            "entity_exchange_imbalance": 0.0,  # Glassnode entities flow
            "btc_dominance_regime": 0.0,  # CoinGecko (rate-limited)
            "stablecoin_reserve_ratio": 0.5,  # no real-time free source
            "network_activity_score": 0.0,  # Glassnode transactions
            # ── Metadata ───────────────────────────────────────────────────
            "confidence": confidence,
            "timestamp": ts,
        }

        self._log.debug(
            "binance_intelligence.metrics_fetched",
            stress_score=round(stress_score, 3),
            basis_bps=round(basis_bps, 2),
            funding_rate_pct=round(funding_rate_pct, 6),
            funding_zscore=round(funding_zscore, 3),
            oi_change_pct=round(oi_change_pct, 3),
            whale_ratio=round(whale_ratio, 3),
            confidence=round(confidence, 2),
        )
        return metrics

    # ------------------------------------------------------------------
    # Private fetch helpers — each isolated, fail-safe
    # ------------------------------------------------------------------

    async def _fetch_funding_data(self) -> dict[str, float]:
        """
        Fetch current funding rate and 30-day z-score.

        Uses Binance Futures public endpoint: GET /fapi/v1/fundingRate
        No authentication required.
        """
        cache_key = f"funding:{self._perp_symbol}"
        cached = self._get_cache(cache_key)
        if cached is not None:
            return cached  # type: ignore[return-value]

        # Fetch history for z-score baseline
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
            "rate_pct": current * 100.0,  # convert to percent
            "zscore": zscore,
        }
        self._set_cache(cache_key, result)
        return result

    async def _fetch_oi_data(self) -> dict[str, float]:
        """
        Fetch 24h open interest change and current OI value.

        Uses Binance Futures public endpoint: GET /fapi/v1/openInterestHist
        """
        cache_key = f"oi:{self._perp_symbol}"
        cached = self._get_cache(cache_key)
        if cached is not None:
            return cached  # type: ignore[return-value]

        history = await self._perp.fetch_open_interest_history(
            self._perp_symbol, "1h", limit=_OI_HISTORY_HOURS
        )
        if not history or len(history) < 2:
            return {"change_pct": 0.0, "value_usd": 0.0}

        oi_now = history[-1]["openInterestAmount"]
        oi_24h = history[0]["openInterestAmount"]
        oi_val = history[-1].get("openInterestValue") or 0.0

        change_pct = ((oi_now - oi_24h) / oi_24h * 100.0) if oi_24h > 0 else 0.0

        result = {"change_pct": change_pct, "value_usd": oi_val}
        self._set_cache(cache_key, result)
        return result

    async def _fetch_basis_data(self) -> float:
        """
        Compute perp-spot basis in bps.

        basis_bps = (perp_last - spot_last) / spot_last x 10_000

        Positive → perp at premium (bullish leverage).
        Negative → perp at discount (bearish/de-risking).
        """
        cache_key = f"basis:{self._symbol}"
        cached = self._get_cache(cache_key)
        if cached is not None:
            return cached  # type: ignore[return-value]

        spot_ticker, perp_ticker = await asyncio.gather(
            self._spot.fetch_ticker(self._symbol),
            self._perp.fetch_ticker(self._perp_symbol),
        )

        spot_last = spot_ticker.get("last") or spot_ticker.get("close")
        perp_last = perp_ticker.get("last") or perp_ticker.get("close")

        if not spot_last or not perp_last or spot_last <= 0:
            return 0.0

        basis_bps = ((perp_last - spot_last) / spot_last) * 10_000.0
        # Clamp to ±500 bps (beyond this is data error / exchange anomaly)
        basis_bps = max(-500.0, min(500.0, basis_bps))
        self._set_cache(cache_key, basis_bps)
        return basis_bps

    async def _fetch_whale_taker_ratio(self) -> float:
        """
        Compute taker buy ratio as a proxy for whale/smart-money flow.

        Uses Binance Futures klines (public, no key) with taker buy volume.
        taker_buy_ratio = sum(taker_buy_vol) / sum(total_vol) over last 12h.

        Values > 0.55 suggest aggressive buying (bullish).
        Values < 0.45 suggest aggressive selling (bearish).
        Returns ratio in [0, 10] to match IntelligenceMetrics.whale_buy_sell_ratio
        convention (buy_vol / sell_vol, where 1.0 = neutral).
        """
        cache_key = f"whale:{self._perp_symbol}"
        cached = self._get_cache(cache_key)
        if cached is not None:
            return cached  # type: ignore[return-value]

        # BUG FIX (found + verified live this session): ccxt's unified
        # fetch_ohlcv() normalizes every exchange to the standard 6-field
        # [ts, open, high, low, close, volume] tuple -- it silently drops
        # taker-buy-volume even though Binance's raw REST response includes
        # it. The old code here checked `len(bar) > 7`, which is *always*
        # false against ccxt's normalized output, so this method returned
        # the hardcoded neutral 1.0 on every single call, unconditionally,
        # with no exception and no confidence penalty -- meaning
        # whale_buy_sell_ratio (which feeds the live check_whale_activity
        # gate) has never reflected real data. Confirmed via a live call:
        # ccxt fetch_ohlcv returned exactly 6 elements per bar.
        #
        # On top of that, the original index (7) was also wrong even for
        # the raw format: Binance's documented kline schema
        # (https://binance-docs.github.io/apidocs/futures/en/#kline-candlestick-data)
        # is [open_time, open, high, low, close, volume, close_time,
        # quote_asset_volume, number_of_trades, taker_buy_base_asset_volume,
        # taker_buy_quote_asset_volume, ignore] -- taker_buy_base_asset_volume
        # is index 9, not 7 (7 is quote_asset_volume). Verified live against
        # the raw (non-normalized) endpoint.
        #
        # Fix: call the raw/implicit ccxt method (bypasses normalization)
        # and read the correct index.
        market = self._perp.market(self._perp_symbol)
        raw = await self._perp.fapiPublicGetKlines(
            {
                "symbol": market["id"],
                "interval": self._kline_tf,
                "limit": _KLINE_LIMIT,
            }
        )
        if not raw:
            return 1.0

        total_vol = sum(float(bar[5]) for bar in raw if bar[5] is not None)
        taker_buy_vol = sum(float(bar[9]) for bar in raw if len(bar) > 9 and bar[9] is not None)

        if total_vol < 1e-9 or taker_buy_vol == 0.0:
            return 1.0

        taker_buy_vol / total_vol  # [0, 1]
        sell_vol = total_vol - taker_buy_vol
        # Ratio convention: buy / sell (>1 = net buy, <1 = net sell)
        ratio = taker_buy_vol / sell_vol if sell_vol > 1e-9 else 10.0
        ratio = min(ratio, 10.0)  # cap at 10

        self._set_cache(cache_key, ratio)
        return ratio

    # ------------------------------------------------------------------
    # Stress score composite
    # ------------------------------------------------------------------

    @staticmethod
    def _compute_stress_score(
        basis_bps: float,
        funding_zscore: float,
        oi_change_pct: float,
    ) -> float:
        """
        Exchange stress composite [0, 1].

        Components:
          basis_bps     : extreme positive or negative basis signals fragmentation
          funding_zscore: high z-score = elevated leverage = stress
          oi_change_pct : rapid OI drop = forced deleveraging = stress

        All components normalised to [0, 1] before weighting.
        Higher = more stress.
        """
        # Basis: stress from extreme basis in either direction (>50 bps = stressed)
        basis_stress = min(abs(basis_bps) / 100.0, 1.0)

        # Funding: high z-score → over-leveraged → stress
        funding_stress = min(abs(funding_zscore) / 3.0, 1.0)

        # OI drop: rapid deleveraging (negative OI change) = forced liquidations
        oi_stress = min(max(-oi_change_pct, 0.0) / 5.0, 1.0)

        score = _W_BASIS * basis_stress + _W_FR_Z * funding_stress + _W_OI * oi_stress
        return round(min(max(score, 0.0), 1.0), 4)

    # ------------------------------------------------------------------
    # Cache helpers
    # ------------------------------------------------------------------

    def _get_cache(self, key: str) -> object | None:
        entry = self._cache.get(key)
        if entry is None:
            return None
        ts, value = entry
        if time.time() - ts > self._cache_ttl:
            return None
        return value

    def _set_cache(self, key: str, value: object) -> None:
        self._cache[key] = (time.time(), value)


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_provider: BinanceIntelligenceProvider | None = None


def get_binance_intelligence_provider(
    symbol: str = "BTC/USDT",
    perp_symbol: str = "BTC/USDT:USDT",
) -> BinanceIntelligenceProvider:
    """
    Return the module-level BinanceIntelligenceProvider singleton.

    The symbol/perp_symbol arguments are only used on first call —
    subsequent calls return the existing instance regardless of args.
    """
    global _provider
    if _provider is None:
        _provider = BinanceIntelligenceProvider(
            symbol=symbol,
            perp_symbol=perp_symbol,
        )
    return _provider
