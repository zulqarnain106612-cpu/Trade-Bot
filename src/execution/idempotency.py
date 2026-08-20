"""
Idempotency keys and submission de-duplication for every order path (LAW3).

The architectural law is that duplicate prevention is *structural*, not a
convention: a retry, a WebSocket reconnect, or a reconciliation pass must not
be able to put a second live order on an exchange. Two mechanisms cooperate
here, and both are required — either alone leaves a hole:

1. **Deterministic key.** :func:`derive_idempotency_key` hashes the *intent*
   (strategy, symbol, side, quantised quantity, purpose, coarse time bucket)
   rather than the attempt. The same intent replayed for any reason produces
   the same key, so the duplicate is recognisable before it reaches the wire.
2. **Client order id on the exchange call.** The key is sent as the venue's
   client-order-id field (:func:`client_order_id_params`). This is the only
   defence that survives a process crash between submit and ack: the exchange
   itself rejects the second submission of an id it has already seen. A purely
   in-process registry cannot do that, because the registry dies with the
   process while the order lives on.

The local :class:`IdempotencyRegistry` is the fast path — it rejects the
duplicate without spending an API call, and it remembers the outcome of the
first attempt so the caller can return the original result instead of an
error.

Quantity is quantised before hashing (:data:`_QTY_QUANTUM`). Float sizing is
not bit-reproducible across a retry that recomputes Kelly from a marginally
different equity reading, and a key that changes with the eighth decimal of
size is not an idempotency key at all.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from dataclasses import dataclass, field
from decimal import ROUND_HALF_EVEN, Decimal
from enum import StrEnum
from typing import Any, Final


__all__ = [
    "DuplicateOrderError",
    "IdempotencyRecord",
    "IdempotencyRegistry",
    "SubmissionState",
    "client_order_id_params",
    "derive_idempotency_key",
]

# Client-order-id length limits are venue-specific and unforgiving: Binance
# accepts 36 chars, OKX only 32, Bybit 36. A single 32-char alphanumeric form
# is valid everywhere, so keys are generated once and never re-encoded per
# venue -- re-encoding would mean the "same" intent carries different ids on
# different venues and cross-venue duplicate detection silently stops working.
_KEY_PREFIX: Final[str] = "tb"
_KEY_HEX_LEN: Final[int] = 30
KEY_MAX_LEN: Final[int] = len(_KEY_PREFIX) + _KEY_HEX_LEN  # 32

# Sizing is recomputed on a retry and will not reproduce bit-for-bit; 1e-8 is
# the smallest unit any supported venue actually settles.
_QTY_QUANTUM: Final[Decimal] = Decimal("0.00000001")

# Intents are bucketed in time so that the *same* signal re-issued moments
# later collides, while a genuinely new signal for the same symbol an hour
# later does not. 60s matches the shortest trading timeframe in this repo.
DEFAULT_BUCKET_S: Final[int] = 60

# Field name each venue uses for the caller-supplied client order id.
_CLIENT_ID_FIELD: Final[dict[str, str]] = {
    "binance": "newClientOrderId",
    "binanceusdm": "newClientOrderId",
    "binancecoinm": "newClientOrderId",
    "okx": "clOrdId",
    "bybit": "orderLinkId",
}
# ccxt normalises this for venues it knows; used when the venue is unknown.
_CLIENT_ID_FIELD_DEFAULT: Final[str] = "clientOrderId"


class SubmissionState(StrEnum):
    """Lifecycle of a single idempotency key."""

    IN_FLIGHT = "in_flight"
    COMPLETED = "completed"
    FAILED = "failed"


class DuplicateOrderError(Exception):
    """
    Raised when an order submission reuses a key that is in flight or done.

    Carries the prior record so a caller that is retrying deliberately can
    return the original outcome rather than treating the rejection as a
    failure.
    """

    def __init__(self, key: str, record: IdempotencyRecord) -> None:
        super().__init__(
            f"Duplicate order submission for idempotency key {key!r} "
            f"(prior state={record.state.value}, order_id={record.order_id!r})"
        )
        self.key = key
        self.record = record


@dataclass
class IdempotencyRecord:
    """Outcome of the first submission seen for a key."""

    key: str
    state: SubmissionState = SubmissionState.IN_FLIGHT
    order_id: str | None = None
    result: dict[str, Any] | None = None
    error: str | None = None
    created_at: float = field(default_factory=time.monotonic)
    updated_at: float = field(default_factory=time.monotonic)


def derive_idempotency_key(
    *,
    strategy_id: str,
    symbol: str,
    side: str,
    quantity: float,
    purpose: str,
    intent_id: str | None = None,
    bucket_s: int = DEFAULT_BUCKET_S,
    now: float | None = None,
) -> str:
    """
    Build the deterministic idempotency key for one order intent.

    ``intent_id`` pins the key to a caller-owned identity (a trade_id when
    exiting a known position, for instance). When it is given the time bucket
    is *not* mixed in: an exit for a specific position is the same intent
    whether it is retried now or in ten minutes, and bucketing it would let a
    retry that straddles a bucket boundary submit a second exit order.

    ``purpose`` separates orders that would otherwise collide -- an entry and
    the emergency flatten that follows it share symbol and quantity but are
    opposite intents.
    """
    if bucket_s <= 0:
        raise ValueError(f"bucket_s must be positive, got {bucket_s}")

    qty = Decimal(str(quantity)).quantize(_QTY_QUANTUM, rounding=ROUND_HALF_EVEN)

    parts = [strategy_id, symbol, side.lower(), format(qty, "f"), purpose]
    if intent_id is not None:
        parts.append(intent_id)
    else:
        ts = time.time() if now is None else now
        parts.append(str(int(ts // bucket_s)))

    digest = hashlib.sha256("\x1f".join(parts).encode()).hexdigest()
    return f"{_KEY_PREFIX}{digest[:_KEY_HEX_LEN]}"


def client_order_id_params(
    exchange_id: str | None,
    idempotency_key: str,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Return ``params`` for a ccxt order call carrying the client order id.

    An explicit id already present in ``params`` wins -- callers that need a
    venue-specific format (a broker/referral-tagged id, say) must not have it
    silently overwritten.
    """
    out = dict(params or {})
    field_name = _CLIENT_ID_FIELD.get((exchange_id or "").lower(), _CLIENT_ID_FIELD_DEFAULT)
    if field_name not in out and "clientOrderId" not in out:
        out[field_name] = idempotency_key
    return out


class IdempotencyRegistry:
    """
    In-process record of submitted idempotency keys.

    Entries expire after ``ttl_s``. The TTL exists because unbounded retention
    is a leak in a long-running process, and because a key whose time bucket
    closed long ago can never be legitimately replayed. It must comfortably
    exceed the order-confirmation timeout, or an order still being polled
    could have its own key expire underneath it.
    """

    def __init__(self, ttl_s: float = 3600.0, max_entries: int = 10_000) -> None:
        if ttl_s <= 0:
            raise ValueError(f"ttl_s must be positive, got {ttl_s}")
        self._ttl_s = ttl_s
        self._max_entries = max_entries
        self._records: dict[str, IdempotencyRecord] = {}
        self._lock = asyncio.Lock()

    async def reserve(self, key: str) -> IdempotencyRecord:
        """
        Claim ``key`` for a submission about to be made.

        Raises :class:`DuplicateOrderError` if the key is already in flight or
        has completed. A previously FAILED key is reclaimable: the first
        attempt provably did not result in a live order, so retrying it is the
        correct behaviour rather than a duplicate.
        """
        async with self._lock:
            self._evict_expired()
            existing = self._records.get(key)
            if existing is not None and existing.state is not SubmissionState.FAILED:
                raise DuplicateOrderError(key, existing)

            record = IdempotencyRecord(key=key)
            self._records[key] = record
            self._enforce_capacity()
            return record

    async def complete(
        self,
        key: str,
        order_id: str | None,
        result: dict[str, Any] | None = None,
    ) -> None:
        """Mark a reserved key as having produced a real exchange order."""
        async with self._lock:
            record = self._records.get(key)
            if record is None:
                record = IdempotencyRecord(key=key)
                self._records[key] = record
            record.state = SubmissionState.COMPLETED
            record.order_id = order_id
            record.result = result
            record.updated_at = time.monotonic()

    async def fail(self, key: str, error: str, *, retryable: bool) -> None:
        """
        Record the outcome of a failed submission.

        ``retryable`` is the caller's assertion that no order reached the
        exchange -- only then is the key released for reuse. A network error
        or timeout is *not* retryable in this sense: the request may well have
        been executed with the response lost, so the key stays claimed and the
        order is left to reconciliation rather than being submitted again.
        """
        async with self._lock:
            record = self._records.get(key)
            if record is None:
                record = IdempotencyRecord(key=key)
                self._records[key] = record
            record.state = SubmissionState.FAILED if retryable else SubmissionState.COMPLETED
            record.error = error
            record.updated_at = time.monotonic()

    async def get(self, key: str) -> IdempotencyRecord | None:
        """Return the live record for ``key``, or None if unknown/expired."""
        async with self._lock:
            self._evict_expired()
            return self._records.get(key)

    async def seen(self, key: str) -> bool:
        """True if ``key`` is in flight or completed (i.e. must not resubmit)."""
        record = await self.get(key)
        return record is not None and record.state is not SubmissionState.FAILED

    def _evict_expired(self) -> None:
        cutoff = time.monotonic() - self._ttl_s
        for key in [k for k, r in self._records.items() if r.updated_at < cutoff]:
            del self._records[key]

    def _enforce_capacity(self) -> None:
        # Bound the map even when everything is within TTL. Oldest-first, and
        # never at the expense of an in-flight key -- dropping one of those
        # would re-open the duplicate window it exists to close.
        overflow = len(self._records) - self._max_entries
        if overflow <= 0:
            return
        evictable = sorted(
            (r for r in self._records.values() if r.state is not SubmissionState.IN_FLIGHT),
            key=lambda r: r.updated_at,
        )
        for record in evictable[:overflow]:
            del self._records[record.key]
