"""
Order throttler — token-bucket rate limiter for outgoing exchange orders.

Prevents hitting ccxt/exchange API rate limits under burst conditions by
enforcing per-exchange order-rate caps using a token-bucket algorithm.

Token bucket model (Tanenbaum & Wetherall 2011 §5.3):
  - Bucket holds up to ``burst`` tokens.
  - Tokens refill at ``rate`` tokens/second.
  - Each order attempt consumes 1 token.
  - If no tokens are available, the request is rejected (non-blocking) or
    the caller is told the wait time.

Usage
-----
    throttler = OrderThrottler(rate=10.0, burst=20)  # 10 orders/s, burst 20
    result = throttler.acquire("binance")
    if result.allowed:
        # place order
        ...
    else:
        log.warning("throttled", wait_s=result.wait_s)

Multiple exchanges can share one throttler (per-exchange buckets) or each
have their own instance.

Authority:
  Tanenbaum & Wetherall (2011) Computer Networks §5.3 — token bucket.
  Binance API docs (2024) — 1200 weight/min limit ≈ 20 req/s burst.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Final

import structlog

log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

_DEFAULT_RATE: Final[float] = 10.0  # tokens per second
_DEFAULT_BURST: Final[int] = 20  # maximum burst
_EPS: Final[float] = 1e-9


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ThrottleResult:
    """Result of a throttle acquire() call."""

    allowed: bool
    exchange: str
    tokens_remaining: float  # tokens in bucket after this call
    wait_s: float  # seconds to wait until 1 token available (0 if allowed)
    reject_reason: str  # empty when allowed


# ---------------------------------------------------------------------------
# Per-exchange token bucket
# ---------------------------------------------------------------------------


class _Bucket:
    """Single token-bucket for one exchange."""

    __slots__ = ("_burst", "_last_refill", "_rate", "_tokens")

    def __init__(self, rate: float, burst: int) -> None:
        self._rate = rate
        self._burst = float(burst)
        self._tokens = float(burst)  # start full
        self._last_refill = time.monotonic()

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last_refill
        self._tokens = min(self._burst, self._tokens + elapsed * self._rate)
        self._last_refill = now

    def try_acquire(self) -> tuple[bool, float, float]:
        """
        Try to consume 1 token.

        Returns (allowed, tokens_remaining, wait_s).
        """
        self._refill()
        if self._tokens >= 1.0:
            self._tokens -= 1.0
            return True, self._tokens, 0.0
        # Compute wait time until 1 token is available
        wait_s = (1.0 - self._tokens) / max(self._rate, _EPS)
        return False, self._tokens, wait_s

    @property
    def tokens(self) -> float:
        self._refill()
        return self._tokens

    def reset(self) -> None:
        self._tokens = self._burst
        self._last_refill = time.monotonic()


# ---------------------------------------------------------------------------
# Main throttler
# ---------------------------------------------------------------------------


class OrderThrottler:
    """
    Per-exchange token-bucket order rate limiter.

    Parameters
    ----------
    rate:
        Sustained token refill rate (orders per second). Default 10/s.
    burst:
        Maximum burst capacity (tokens in full bucket). Default 20.
    """

    def __init__(
        self,
        rate: float = _DEFAULT_RATE,
        burst: int = _DEFAULT_BURST,
    ) -> None:
        if rate <= 0:
            raise ValueError(f"rate must be positive, got {rate}")
        if burst < 1:
            raise ValueError(f"burst must be >= 1, got {burst}")
        self._rate = rate
        self._burst = burst
        self._buckets: dict[str, _Bucket] = {}
        # The throttler is shared across the asyncio event loop *and* any
        # thread-pool executor that places orders, so bucket creation and
        # token consumption must be atomic — two concurrent acquire() calls
        # that both read _tokens == 1.0 would otherwise both be allowed and
        # overdraw the bucket, which is exactly the burst the exchange bans.
        self._lock: threading.Lock = threading.Lock()

    def _get_bucket(self, exchange: str) -> _Bucket:
        """Caller must hold ``self._lock``."""
        if exchange not in self._buckets:
            self._buckets[exchange] = _Bucket(self._rate, self._burst)
        return self._buckets[exchange]

    def acquire(self, exchange: str = "default") -> ThrottleResult:
        """
        Try to acquire a token for one order on the given exchange.

        Non-blocking: returns immediately with allowed=False and a wait_s
        estimate if the bucket is empty.
        """
        with self._lock:
            allowed, remaining, wait_s = self._get_bucket(exchange).try_acquire()

        if not allowed:
            log.warning(
                "order_throttler.rejected",
                exchange=exchange,
                tokens_remaining=round(remaining, 3),
                wait_s=round(wait_s, 3),
            )
            return ThrottleResult(
                allowed=False,
                exchange=exchange,
                tokens_remaining=remaining,
                wait_s=wait_s,
                reject_reason=f"rate_limit: wait {wait_s:.3f}s for next token",
            )

        log.debug(
            "order_throttler.acquired",
            exchange=exchange,
            tokens_remaining=round(remaining, 2),
        )
        return ThrottleResult(
            allowed=True,
            exchange=exchange,
            tokens_remaining=remaining,
            wait_s=0.0,
            reject_reason="",
        )

    def tokens_remaining(self, exchange: str = "default") -> float:
        """Current token count for the given exchange (without consuming)."""
        with self._lock:
            return self._get_bucket(exchange).tokens

    def reset(self, exchange: str | None = None) -> None:
        """
        Reset bucket(s) to full.

        Parameters
        ----------
        exchange:
            If given, resets only that exchange's bucket. If None, resets all.
        """
        with self._lock:
            if exchange is not None:
                if exchange in self._buckets:
                    self._buckets[exchange].reset()
            else:
                for b in self._buckets.values():
                    b.reset()

    def set_rate(self, rate: float, burst: int | None = None) -> None:
        """Update rate (and optionally burst) for future buckets and reset existing."""
        if rate <= 0:
            raise ValueError(f"rate must be positive, got {rate}")
        if burst is not None and burst < 1:
            raise ValueError(f"burst must be >= 1, got {burst}")
        with self._lock:
            self._rate = rate
            if burst is not None:
                self._burst = burst
            # Recreate existing buckets with new params
            for key in list(self._buckets):
                self._buckets[key] = _Bucket(self._rate, self._burst)

    @property
    def rate(self) -> float:
        return self._rate

    @property
    def burst(self) -> int:
        return self._burst

    @property
    def n_exchanges(self) -> int:
        return len(self._buckets)

    def status(self) -> dict[str, object]:
        with self._lock:
            return {
                "rate": self._rate,
                "burst": self._burst,
                "exchanges": {ex: round(b.tokens, 3) for ex, b in self._buckets.items()},
            }
