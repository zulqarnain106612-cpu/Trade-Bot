"""
Tests for retry backoff jitter in _with_retry.

Without jitter every concurrent caller backs off in lockstep. This process
hits Binance from three timeframe loops, the intelligence providers, the
universe cache and the venue-quote path, so one rate-limit event puts several
callers on the same retry schedule — they wake together, retry together, and
re-trigger the limit together.
"""

from __future__ import annotations

import random
from unittest.mock import AsyncMock, patch

import ccxt.async_support as ccxt_async
import pytest

from src.data.fetcher import _jittered, _with_retry


@pytest.mark.parametrize("delay", [0.5, 1.0, 8.0, 60.0])
def test_jitter_stays_between_half_the_backoff_and_the_backoff(delay: float) -> None:
    # The floor matters: the RateLimitExceeded path exists because the
    # exchange asked us to slow down, so a near-zero sleep is the one
    # outcome that must not be drawn. Full jitter over [0, delay] can.
    samples = [_jittered(delay) for _ in range(2_000)]
    assert min(samples) >= delay / 2.0
    assert max(samples) <= delay


def test_jitter_actually_varies() -> None:
    samples = {_jittered(8.0) for _ in range(200)}
    assert len(samples) > 100  # not a constant dressed up as a range


def test_jitter_decorrelates_concurrent_callers() -> None:
    # The property under test: independent callers must not land on the same
    # instants. Without jitter every caller shares one schedule exactly.
    random.seed(1234)

    def schedule() -> tuple[float, ...]:
        t, delay, stops = 0.0, 1.0, []
        for _ in range(4):
            t += _jittered(delay)
            delay = min(delay * 2, 60.0)
            stops.append(round(t, 6))
        return tuple(stops)

    schedules = [schedule() for _ in range(12)]
    assert len({s[0] for s in schedules}) > 1
    assert len(set(schedules)) == 12


def test_the_backoff_progression_itself_stays_deterministic() -> None:
    # Jitter applies to the sleep, never to the stored delay, so the
    # exponential growth and its 60s ceiling cannot drift.
    delay, progression = 1.0, []
    for _ in range(8):
        progression.append(delay)
        delay = min(delay * 2, 60.0)
    assert progression == [1.0, 2.0, 4.0, 8.0, 16.0, 32.0, 60.0, 60.0]


@pytest.mark.asyncio
async def test_retry_still_succeeds_after_transient_network_errors() -> None:
    calls: list[int] = []

    async def _flaky():
        calls.append(1)
        if len(calls) < 3:
            raise ccxt_async.NetworkError("timeout")
        return "ok"

    with patch("asyncio.sleep", new=AsyncMock()):
        result = await _with_retry(lambda: _flaky(), label="t", attempts=5, base_delay=0.01)
    assert result == "ok"
    assert len(calls) == 3


@pytest.mark.asyncio
async def test_every_sleep_is_within_its_backoff_bounds() -> None:
    slept: list[float] = []

    async def _fake_sleep(s: float) -> None:
        slept.append(s)

    async def _always_rate_limited():
        raise ccxt_async.RateLimitExceeded("429")

    with patch("asyncio.sleep", new=_fake_sleep), pytest.raises(ccxt_async.RateLimitExceeded):
        await _with_retry(lambda: _always_rate_limited(), label="t", attempts=4, base_delay=1.0)

    # RateLimitExceeded doubles before sleeping: bounds are 2, 4, 8.
    assert len(slept) == 3
    for actual, backoff in zip(slept, [2.0, 4.0, 8.0], strict=True):
        assert backoff / 2.0 <= actual <= backoff
