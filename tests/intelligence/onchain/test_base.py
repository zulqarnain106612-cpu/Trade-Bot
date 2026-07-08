"""
OCI-001: Unit tests — RateLimiter, CircuitBreaker, AsyncHTTPCache.
"""
from __future__ import annotations

import asyncio
import time

import pytest

from src.intelligence.onchain.base import (
    AsyncHTTPCache,
    CircuitBreaker,
    CircuitOpenError,
    RateLimiter,
)


# ---------------------------------------------------------------------------
# RateLimiter
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_rate_limiter_allows_within_limit() -> None:
    limiter = RateLimiter(rate=10.0, window_s=1.0)
    t0 = time.monotonic()
    await limiter.acquire()
    assert time.monotonic() - t0 < 0.05


@pytest.mark.asyncio
async def test_rate_limiter_blocks_excess_calls() -> None:
    limiter = RateLimiter(rate=2.0, window_s=1.0)
    await limiter.acquire()
    await limiter.acquire()
    t0 = time.monotonic()
    await limiter.acquire()
    assert time.monotonic() - t0 >= 0.4


@pytest.mark.asyncio
async def test_rate_limiter_rejects_non_positive_rate() -> None:
    with pytest.raises(ValueError):
        RateLimiter(rate=0)


# ---------------------------------------------------------------------------
# CircuitBreaker
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_circuit_breaker_opens_after_threshold() -> None:
    cb = CircuitBreaker(failure_threshold=3, cooldown_s=999.0)

    async def failing() -> None:
        raise RuntimeError("fail")

    for _ in range(3):
        with pytest.raises(RuntimeError):
            await cb.call(failing())

    coro = failing()
    with pytest.raises(CircuitOpenError):
        await cb.call(coro)
    coro.close()


@pytest.mark.asyncio
async def test_circuit_breaker_half_open_on_cooldown_expiry() -> None:
    cb = CircuitBreaker(failure_threshold=1, cooldown_s=0.05)

    async def failing() -> None:
        raise RuntimeError("fail")

    with pytest.raises(RuntimeError):
        await cb.call(failing())

    # Circuit is OPEN — close coroutine to suppress RuntimeWarning
    coro = failing()
    with pytest.raises(CircuitOpenError):
        await cb.call(coro)
    coro.close()

    # Wait for cooldown
    await asyncio.sleep(0.1)

    # Now in HALF_OPEN — next call executes (fails again → reopens)
    with pytest.raises(RuntimeError):
        await cb.call(failing())


@pytest.mark.asyncio
async def test_circuit_breaker_closes_on_success_in_half_open() -> None:
    cb = CircuitBreaker(failure_threshold=1, cooldown_s=0.05)

    async def failing() -> None:
        raise RuntimeError("fail")

    async def succeeding() -> str:
        return "ok"

    with pytest.raises(RuntimeError):
        await cb.call(failing())

    await asyncio.sleep(0.1)  # cooldown → HALF_OPEN

    result = await cb.call(succeeding())
    assert result == "ok"

    # Should be CLOSED now — another success works without CircuitOpenError
    result2 = await cb.call(succeeding())
    assert result2 == "ok"


# ---------------------------------------------------------------------------
# AsyncHTTPCache
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_async_http_cache_hit_miss_expiry() -> None:
    cache = AsyncHTTPCache(default_ttl_s=1)

    # Miss
    assert await cache.get("k") is None

    # Set + hit
    await cache.set("k", {"v": 42})
    assert await cache.get("k") == {"v": 42}

    # Expiry
    await cache.set("exp", "soon", ttl_s=0)
    await asyncio.sleep(0.01)
    assert await cache.get("exp") is None


@pytest.mark.asyncio
async def test_cache_concurrent_access_no_race() -> None:
    cache = AsyncHTTPCache(default_ttl_s=60)
    results: list[str] = []

    async def writer(i: int) -> None:
        await cache.set(f"key-{i}", i)
        val = await cache.get(f"key-{i}")
        results.append(str(val))

    await asyncio.gather(*[writer(i) for i in range(20)])
    assert len(results) == 20
    assert all(r is not None for r in results)
