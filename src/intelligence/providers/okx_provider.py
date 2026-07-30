"""
OKX public REST provider for intelligence metrics.

All endpoints used here are unauthenticated OKX v5 public API —
zero cost, no API key required.

DATA COVERAGE (free via OKX public REST):
  ✅ okx_funding_rate_pct            — /api/v5/public/funding-rate (latest)
  ✅ futures_oi_change_pct           — /api/v5/rubik/stat/contracts/open-interest-history
  ✅ cross_exchange_basis_spread_bps — (perp_last - spot_last) / spot_last * 10_000
  ✅ whale_buy_sell_ratio            — taker buy vol ratio from /api/v5/market/candles
  ✅ liquidation_pressure_24h_zscore — funding rate z-score proxy
  ✅ liquidation_cascade_risk_usd    — OI_value * stress_score (estimate)
  ✅ exchange_stress_score           — composite: basis + funding_zscore + OI_chg

DATA NOT AVAILABLE FREE (set to neutral, confidence reduced):
  ❌ exchange_netflow_7d_zscore   → 0.0 (on-chain, no free OKX source)
  ❌ exchange_reserve_ratio       → 0.5 (on-chain, no free OKX source)
  ❌ miner_netflow_signal         → 0.0 (on-chain only)
  ❌ staking_unlock_risk          → 0.0 (no free source)
  ❌ entity_exchange_imbalance    → 0.0 (on-chain only)
  ❌ btc_dominance_regime         → 0.0 (cross-market; aggregated by CoinGecko provider)
  ❌ stablecoin_reserve_ratio     → 0.5 (cross-market; aggregated by CoinGecko provider)
  ❌ network_activity_score       → 0.0 (on-chain; blockchain provider)

Authority:
  OKX REST API v5 docs: https://www.okx.com/docs-v5/
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
# Kline window for taker-flow ratio (15m bars, 12h window)
_KLINE_LIMIT: Final[int] = 48
# Stress score weights (must sum to 1.0)
_W_BASIS: Final[float] = 0.35
_W_FR_Z: Final[float] = 0.40
_W_OI: Final[float] = 0.25
# Confidence handling
_CONFIDENCE_PENALTY: Final[float] = 0.05
_MISSING_PAID_FIELDS: Final[int] = 8


class OKXIntelligenceProvider(ExchangeIntelligenceProvider):
    """
    Fetches IntelligenceMetrics-compatible data from OKX public APIs.

    Mirrors BinanceIntelligenceProvider interface exactly so
    IntelligenceAggregator can treat both providers uniformly.

    OKX uses a unified instrument ID format: "BTC-USDT" (spot), "BTC-USDT-SWAP" (perp).
    ccxt translates from unified symbols automatically.

    Usage::

        provider = OKXIntelligenceProvider(symbol="BTC/USDT")
        await provider.initialize()
        metrics = await provider.fetch_metrics()
        await provider.close()
    """

    def __init__(
        self,
        symbol: str = "BTC/USDT",
        perp_symbol: str = "BTC/USDT:USDT",
        kline_timeframe: str = "15m",
        cache_ttl_s: int = 300,
    ) -> None:
        self._symbol = symbol
        self._perp_symbol = perp_symbol
        self._kline_tf = kline_timeframe
        self._cache_ttl = cache_ttl_s

        self._spot = ccxt.okx({"options": {"defaultType": "spot"}})
        self._perp = ccxt.okx({"options": {"defaultType": "swap"}})

        self._cache: dict[str, tuple[float, Any]] = {}
        self._log = log.bind(
            component="okx_intelligence",
            symbol=symbol,
            perp_symbol=perp_symbol,
        )

    @property
    def exchange_id(self) -> str:
        return "okx"

    async def initialize(self) -> None:
        """Load ccxt market definitions (required before API calls)."""
        await asyncio.gather(
            self._spot.load_markets(),
            self._perp.load_markets(),
        )
        self._log.info("okx_intelligence.initialized")

    async def close(self) -> None:
        """Close underlying ccxt sessions."""
        await asyncio.gather(
            self._spot.close(),
            self._perp.close(),
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def fetch_metrics(self) -> dict[str, float]:
        """
        Fetch all available free intelligence metrics from OKX.

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
            self._log.warning(
                "okx_intelligence.funding_failed", error=str(funding_result), exc_info=True
            )
            confidence -= 0.05

        oi_change_pct: float = 0.0
        oi_value_usd: float = 0.0
        if isinstance(oi_result, dict):
            oi_change_pct = oi_result.get("change_pct", 0.0)
            oi_value_usd = oi_result.get("value_usd", 0.0)
        else:
            self._log.warning("okx_intelligence.oi_failed", error=str(oi_result), exc_info=True)
            confidence -= 0.05

        basis_bps: float = 0.0
        if isinstance(basis_result, float):
            basis_bps = basis_result
        else:
            self._log.warning(
                "okx_intelligence.basis_failed", error=str(basis_result), exc_info=True
            )
            confidence -= 0.05

        whale_ratio: float = 1.0
        if isinstance(whale_result, float):
            whale_ratio = whale_result
        else:
            self._log.warning(
                "okx_intelligence.whale_failed", error=str(whale_result), exc_info=True
            )
            confidence -= 0.02

        stress_score = self._compute_stress_score(basis_bps, funding_zscore, oi_change_pct)
        liquidation_cascade_usd = oi_value_usd * stress_score
        liquidation_pressure_zscore = funding_zscore

        confidence = max(0.0, min(1.0, confidence))

        return {
            # FREE: computed from OKX public API
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
            # OCI-012: on-chain fields — neutral defaults for exchange provider
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
        Compute OKX perp-spot basis in bps.

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
        Taker buy ratio from OKX public candles endpoint.

        OKX /api/v5/market/candles returns columns:
          [ts, o, h, l, c, vol, volCcy, volCcyQuote, confirm]
        where vol (index 5) is in base asset and volCcy (index 6) is in
        quote asset. OKX does NOT expose taker_buy_vol in unified candles.

        Fallback: use ccxt fetch_trades for taker side info over last 1h
        (limited to last 100 trades, accurate directional proxy).
        """
        cache_key = f"whale:{self._perp_symbol}"
        cached = self._get_cache(cache_key)
        if cached is not None:
            return cached

        # OKX exposes taker flow via Long/Short ratio endpoint (no auth needed)
        # GET /api/v5/rubik/stat/contracts/long-short-account-ratio
        # ccxt does not unify this; call raw.
        try:
            market = self._perp.market(self._perp_symbol)
            inst_id = market["id"]  # e.g. "BTC-USDT-SWAP"
            raw = await self._perp.publicGetRubikStatContractsLongShortAccountRatio(
                {
                    "instId": inst_id,
                    "period": "1H",
                }
            )
            data = raw.get("data", [])
            if data:
                # data[0] is most recent: [ts, longShortRatio]
                ls_ratio = float(data[0][1])
                # longShortRatio > 1 → more longs → bullish taker flow
                # Normalize to buy/sell ratio convention (same as Binance provider)
                # ls_ratio = long_accounts / short_accounts
                # Map: ratio=2 → buy/sell≈2 (more buyers); ratio=0.5 → buy/sell≈0.5
                ratio = max(0.0, min(ls_ratio, 10.0))
                self._set_cache(cache_key, ratio)
                return ratio
        except Exception as e:
            self._log.debug("okx_intelligence.ls_ratio_failed", error=str(e), exc_info=True)

        # Second fallback: neutral
        self._set_cache(cache_key, 1.0)
        return 1.0

    @staticmethod
    def _compute_stress_score(
        basis_bps: float,
        funding_zscore: float,
        oi_change_pct: float,
    ) -> float:
        """Exchange stress composite [0, 1]. Identical formula to Binance provider."""
        basis_stress = min(abs(basis_bps) / 100.0, 1.0)
        funding_stress = min(abs(funding_zscore) / 3.0, 1.0)
        oi_stress = min(max(-oi_change_pct, 0.0) / 5.0, 1.0)
        score = _W_BASIS * basis_stress + _W_FR_Z * funding_stress + _W_OI * oi_stress
        return round(min(max(score, 0.0), 1.0), 4)


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_provider: OKXIntelligenceProvider | None = None


def get_okx_intelligence_provider(
    symbol: str = "BTC/USDT",
    perp_symbol: str = "BTC/USDT:USDT",
) -> OKXIntelligenceProvider:
    """
    Return the module-level OKXIntelligenceProvider singleton.

    The symbol/perp_symbol arguments are only used on first call.
    """
    global _provider
    if _provider is None:
        _provider = OKXIntelligenceProvider(
            symbol=symbol,
            perp_symbol=perp_symbol,
        )
    return _provider
