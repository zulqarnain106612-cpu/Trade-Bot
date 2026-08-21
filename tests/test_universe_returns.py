"""
Tests for the universe trailing-returns cache.

Two properties carry the design and are easy to lose later: the TTL (without
it, wiring the cross-sectional family multiplies this bot's exchange request
rate by the universe size), and degrading per symbol rather than per universe
(one delisted symbol must not blank the cross-section).
"""

from __future__ import annotations

import time

import pytest

from src.config import Timeframe
from src.engine.universe_returns import UniverseReturnsCache


class _Bar:
    def __init__(self, ts: int, close: float) -> None:
        self.ts = ts
        self.close = close


class _Fetcher:
    """Returns a canned bar series per symbol, or raises for that symbol."""

    def __init__(self, series: dict[str, object]) -> None:
        self._series = series
        self.calls: list[str] = []

    async def fetch_ohlcv_okx(
        self,
        symbol: str,
        timeframe: Timeframe,
        since_ms: int,
        limit: int = 100,
    ) -> list[_Bar]:
        self.calls.append(symbol)
        value = self._series.get(symbol)
        if isinstance(value, Exception):
            raise value
        if value is None:
            return []
        return list(value)  # type: ignore[arg-type]


def _rising(start: float, end: float) -> list[_Bar]:
    return [_Bar(1_000, start), _Bar(2_000, end)]


def _cache(fetcher: _Fetcher, symbols: tuple[str, ...], **kw: object) -> UniverseReturnsCache:
    return UniverseReturnsCache(fetcher, symbols, **kw)  # type: ignore[arg-type]


# ------------------------------------------------------------- construction


@pytest.mark.parametrize(
    "kwargs",
    [{"lookback_days": 0}, {"ttl_seconds": 0.0}, {"max_concurrency": 0}],
)
def test_rejects_degenerate_configuration(kwargs: dict) -> None:
    with pytest.raises(ValueError):
        _cache(_Fetcher({}), ("A",), **kwargs)


async def test_empty_universe_never_touches_the_exchange() -> None:
    # An unconfigured universe must cost nothing: the family abstains and no
    # request is made on any tick.
    fetcher = _Fetcher({})
    cache = _cache(fetcher, ())
    assert await cache.trailing_returns() == {}
    assert fetcher.calls == []


# ------------------------------------------------------------- computation


async def test_computes_trailing_return_per_symbol() -> None:
    fetcher = _Fetcher({"A": _rising(100.0, 110.0), "B": _rising(100.0, 90.0)})
    returns = await _cache(fetcher, ("A", "B")).trailing_returns()
    assert returns["A"] == pytest.approx(0.10)
    assert returns["B"] == pytest.approx(-0.10)


async def test_bars_are_ordered_before_differencing() -> None:
    # Exchange ordering is not guaranteed; a reversed page would flip the
    # sign of every return in the cross-section.
    fetcher = _Fetcher({"A": [_Bar(2_000, 110.0), _Bar(1_000, 100.0)]})
    returns = await _cache(fetcher, ("A",)).trailing_returns()
    assert returns["A"] == pytest.approx(0.10)


# ------------------------------------------------------------- degradation


async def test_one_failing_symbol_drops_out_without_blanking_the_universe() -> None:
    fetcher = _Fetcher({"A": _rising(100.0, 110.0), "B": RuntimeError("delisted")})
    returns = await _cache(fetcher, ("A", "B")).trailing_returns()
    assert set(returns) == {"A"}


async def test_symbol_with_too_few_bars_is_absent_not_zero() -> None:
    # A zero would rank the symbol mid-universe — exactly where a decile
    # strategy will never notice it was never measured.
    fetcher = _Fetcher({"A": _rising(100.0, 110.0), "B": [_Bar(1_000, 100.0)]})
    returns = await _cache(fetcher, ("A", "B")).trailing_returns()
    assert "B" not in returns


async def test_non_positive_first_close_is_treated_as_corrupt() -> None:
    fetcher = _Fetcher({"A": [_Bar(1_000, 0.0), _Bar(2_000, 110.0)]})
    assert await _cache(fetcher, ("A",)).trailing_returns() == {}


async def test_total_outage_retains_the_previous_snapshot() -> None:
    # Freshness is the right thing to lose in an outage; the cross-section
    # is not. An empty universe returns the family to being inert.
    series: dict[str, object] = {"A": _rising(100.0, 110.0)}
    fetcher = _Fetcher(series)
    cache = _cache(fetcher, ("A",), ttl_seconds=0.01)
    assert await cache.trailing_returns() == {"A": pytest.approx(0.10)}

    series["A"] = RuntimeError("exchange down")
    cache._fetched_at = 0.0  # force the TTL to have expired
    assert await cache.trailing_returns() == {"A": pytest.approx(0.10)}


# ------------------------------------------------------------------- TTL


async def test_second_call_within_ttl_does_not_refetch() -> None:
    # Without this the cross-sectional family multiplies the bot's request
    # rate by the universe size, per tick, for daily-moving information.
    fetcher = _Fetcher({"A": _rising(100.0, 110.0), "B": _rising(100.0, 105.0)})
    cache = _cache(fetcher, ("A", "B"), ttl_seconds=3600.0)
    await cache.trailing_returns()
    await cache.trailing_returns()
    await cache.trailing_returns()
    assert sorted(fetcher.calls) == ["A", "B"]


async def test_expired_ttl_refetches() -> None:
    fetcher = _Fetcher({"A": _rising(100.0, 110.0)})
    cache = _cache(fetcher, ("A",), ttl_seconds=3600.0)
    await cache.trailing_returns()
    # Stamped on the monotonic clock, whose origin is arbitrary — 0.0 is not
    # reliably more than one TTL in the past.
    cache._fetched_at = time.monotonic() - 7200.0
    await cache.trailing_returns()
    assert fetcher.calls == ["A", "A"]


async def test_concurrent_callers_share_one_refresh() -> None:
    import asyncio

    fetcher = _Fetcher({"A": _rising(100.0, 110.0), "B": _rising(100.0, 105.0)})
    cache = _cache(fetcher, ("A", "B"), ttl_seconds=3600.0)
    await asyncio.gather(*(cache.trailing_returns() for _ in range(5)))
    # Five concurrent ticks must not become five request bursts.
    assert sorted(fetcher.calls) == ["A", "B"]


async def test_snapshot_and_staleness_are_reported() -> None:
    fetcher = _Fetcher({"A": _rising(100.0, 110.0)})
    cache = _cache(fetcher, ("A",), ttl_seconds=3600.0)
    assert cache.snapshot() == {}
    assert cache.is_stale() is True
    await cache.trailing_returns()
    assert cache.snapshot() == {"A": pytest.approx(0.10)}
    assert cache.is_stale() is False
    assert cache.fetched_at > 0.0
    assert cache.symbols == ("A",)


# ------------------------------------------------------------ failure backoff


async def test_a_failing_refresh_does_not_retry_on_every_call() -> None:
    # The defect: a failed refresh never advances _fetched_at, so is_stale()
    # stays True and every tick re-fires the whole universe — the heaviest
    # request pattern possible, aimed at an exchange that has just shown it
    # is unhealthy.
    fetcher = _Fetcher({"A": RuntimeError("503"), "B": RuntimeError("503")})
    cache = _cache(fetcher, ("A", "B"), ttl_seconds=0.01)

    for _ in range(5):
        assert await cache.trailing_returns() == {}

    # One attempt, not five.
    assert sorted(fetcher.calls) == ["A", "B"]


async def test_backoff_grows_with_consecutive_failures() -> None:
    fetcher = _Fetcher({"A": RuntimeError("503")})
    cache = _cache(fetcher, ("A",), ttl_seconds=0.01)

    await cache.trailing_returns()
    first = cache._backoff_seconds()
    # Force the backoff window open and fail again.
    cache._last_attempt_at -= first + 1.0
    await cache.trailing_returns()

    assert cache._backoff_seconds() > first


async def test_backoff_is_capped() -> None:
    cache = _cache(_Fetcher({}), ("A",))
    cache._consecutive_failures = 100
    assert cache._backoff_seconds() <= 900.0


async def test_a_success_clears_the_backoff() -> None:
    series: dict[str, object] = {"A": RuntimeError("503")}
    fetcher = _Fetcher(series)
    cache = _cache(fetcher, ("A",), ttl_seconds=0.01)

    await cache.trailing_returns()
    assert cache._consecutive_failures == 1

    series["A"] = _rising(100.0, 110.0)
    cache._last_attempt_at -= 10_000.0
    assert await cache.trailing_returns() == {"A": pytest.approx(0.10)}
    assert cache._consecutive_failures == 0
    assert cache._may_attempt() is True


async def test_stale_data_is_still_served_while_in_backoff() -> None:
    # Backoff must not cost the cross-section, only freshness. An empty
    # universe would return the family to the inert state this module ends.
    series: dict[str, object] = {"A": _rising(100.0, 110.0)}
    fetcher = _Fetcher(series)
    cache = _cache(fetcher, ("A",), ttl_seconds=0.01)
    assert await cache.trailing_returns() == {"A": pytest.approx(0.10)}

    series["A"] = RuntimeError("503")
    cache._fetched_at = 0.0
    await cache.trailing_returns()  # fails, enters backoff

    for _ in range(3):
        assert await cache.trailing_returns() == {"A": pytest.approx(0.10)}


async def test_staleness_and_attempt_permission_are_independent() -> None:
    # is_stale() asks how old the DATA is; _may_attempt() asks whether a
    # refresh is allowed. Conflating them is what produced the retry storm.
    cache = _cache(_Fetcher({}), ("A",), ttl_seconds=0.01)
    cache._consecutive_failures = 1
    cache._last_attempt_at = time.time()

    assert cache.is_stale() is True
    assert cache._may_attempt() is False


# ------------------------------------------------------------ monotonic clock


async def test_a_never_fetched_cache_is_stale_regardless_of_clock_origin() -> None:
    # The TTL runs on time.monotonic(), whose epoch is arbitrary and on a
    # freshly booted host can be far below the TTL itself. A 0.0 "never"
    # sentinel would then make an unfetched cache read as FRESH and never
    # populate — the sentinel has to be explicit, not rely on the origin.
    cache = _cache(_Fetcher({}), ("A",), ttl_seconds=3600.0)
    assert cache.is_stale() is True
    assert cache.fetched_at == 0.0


async def test_first_refresh_happens_even_with_a_long_ttl() -> None:
    fetcher = _Fetcher({"A": _rising(100.0, 110.0)})
    cache = _cache(fetcher, ("A",), ttl_seconds=86_400.0)
    assert await cache.trailing_returns() == {"A": pytest.approx(0.10)}
    assert fetcher.calls == ["A"]


async def test_backoff_permits_the_first_attempt() -> None:
    # _last_attempt_at starts unset; with no failures recorded the gate must
    # be open rather than comparing against a sentinel.
    cache = _cache(_Fetcher({}), ("A",))
    assert cache._may_attempt() is True
