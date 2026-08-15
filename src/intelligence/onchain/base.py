"""
On-chain provider foundation: RateLimiter, CircuitBreaker, AsyncHTTPCache, OnChainProvider ABC.

_get/_post never raise — failures log + return None for graceful degradation.

Authority:
  Token-bucket: Tanenbaum §5.3 leaky-bucket equivalence
  Circuit-breaker: Nygard "Release It!" §5 (threshold/cooldown/half-open)
  aiohttp: https://docs.aiohttp.org/en/stable/client.html
"""

from __future__ import annotations

import asyncio
import logging
import time
from abc import abstractmethod
from collections.abc import Awaitable, Callable
from enum import Enum, auto
from typing import Any

import aiohttp

from src.intelligence.providers.base import ExchangeIntelligenceProvider


logger = logging.getLogger(__name__)


class CircuitOpenError(Exception):
    """Raised when CircuitBreaker is in OPEN state."""


class RateLimiter:
    """Async token-bucket. rate=calls per window_s."""

    def __init__(self, rate: float, window_s: float = 1.0) -> None:
        if rate <= 0:
            raise ValueError(f"rate must be > 0, got {rate}")
        self._rate = rate
        self._window_s = window_s
        self._tokens: float = rate
        self._last_refill: float = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            now = time.monotonic()
            elapsed = now - self._last_refill
            self._tokens = min(
                self._rate,
                self._tokens + elapsed * (self._rate / self._window_s),
            )
            self._last_refill = now
            if self._tokens < 1.0:
                sleep_s = (1.0 - self._tokens) * (self._window_s / self._rate)
                await asyncio.sleep(sleep_s)
                self._tokens = 0.0
                # Advance the refill mark past the sleep. Without this,
                # _last_refill still points at the instant BEFORE the wait, so
                # the next acquire() computes elapsed across a window that has
                # already been spent accruing the token just consumed — and
                # credits it a second time.
                #
                # The effect is not subtle: the bucket runs at exactly twice
                # its configured rate once it starts throttling. Simulated at
                # rate=10/s, fifty throttled calls completed in 2.5s rather
                # than 5.0s. For an API limiter that means 429s and bans, and
                # exchange rate limits are a first-class constraint here.
                #
                # Set from the post-sleep instant rather than time.monotonic()
                # so the accounting matches the sleep exactly and cannot drift
                # with scheduler latency.
                self._last_refill = now + sleep_s
            else:
                self._tokens -= 1.0


class _CBState(Enum):
    CLOSED = auto()
    OPEN = auto()
    HALF_OPEN = auto()


class CircuitBreaker:
    """
    3-state circuit breaker.

    CLOSED -> OPEN after `failure_threshold` CONSECUTIVE failures,
    OPEN -> HALF_OPEN after `cooldown_s`, HALF_OPEN -> CLOSED on one success.

    Consecutive is the operative word. Counting cumulative failures instead
    means the counter only ever rises, so any provider that fails even
    occasionally trips eventually — a process running for months opens every
    breaker it owns regardless of how healthy the providers are, and then
    spends the rest of its life cycling through the cooldown.
    """

    def __init__(self, failure_threshold: int = 3, cooldown_s: float = 300.0) -> None:
        self._threshold = failure_threshold
        self._cooldown = cooldown_s
        self._failures = 0
        self._state = _CBState.CLOSED
        self._opened_at: float = 0.0

    async def call(self, coro_factory: Callable[[], Awaitable[Any]]) -> Any:
        if self._state == _CBState.OPEN:
            if time.monotonic() - self._opened_at >= self._cooldown:
                self._state = _CBState.HALF_OPEN
            else:
                raise CircuitOpenError("Circuit is OPEN")
        try:
            result = await coro_factory()
            if self._state == _CBState.HALF_OPEN:
                self._failures = 0
                self._state = _CBState.CLOSED
            else:
                # A success while CLOSED clears the run. Without this the
                # counter is cumulative rather than consecutive: it was only
                # ever reset on the HALF_OPEN recovery path, so a provider
                # failing once per 500 successes still reached the threshold
                # and opened — measured, at exactly the third failure,
                # 1503 calls in.
                self._failures = 0
            return result
        except Exception:
            self._failures += 1
            if self._state == _CBState.HALF_OPEN or self._failures >= self._threshold:
                self._state = _CBState.OPEN
                self._opened_at = time.monotonic()
            raise


class AsyncHTTPCache:
    """In-memory async cache with per-key TTL. Thread-safe via per-key asyncio.Lock."""

    def __init__(self, default_ttl_s: int) -> None:
        self._default_ttl = default_ttl_s
        self._store: dict[str, tuple[Any, float]] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._meta_lock = asyncio.Lock()

    async def _key_lock(self, key: str) -> asyncio.Lock:
        async with self._meta_lock:
            if key not in self._locks:
                self._locks[key] = asyncio.Lock()
            return self._locks[key]

    async def get(self, key: str) -> Any | None:
        lock = await self._key_lock(key)
        async with lock:
            entry = self._store.get(key)
            if entry is None:
                return None
            value, expires_at = entry
            if time.monotonic() > expires_at:
                del self._store[key]
                return None
            return value

    async def set(self, key: str, value: Any, ttl_s: int | None = None) -> None:
        ttl = ttl_s if ttl_s is not None else self._default_ttl
        lock = await self._key_lock(key)
        async with lock:
            self._store[key] = (value, time.monotonic() + ttl)


# ---------------------------------------------------------------------------
# OnChainProvider ABC
# ---------------------------------------------------------------------------


class OnChainProvider(ExchangeIntelligenceProvider):
    """
    Abstract base for on-chain data providers (Arkham, Dune, DeFiLlama, Coinglass).

    Provides:
      _cache    : AsyncHTTPCache  — shared response cache
      _limiter  : RateLimiter     — provider-specific rate cap
      _breaker  : CircuitBreaker  — fault isolation
      _get()    : rate-limited, cached, circuit-broken HTTP GET → dict | None
      _post()   : same for POST (Dune execute endpoint)

    Subclasses must set:
      _BASE_URL : str
      _CACHE_TTL_S : int
      _RATE     : float  (calls/s)

    Subclasses must implement: exchange_id, initialize(), close(), fetch_metrics().
    """

    _BASE_URL: str = ""
    _CACHE_TTL_S: int = 60
    _RATE: float = 1.0

    def __init__(self) -> None:
        self._async_cache: AsyncHTTPCache = AsyncHTTPCache(default_ttl_s=self._CACHE_TTL_S)
        self._limiter = RateLimiter(rate=self._RATE)
        self._breaker = CircuitBreaker()
        self._session: aiohttp.ClientSession | None = None

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=15),
                connector=aiohttp.TCPConnector(limit=10),
            )
        return self._session

    async def _get(
        self,
        url: str,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        cache_key = f"GET:{url}:{sorted((params or {}).items())}"
        cached = await self._async_cache.get(cache_key)
        if cached is not None:
            return cached
        await self._limiter.acquire()
        try:

            async def _do() -> dict[str, Any]:
                session = await self._ensure_session()
                async with session.get(url, headers=headers, params=params) as resp:
                    resp.raise_for_status()
                    return await resp.json(content_type=None)

            data = await self._breaker.call(_do)
            await self._async_cache.set(cache_key, data)
            return data
        except CircuitOpenError:
            logger.warning("%s._get circuit OPEN — skipping %s", self.__class__.__name__, url)
            return None
        except Exception as exc:
            logger.warning(
                "%s._get failed url=%s err=%s", self.__class__.__name__, url, exc, exc_info=True
            )
            return None

    async def _post(
        self,
        url: str,
        headers: dict[str, str] | None = None,
        json: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        await self._limiter.acquire()
        try:

            async def _do() -> dict[str, Any]:
                session = await self._ensure_session()
                async with session.post(url, headers=headers, json=json) as resp:
                    resp.raise_for_status()
                    return await resp.json(content_type=None)

            return await self._breaker.call(_do)
        except CircuitOpenError:
            logger.warning("%s._post circuit OPEN — skipping %s", self.__class__.__name__, url)
            return None
        except Exception as exc:
            logger.warning(
                "%s._post failed url=%s err=%s", self.__class__.__name__, url, exc, exc_info=True
            )
            return None

    @property
    @abstractmethod
    def exchange_id(self) -> str: ...

    @abstractmethod
    async def initialize(self) -> None: ...

    @abstractmethod
    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    @abstractmethod
    async def fetch_metrics(self) -> dict[str, float]: ...
