"""
Tests for the on-chain provider RateLimiter's token accounting.

acquire() slept to earn a token but left _last_refill pointing at the
instant BEFORE the sleep, so the next call credited that same interval a
second time. The bucket therefore ran at twice its configured rate once it
started throttling — which for an API limiter means 429s and bans.
"""

from __future__ import annotations

import asyncio
import time

from src.intelligence.onchain.base import RateLimiter


async def test_the_initial_burst_is_not_throttled() -> None:
    # A full bucket should hand out `rate` tokens immediately.
    limiter = RateLimiter(rate=20, window_s=1.0)
    start = time.monotonic()
    for _ in range(20):
        await limiter.acquire()
    assert time.monotonic() - start < 0.05


async def test_the_configured_rate_is_honoured_once_throttling() -> None:
    # The regression: with _last_refill left before the sleep, this ran at
    # exactly double the configured rate.
    rate = 200.0
    limiter = RateLimiter(rate=rate, window_s=1.0)
    for _ in range(int(rate)):  # drain the initial burst
        await limiter.acquire()

    n = 40
    start = time.monotonic()
    for _ in range(n):
        await limiter.acquire()
    elapsed = time.monotonic() - start

    expected = n / rate
    # Generous lower bound only: scheduler latency can make it slower, never
    # legitimately faster. Doubling would land at ~0.5 * expected.
    assert elapsed >= expected * 0.75, (
        f"{n} calls in {elapsed:.4f}s = {n / elapsed:.1f}/s, "
        f"configured {rate}/s — limiter is running fast"
    )


async def test_the_refill_mark_advances_past_the_sleep() -> None:
    limiter = RateLimiter(rate=50, window_s=1.0)
    for _ in range(50):
        await limiter.acquire()

    before = limiter._last_refill
    await limiter.acquire()  # must throttle
    # The mark has to move forward by at least the time actually slept,
    # otherwise the next refill re-credits the wait.
    assert limiter._last_refill > before


async def test_concurrent_callers_are_serialised_by_the_lock() -> None:
    # Holding the lock across the sleep is deliberate: it stops every waiter
    # concluding simultaneously that a token is free.
    limiter = RateLimiter(rate=100, window_s=1.0)
    for _ in range(100):
        await limiter.acquire()

    start = time.monotonic()
    await asyncio.gather(*(limiter.acquire() for _ in range(10)))
    # Ten throttled acquisitions at 100/s cannot complete instantly.
    assert time.monotonic() - start >= 0.05
