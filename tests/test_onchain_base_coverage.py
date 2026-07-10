"""Tests for src/intelligence/onchain/base.py."""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.intelligence.onchain.base import (
    AsyncHTTPCache,
    CircuitBreaker,
    CircuitOpenError,
    OnChainProvider,
    RateLimiter,
)


# ---------------------------------------------------------------------------
# AsyncHTTPCache
# ---------------------------------------------------------------------------


class TestAsyncHTTPCache:
    @pytest.mark.asyncio
    async def test_get_miss_returns_none(self):
        cache = AsyncHTTPCache(default_ttl_s=60)
        result = await cache.get("missing_key")
        assert result is None

    @pytest.mark.asyncio
    async def test_set_then_get_returns_value(self):
        cache = AsyncHTTPCache(default_ttl_s=60)
        await cache.set("key1", {"data": 42})
        result = await cache.get("key1")
        assert result == {"data": 42}

    @pytest.mark.asyncio
    async def test_expired_entry_returns_none(self):
        cache = AsyncHTTPCache(default_ttl_s=1)
        await cache.set("key1", "value", ttl_s=0)  # immediately expired
        # Force expire by setting the stored time in the past
        cache._store["key1"] = ("value", time.monotonic() - 1.0)
        result = await cache.get("key1")
        assert result is None

    @pytest.mark.asyncio
    async def test_multiple_keys_independent(self):
        cache = AsyncHTTPCache(default_ttl_s=60)
        await cache.set("k1", 1)
        await cache.set("k2", 2)
        assert await cache.get("k1") == 1
        assert await cache.get("k2") == 2

    @pytest.mark.asyncio
    async def test_overwrite_key(self):
        cache = AsyncHTTPCache(default_ttl_s=60)
        await cache.set("k", "old")
        await cache.set("k", "new")
        assert await cache.get("k") == "new"

    @pytest.mark.asyncio
    async def test_custom_ttl(self):
        cache = AsyncHTTPCache(default_ttl_s=1)
        await cache.set("k", "val", ttl_s=3600)
        result = await cache.get("k")
        assert result == "val"


# ---------------------------------------------------------------------------
# CircuitBreaker
# ---------------------------------------------------------------------------


class TestCircuitBreaker:
    @pytest.mark.asyncio
    async def test_closed_state_passes_calls(self):
        cb = CircuitBreaker(failure_threshold=3)

        async def _ok():
            return "ok"

        result = await cb.call(_ok)
        assert result == "ok"

    @pytest.mark.asyncio
    async def test_opens_after_threshold_failures(self):
        cb = CircuitBreaker(failure_threshold=2, cooldown_s=300)

        async def _fail():
            raise RuntimeError("fail")

        for _ in range(2):
            with pytest.raises(RuntimeError):
                await cb.call(_fail)

        with pytest.raises(CircuitOpenError):
            await cb.call(_fail)

    @pytest.mark.asyncio
    async def test_half_open_recovers_on_success(self):
        cb = CircuitBreaker(failure_threshold=1, cooldown_s=0.01)

        async def _fail():
            raise RuntimeError("fail")

        with pytest.raises(RuntimeError):
            await cb.call(_fail)

        # Wait for cooldown
        await asyncio.sleep(0.02)

        # Now it should be HALF_OPEN, success closes it
        async def _ok():
            return "recovered"

        result = await cb.call(_ok)
        assert result == "recovered"

    @pytest.mark.asyncio
    async def test_circuit_open_immediately_raises(self):
        cb = CircuitBreaker(failure_threshold=1, cooldown_s=300)

        async def _fail():
            raise RuntimeError("fail")

        with pytest.raises(RuntimeError):
            await cb.call(_fail)

        # Circuit is open - immediate raise
        with pytest.raises(CircuitOpenError):
            await cb.call(_fail)

    @pytest.mark.asyncio
    async def test_half_open_failure_reopens(self):
        cb = CircuitBreaker(failure_threshold=1, cooldown_s=0.01)

        async def _fail():
            raise RuntimeError("fail")

        with pytest.raises(RuntimeError):
            await cb.call(_fail)

        await asyncio.sleep(0.02)

        # HALF_OPEN, but fails again → back to OPEN
        with pytest.raises(RuntimeError):
            await cb.call(_fail)

        with pytest.raises(CircuitOpenError):
            await cb.call(_fail)


# ---------------------------------------------------------------------------
# RateLimiter
# ---------------------------------------------------------------------------


class TestRateLimiter:
    def test_init_invalid_rate_raises(self):
        with pytest.raises(ValueError, match="rate must be > 0"):
            RateLimiter(rate=0)

    def test_init_negative_rate_raises(self):
        with pytest.raises(ValueError):
            RateLimiter(rate=-1)

    @pytest.mark.asyncio
    async def test_acquire_consumes_token(self):
        limiter = RateLimiter(rate=10.0)
        await limiter.acquire()  # should not block
        assert limiter._tokens < 10.0

    @pytest.mark.asyncio
    async def test_acquire_multiple_times(self):
        limiter = RateLimiter(rate=5.0)
        for _ in range(3):
            await limiter.acquire()

    @pytest.mark.asyncio
    async def test_acquire_sleeps_when_no_tokens(self):
        limiter = RateLimiter(rate=1.0)
        # Drain tokens
        limiter._tokens = 0.0
        limiter._last_refill = time.monotonic()

        with patch("asyncio.sleep", new=AsyncMock()) as mock_sleep:
            await limiter.acquire()
            mock_sleep.assert_awaited_once()


# ---------------------------------------------------------------------------
# OnChainProvider (via concrete subclass)
# ---------------------------------------------------------------------------


class ConcreteProvider(OnChainProvider):
    _CACHE_TTL_S = 60
    _RATE = 5.0

    @property
    def exchange_id(self) -> str:
        return "test_exchange"

    async def initialize(self) -> None:
        pass

    async def close(self) -> None:
        await super().close()

    async def fetch_metrics(self) -> dict:
        return {}


class TestOnChainProvider:
    @pytest.mark.asyncio
    async def test_ensure_session_creates_session(self):
        provider = ConcreteProvider()
        session = await provider._ensure_session()
        assert session is not None
        await session.close()

    @pytest.mark.asyncio
    async def test_ensure_session_reuses_open_session(self):
        provider = ConcreteProvider()
        s1 = await provider._ensure_session()
        s2 = await provider._ensure_session()
        assert s1 is s2
        await s1.close()

    @pytest.mark.asyncio
    async def test_ensure_session_recreates_closed_session(self):
        provider = ConcreteProvider()
        s1 = await provider._ensure_session()
        await s1.close()
        s2 = await provider._ensure_session()
        assert s2 is not s1
        await s2.close()

    @pytest.mark.asyncio
    async def test_get_cache_hit_skips_http(self):
        provider = ConcreteProvider()
        cached_data = {"result": "cached"}
        await provider._cache.set("GET:http://test:[]", cached_data)

        # _get should return cached without making HTTP call
        result = await provider._get("http://test", params={})
        # The key format is f"GET:{url}:{sorted(params.items())}"
        # With params={} → sorted({}.items()) = []
        assert result == cached_data

    @pytest.mark.asyncio
    async def test_get_circuit_open_returns_none(self):
        provider = ConcreteProvider()
        # Force circuit open
        provider._breaker._state = provider._breaker._state.__class__["OPEN"]
        from src.intelligence.onchain.base import _CBState

        provider._breaker._state = _CBState.OPEN
        provider._breaker._opened_at = time.monotonic()  # recent open

        result = await provider._get("http://test")
        assert result is None

    @pytest.mark.asyncio
    async def test_get_http_error_returns_none(self):
        provider = ConcreteProvider()

        # Patch the session to raise
        mock_session = AsyncMock()
        mock_resp = AsyncMock()
        mock_resp.raise_for_status = MagicMock(side_effect=RuntimeError("500 error"))
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)
        mock_session.get = MagicMock(return_value=mock_resp)
        provider._session = mock_session

        result = await provider._get("http://test/endpoint")
        assert result is None

    @pytest.mark.asyncio
    async def test_post_circuit_open_returns_none(self):
        provider = ConcreteProvider()
        from src.intelligence.onchain.base import _CBState

        provider._breaker._state = _CBState.OPEN
        provider._breaker._opened_at = time.monotonic()

        result = await provider._post("http://test", json={"a": 1})
        assert result is None

    @pytest.mark.asyncio
    async def test_post_http_error_returns_none(self):
        provider = ConcreteProvider()

        mock_session = AsyncMock()
        mock_resp = AsyncMock()
        mock_resp.raise_for_status = MagicMock(side_effect=RuntimeError("error"))
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)
        mock_session.post = MagicMock(return_value=mock_resp)
        provider._session = mock_session

        result = await provider._post("http://test", json={})
        assert result is None

    @pytest.mark.asyncio
    async def test_close_closes_session(self):
        provider = ConcreteProvider()
        s = await provider._ensure_session()
        assert not s.closed
        await provider.close()
        assert s.closed
