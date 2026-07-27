"""
Multi-provider intelligence client aggregator.

Responsibilities:
  - Manage credentials for all providers
  - Coordinate API calls with caching
  - Handle provider-specific rate limits
  - Fallback strategy if one provider fails
  - Data validation and standardization
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import structlog


log = structlog.get_logger(__name__)


@dataclass
class CacheEntry:
    """Single cached metric with TTL."""

    value: Any
    fetched_at: datetime
    ttl_seconds: int

    @property
    def is_stale(self) -> bool:
        """Check if entry has expired."""
        age = (datetime.now(UTC) - self.fetched_at).total_seconds()
        return age > self.ttl_seconds


class IntelligenceAggregator:
    """
    Central hub for crypto intelligence data.

    Manages:
      - Glassnode on-chain metrics (exchange netflow, whale activity)
      - Binance perpetual futures funding rate via ccxt (no key required)
      - Local cache (with configurable TTL per metric)
      - Rate limiting per provider
      - Fallback logic (stale data > safe defaults, never silent zeros)

    GAP-015: Constructor now reads from IntelligenceSettings (config.py)
    by default, so callers that use get_intelligence_aggregator() get a
    fully-configured instance without having to wire keys manually.
    Direct-kwarg construction is preserved for backward compat and tests.
    """

    def __init__(
        self,
        glassnode_api_key: str | None = None,
        cryptoquant_api_key: str | None = None,
        cache_ttl_onchain_seconds: int | None = None,
        cache_ttl_exchange_seconds: int | None = None,
        _settings=None,  # injected in tests; reads get_settings() if None
    ) -> None:
        from src.config import get_settings

        cfg = _settings if _settings is not None else get_settings().intelligence

        self.glassnode_key = glassnode_api_key or cfg.glassnode_api_key
        self.cryptoquant_key = cryptoquant_api_key or cfg.cryptoquant_api_key
        self.cache_ttl_onchain = (
            cache_ttl_onchain_seconds
            if cache_ttl_onchain_seconds is not None
            else cfg.cache_ttl_onchain_seconds
        )
        self.cache_ttl_exchange = (
            cache_ttl_exchange_seconds
            if cache_ttl_exchange_seconds is not None
            else cfg.cache_ttl_exchange_seconds
        )
        self._glassnode_base_url = cfg.glassnode_base_url
        self._glassnode_min_interval = cfg.glassnode_rate_limit_seconds
        self._funding_rate_perp_symbol = cfg.funding_rate_perp_symbol
        self._cache: dict[str, CacheEntry] = {}
        self._cache_lock: asyncio.Lock = asyncio.Lock()
        self._last_glassnode_call: datetime = datetime.fromtimestamp(0, UTC)
        self._last_cryptoquant_call: datetime = datetime.fromtimestamp(0, UTC)

        log.info(
            "intelligence_aggregator_init",
            glassnode_enabled=bool(self.glassnode_key),
            cryptoquant_enabled=bool(self.cryptoquant_key),
            funding_rate_via="binance_ccxt_public",
        )

    async def get_exchange_netflow(
        self,
        symbol: str = "BTC",
        exchange: str | None = None,  # None = aggregate all
        days_back: int = 7,
    ) -> dict[str, float]:
        """
        Fetch exchange netflow (inflow - outflow).

        Returns:
            {
                "netflow": float,           # BTC (negative = sellers leaving)
                "inflow": float,            # BTC incoming
                "outflow": float,           # BTC outgoing
                "tscore": float,            # z-score vs 30d MA
                "timestamp": int,           # Unix seconds
            }
        """
        cache_key = f"exchange_netflow_{symbol}_{exchange or 'all'}_{days_back}d"

        # Guard: hold lock for entire check-then-write to prevent TOCTOU races
        # between concurrent coroutines hitting the same cache key simultaneously.
        async with self._cache_lock:
            if cache_key in self._cache and not self._cache[cache_key].is_stale:
                return self._cache[cache_key].value

        # Fetch from Glassnode (outside lock — long-running I/O should not block peers)
        try:
            result = await self._fetch_glassnode_netflow(
                symbol=symbol, exchange=exchange, days_back=days_back
            )
            async with self._cache_lock:
                self._cache[cache_key] = CacheEntry(
                    value=result,
                    fetched_at=datetime.now(UTC),
                    ttl_seconds=self.cache_ttl_exchange,
                )
            return result
        except Exception as e:
            log.error("glassnode_netflow_fetch_failed", error=str(e), symbol=symbol)
            async with self._cache_lock:
                if cache_key in self._cache:
                    return self._cache[cache_key].value
            return {"netflow": 0.0, "inflow": 0.0, "outflow": 0.0, "tscore": 0.0}

    async def get_whale_activity(
        self,
        symbol: str = "BTC",
        min_transaction_usd: float = 1_000_000,
    ) -> dict[str, float | str | int]:
        """
        Fetch whale transaction activity.

        Returns:
            {
                "buy_volume": float,        # BTC from large buy txns
                "sell_volume": float,       # BTC from large sell txns
                "ratio": float,             # buy_volume / sell_volume
                "sentiment": str,           # "bullish" | "bearish" | "neutral"
                "timestamp": int,
            }
        """
        cache_key = f"whale_activity_{symbol}_{min_transaction_usd}"

        async with self._cache_lock:
            if cache_key in self._cache and not self._cache[cache_key].is_stale:
                return self._cache[cache_key].value

        try:
            result = await self._fetch_glassnode_whale_activity(
                symbol=symbol, min_transaction_usd=min_transaction_usd
            )
            async with self._cache_lock:
                self._cache[cache_key] = CacheEntry(
                    value=result,
                    fetched_at=datetime.now(UTC),
                    ttl_seconds=self.cache_ttl_onchain,
                )
            return result
        except Exception as e:
            log.error("glassnode_whale_activity_failed", error=str(e))
            async with self._cache_lock:
                if cache_key in self._cache:
                    return self._cache[cache_key].value
            return {"buy_volume": 0.0, "sell_volume": 0.0, "ratio": 1.0, "sentiment": "neutral"}

    async def get_funding_rate(
        self,
        symbol: str | None = None,
    ) -> dict[str, float]:
        """
        Fetch current funding rate (leverage indicator).

        Returns:
            {
                "rate_pct": float,          # Funding rate as %
                "rate_8h_avg": float,       # 8h moving average
                "excessive": bool,          # rate_pct > 0.1% = excessive leverage
                "timestamp": int,
            }
        """
        symbol = symbol or self._funding_rate_perp_symbol
        cache_key = f"funding_rate_{symbol}"

        async with self._cache_lock:
            if cache_key in self._cache and not self._cache[cache_key].is_stale:
                return self._cache[cache_key].value

        try:
            result = await self._fetch_cryptoquant_funding_rate(symbol=symbol)
            async with self._cache_lock:
                self._cache[cache_key] = CacheEntry(
                    value=result,
                    fetched_at=datetime.now(UTC),
                    ttl_seconds=self.cache_ttl_exchange,
                )
            return result
        except Exception as e:
            log.error("cryptoquant_funding_rate_failed", error=str(e))
            async with self._cache_lock:
                if cache_key in self._cache:
                    return self._cache[cache_key].value
            return {"rate_pct": 0.0, "rate_8h_avg": 0.0, "excessive": False}

    # -----------------------------------------------------------------------
    # Private provider implementations
    # -----------------------------------------------------------------------

    async def _fetch_glassnode_netflow(
        self,
        symbol: str,
        exchange: str | None,
        days_back: int,
    ) -> dict[str, float]:
        """
        Call Glassnode exchange net-flow endpoint.

        Endpoint: GET /v1/metrics/transactions/transfers_volume_exchanges_net
        Auth:     X-Api-Key header (Professional tier required for exchange-
                  flow metrics; Starter tier will get 403)
        Response: [{"t": unix_s, "v": btc_float}, ...] newest-last

        Requires INTELLIGENCE_GLASSNODE_API_KEY in .env.
        Raises if key is absent so the caller's except-branch logs loudly
        rather than silently returning zeros.
        """
        await self._rate_limit_glassnode()

        if not self.glassnode_key:
            raise RuntimeError(
                "INTELLIGENCE_GLASSNODE_API_KEY not set — set it in .env or disable on-chain gates"
            )

        import httpx

        now_ts = int(datetime.now(UTC).timestamp())
        since_ts = now_ts - days_back * 86_400

        params: dict[str, Any] = {
            "a": symbol.upper(),
            "i": "24h",
            "s": str(since_ts),
            "u": str(now_ts),
        }
        if exchange:
            params["e"] = exchange.lower()

        headers = {"X-Api-Key": self.glassnode_key}

        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(
                f"{self._base_url}/transactions/transfers_volume_exchanges_net",
                params=params,
                headers=headers,
            )
            resp.raise_for_status()
            data: list[dict] = resp.json()

        if not data:
            raise ValueError("Glassnode returned empty netflow series")

        # data is sorted oldest-first; we want the most-recent window
        values = [float(row["v"]) for row in data if row.get("v") is not None]
        if not values:
            raise ValueError("All Glassnode netflow values were null")

        latest_netflow = values[-1]
        # z-score vs the window mean/std — gives a normalised stress signal
        mean_v = float(sum(values) / len(values))
        std_v = float((sum((v - mean_v) ** 2 for v in values) / max(len(values) - 1, 1)) ** 0.5)
        zscore = (latest_netflow - mean_v) / std_v if std_v > 1e-9 else 0.0

        # Approximate inflow/outflow split: netflow = inflow - outflow.
        # Glassnode separates these on different endpoints; we only call one
        # here to minimise API credits.  Downstream consumers only need
        # netflow and z-score, so this split is informational only.
        inflow = max(latest_netflow, 0.0)
        outflow = abs(min(latest_netflow, 0.0))

        self._last_glassnode_call = datetime.now(UTC)
        return {
            "netflow": latest_netflow,
            "inflow": inflow,
            "outflow": outflow,
            "tscore": round(zscore, 4),
            "timestamp": int(data[-1]["t"]),
        }

    async def _fetch_glassnode_whale_activity(
        self,
        symbol: str,
        min_transaction_usd: float,
    ) -> dict[str, float | str | int]:
        """
        Estimate whale buy/sell ratio from Glassnode large-transaction volume.

        Endpoint used: /v1/metrics/transactions/transfers_volume_large
        This gives the total volume of transactions above a USD threshold.
        We approximate buy vs sell split using the 24h price direction
        (a common on-chain heuristic when exchange-specific flow data is
        unavailable or cost-prohibitive at higher granularity).

        Requires INTELLIGENCE_GLASSNODE_API_KEY (Professional tier).
        """
        await self._rate_limit_glassnode()

        if not self.glassnode_key:
            raise RuntimeError(
                "INTELLIGENCE_GLASSNODE_API_KEY not set — set it in .env or disable on-chain gates"
            )

        import httpx

        now_ts = int(datetime.now(UTC).timestamp())
        since_ts = now_ts - 7 * 86_400  # 7-day window for ratio stability

        headers = {"X-Api-Key": self.glassnode_key}

        # Large-tx volume (all directions, no buy/sell split at this tier)
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(
                f"{self._base_url}/transactions/transfers_volume_large",
                params={
                    "a": symbol.upper(),
                    "i": "24h",
                    "s": str(since_ts),
                    "u": str(now_ts),
                    "min_value": str(int(min_transaction_usd)),
                },
                headers=headers,
            )
            resp.raise_for_status()
            data: list[dict] = resp.json()

        if not data:
            raise ValueError("Glassnode returned empty large-tx series")

        values = [float(row["v"]) for row in data if row.get("v") is not None]
        if not values:
            raise ValueError("All Glassnode large-tx values were null")

        # Heuristic: compare recent 2-day avg vs prior 5-day avg.
        # Rising large-tx volume in recent bars → net accumulation signal.
        recent = values[-2:] if len(values) >= 2 else values
        prior = values[:-2] if len(values) > 2 else values
        recent_avg = sum(recent) / len(recent)
        prior_avg = sum(prior) / max(len(prior), 1)

        # ratio > 1 = whale volume rising (buy-side pressure), < 1 = falling
        ratio = recent_avg / max(prior_avg, 1e-9)
        ratio = max(0.1, min(ratio, 10.0))  # clamp to [0.1, 10]

        buy_vol = recent_avg * ratio / (1.0 + ratio)
        sell_vol = recent_avg / (1.0 + ratio)

        sentiment = "bullish" if ratio > 1.5 else "bearish" if ratio < 0.67 else "neutral"

        self._last_glassnode_call = datetime.now(UTC)
        return {
            "buy_volume": round(buy_vol, 4),
            "sell_volume": round(sell_vol, 4),
            "ratio": round(ratio, 4),
            "sentiment": sentiment,
            "timestamp": int(data[-1]["t"]),
        }

    async def _fetch_cryptoquant_funding_rate(
        self,
        symbol: str,
    ) -> dict[str, float]:
        """
        Fetch perpetual futures funding rate via ccxt (Binance public API).

        GAP-015 / TASK-010 implementation decision:
        CryptoQuant provides funding-rate data but is a paid, key-gated API
        with no meaningful advantage over Binance's own public endpoint for
        this specific metric.  ccxt (already a pinned dependency) wraps
        Binance's /fapi/v1/fundingRate endpoint with built-in retry and
        response normalisation.  We use a read-only, unauthenticated ccxt
        instance pointing at the futures market (defaultType: future) — no
        API key needed, no order-placement risk, completely isolated from
        the spot-trading ccxt instance used by MarketDataFetcher.

        `symbol` should be a ccxt perpetual symbol, e.g. "BTC/USDT:USDT".
        The caller (get_funding_rate) defaults to "BTCUSDT"; we normalise
        below.

        Note: this bot trades SPOT pairs.  Funding rate from the perpetual
        market is used purely as a leverage-sentiment signal — a high
        positive rate means the market is crowded long, which increases the
        probability of a long-squeeze adverse move against a spot long.
        """
        import ccxt.async_support as ccxt_async

        # Normalise: "BTCUSDT" → "BTC/USDT:USDT" (ccxt unified perp format)
        if ":" not in symbol and "/" not in symbol:
            # e.g. "BTCUSDT" → "BTC/USDT:USDT"
            base = symbol.replace("USDT", "")
            symbol_ccxt = f"{base}/USDT:USDT"
        else:
            symbol_ccxt = symbol

        exchange = ccxt_async.binance({"options": {"defaultType": "future"}})
        try:
            # fetch_funding_rate returns the *next* scheduled rate
            funding = await exchange.fetch_funding_rate(symbol_ccxt)
            rate_pct = float(funding.get("fundingRate", 0.0)) * 100.0

            # fetch_funding_rate_history for an 8h rolling avg
            history = await exchange.fetch_funding_rate_history(symbol_ccxt, limit=3)
            if history:
                rates = [float(r.get("fundingRate", 0.0)) * 100.0 for r in history]
                avg_8h = sum(rates) / len(rates)
            else:
                avg_8h = rate_pct

            ts = int(funding.get("timestamp") or datetime.now(UTC).timestamp() * 1000) // 1000

            return {
                "rate_pct": round(rate_pct, 6),
                "rate_8h_avg": round(avg_8h, 6),
                "excessive": abs(rate_pct) > 0.1,
                "timestamp": ts,
            }
        finally:
            await exchange.close()

    # -----------------------------------------------------------------------
    # Rate limiting helpers
    # -----------------------------------------------------------------------

    async def _rate_limit_glassnode(self) -> None:
        """Enforce minimum inter-call spacing for Glassnode (default 1s)."""
        elapsed = (datetime.now(UTC) - self._last_glassnode_call).total_seconds()
        wait = self._glassnode_min_interval - elapsed
        if wait > 0:
            await asyncio.sleep(wait)

    @property
    def _base_url(self) -> str:
        return self._glassnode_base_url

    # -----------------------------------------------------------------------
    # Historical-range fetch methods (GAP-015 step 2)
    # Used by scripts/backfill_intelligence.py to build training history.
    # These mirror the live methods but accept explicit since_ts/until_ts
    # Unix-second boundaries and return a list of (timestamp, value) pairs
    # instead of a single snapshot.
    # -----------------------------------------------------------------------

    async def get_exchange_netflow_history(
        self,
        symbol: str = "BTC",
        since_ts: int = 0,
        until_ts: int = 0,
        interval: str = "24h",
        exchange: str | None = None,
    ) -> list[dict]:
        """
        Fetch historical exchange netflow from Glassnode.

        Args:
            symbol:    Asset symbol (e.g. "BTC").
            since_ts:  Start of range, Unix seconds (inclusive).
            until_ts:  End of range, Unix seconds (inclusive). 0 = now.
            interval:  Glassnode resolution ("24h" | "1h"). Professional tier
                       required for sub-24h resolution.
            exchange:  Exchange slug (e.g. "binance") or None for aggregate.

        Returns:
            List of dicts: [{"ts": int, "netflow": float, "tscore": float}, ...]
            Sorted ascending by ts. Empty list if key absent or API error.
        """
        if not self.glassnode_key:
            log.warning(
                "get_exchange_netflow_history_skipped",
                reason="GLASSNODE_API_KEY not set",
            )
            return []

        await self._rate_limit_glassnode()

        import httpx

        now_ts = int(datetime.now(UTC).timestamp())
        u_ts = until_ts if until_ts > 0 else now_ts

        params: dict[str, Any] = {
            "a": symbol.upper(),
            "i": interval,
            "s": str(since_ts),
            "u": str(u_ts),
        }
        if exchange:
            params["e"] = exchange.lower()

        headers = {"X-Api-Key": self.glassnode_key}

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.get(
                    f"{self._base_url}/transactions/transfers_volume_exchanges_net",
                    params=params,
                    headers=headers,
                )
                resp.raise_for_status()
                data: list[dict] = resp.json()
        except Exception as exc:
            log.error("glassnode_netflow_history_failed", error=str(exc))
            return []
        finally:
            self._last_glassnode_call = datetime.now(UTC)

        values = [(int(row["t"]), float(row["v"])) for row in data if row.get("v") is not None]
        if not values:
            return []

        all_v = [v for _, v in values]
        mean_v = sum(all_v) / len(all_v)
        std_v = (sum((v - mean_v) ** 2 for v in all_v) / max(len(all_v) - 1, 1)) ** 0.5

        return [
            {
                "ts": ts,
                "netflow": v,
                "tscore": round((v - mean_v) / std_v, 4) if std_v > 1e-9 else 0.0,
            }
            for ts, v in sorted(values, key=lambda x: x[0])
        ]

    async def get_whale_activity_history(
        self,
        symbol: str = "BTC",
        since_ts: int = 0,
        until_ts: int = 0,
        interval: str = "24h",
        min_transaction_usd: float = 1_000_000,
    ) -> list[dict]:
        """
        Fetch historical whale large-transaction volume from Glassnode.

        Returns:
            List of dicts: [{"ts": int, "ratio": float, "sentiment": str}, ...]
            Sorted ascending by ts.
        """
        if not self.glassnode_key:
            log.warning(
                "get_whale_activity_history_skipped",
                reason="GLASSNODE_API_KEY not set",
            )
            return []

        await self._rate_limit_glassnode()

        import httpx

        now_ts = int(datetime.now(UTC).timestamp())
        u_ts = until_ts if until_ts > 0 else now_ts

        headers = {"X-Api-Key": self.glassnode_key}

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.get(
                    f"{self._base_url}/transactions/transfers_volume_large",
                    params={
                        "a": symbol.upper(),
                        "i": interval,
                        "s": str(since_ts),
                        "u": str(u_ts),
                        "min_value": str(int(min_transaction_usd)),
                    },
                    headers=headers,
                )
                resp.raise_for_status()
                data: list[dict] = resp.json()
        except Exception as exc:
            log.error("glassnode_whale_history_failed", error=str(exc))
            return []
        finally:
            self._last_glassnode_call = datetime.now(UTC)

        values = [(int(row["t"]), float(row["v"])) for row in data if row.get("v") is not None]
        if not values:
            return []

        # Compute rolling ratio: compare each bar against a 7-bar trailing window
        result = []
        for i, (ts, vol) in enumerate(sorted(values, key=lambda x: x[0])):
            window_start = max(0, i - 6)
            prior = [v for _, v in values[window_start:i]] or [vol]
            prior_avg = sum(prior) / len(prior)
            ratio = vol / max(prior_avg, 1e-9)
            ratio = max(0.1, min(ratio, 10.0))
            sentiment = "bullish" if ratio > 1.5 else "bearish" if ratio < 0.67 else "neutral"
            result.append({"ts": ts, "ratio": round(ratio, 4), "sentiment": sentiment})

        return result

    async def get_funding_rate_history(
        self,
        symbol: str | None = None,
        since_ts: int = 0,
        limit: int = 1000,
    ) -> list[dict]:
        """
        Fetch historical funding rate series from Binance perpetuals (public, no key).

        Args:
            symbol:   ccxt-format perp symbol, e.g. "BTC/USDT:USDT". Defaults
                      to config value (BTCUSDT).
            since_ts: Start Unix milliseconds. 0 = earliest available.
            limit:    Max records per call (Binance cap: 1000).

        Returns:
            List of dicts: [{"ts": int, "rate_pct": float}, ...]  (ts in Unix ms)
        """
        import ccxt.async_support as ccxt_async

        symbol = symbol or self._funding_rate_perp_symbol
        if ":" not in symbol and "/" not in symbol:
            base = symbol.replace("USDT", "")
            symbol_ccxt = f"{base}/USDT:USDT"
        else:
            symbol_ccxt = symbol

        exchange = ccxt_async.binance({"options": {"defaultType": "future"}})
        try:
            history = await exchange.fetch_funding_rate_history(
                symbol_ccxt,
                since=since_ts if since_ts > 0 else None,
                limit=limit,
            )
            return [
                {
                    "ts": int(r["timestamp"]),
                    "rate_pct": round(float(r.get("fundingRate", 0.0)) * 100.0, 6),
                }
                for r in history
                if r.get("timestamp") is not None
            ]
        except Exception as exc:
            log.error("binance_funding_rate_history_failed", error=str(exc))
            return []
        finally:
            await exchange.close()


def get_intelligence_aggregator() -> IntelligenceAggregator:
    """
    Factory: return a fully-configured IntelligenceAggregator from settings.

    GAP-015: This is the canonical way to get an aggregator instance in
    production code. Tests should construct IntelligenceAggregator directly
    with _settings= injection to avoid reading .env.
    """
    return IntelligenceAggregator()
