"""
In-process publish/subscribe bus — GOV-032, GOV-033, GOV-034.

The GUI transport is a timer today: ``/ws`` sleeps for the heartbeat and
then builds a snapshot, so a fill landing one millisecond after a tick is
invisible for the rest of the period. Raising the tick rate trades latency
for load without ever removing the floor. This bus removes the floor by
letting the producer say *something changed*.

Two properties are load-bearing, and both are asserted by tests rather
than described:

**Publishing never blocks.** :meth:`EventBus.publish` is a plain ``def``.
It cannot await, so no amount of subscriber slowness can suspend a caller
inside the trading loop. A GUI client on a congested link must never be
able to delay a fill being recorded.

**A full queue drops the oldest event, not the newest.** Every consumer
here renders current state. When a slow client falls behind, the stale
frames are the ones worth losing; dropping the newest would pin the panel
to whatever was on screen when congestion started and leave it there.
Drops are counted per subscription and surfaced, because a silent drop is
indistinguishable from a quiet market.

The bus is deliberately not durable and not ordered across topics: it is a
liveness mechanism for display, never a substitute for storage. Nothing
that must survive a restart may be published and not persisted.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Final

import structlog

from .topics import ALL, KNOWN_TOPICS

log = structlog.get_logger(__name__)

#: Per-subscription queue depth. Deep enough to absorb a burst from a
#: single trading cycle, shallow enough that a wedged client cannot hold
#: meaningful memory: ~256 small dicts per client.
DEFAULT_MAXSIZE: Final = 256


@dataclass(frozen=True, slots=True)
class Event:
    """One published fact. Immutable so fan-out can share a single object."""

    topic: str
    payload: Mapping[str, Any]
    sequence: int
    published_at: float

    def __post_init__(self) -> None:
        # Fan-out hands the same instance to every subscriber; a mutable
        # payload would let one consumer edit what the others receive.
        object.__setattr__(self, "payload", MappingProxyType(dict(self.payload)))


@dataclass(eq=False)
class Subscription:
    """
    A consumer's view of the bus.

    Async-iterable. Iteration ends when :meth:`close` is called or the bus
    shuts down, so a consumer task exits on its own rather than needing to
    be cancelled.
    """

    topics: frozenset[str]
    maxsize: int
    _queue: asyncio.Queue[Event] = field(init=False)
    _closed: bool = field(default=False, init=False)
    _wake: asyncio.Event = field(default_factory=asyncio.Event, init=False)
    dropped: int = field(default=0, init=False)
    delivered: int = field(default=0, init=False)

    def __post_init__(self) -> None:
        self._queue = asyncio.Queue(maxsize=self.maxsize)

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def pending(self) -> int:
        return self._queue.qsize()

    def wants(self, topic: str) -> bool:
        return ALL in self.topics or topic in self.topics

    def _offer(self, event: Event) -> None:
        """Enqueue, evicting the oldest event if the queue is already full."""
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:  # pragma: no cover - drained concurrently
                pass
            else:
                self.dropped += 1
            # The queue has a free slot now, and publish() is synchronous so
            # nothing could have refilled it in between.
            self._queue.put_nowait(event)
        self.delivered += 1

    def close(self) -> None:
        """Idempotent. Wakes an iterator parked in ``__anext__``."""
        if self._closed:
            return
        self._closed = True
        self._wake.set()

    async def drain(self, window: float) -> int:
        """
        Wait up to `window` seconds for an event, then take every one
        already queued.
        Returns how many were consumed; ``0`` means the timeout elapsed or
        the subscription closed.

        Consumers of this bus render current state, so a burst -- one
        trading cycle closing three positions -- should cost one render,
        not three that each overwrite the last. Coalescing here rather
        than in each consumer keeps that decision in one place.

        A timeout is not an error: for a socket it is the keepalive tick,
        and the caller does not need to tell the two apart.
        """
        try:
            await asyncio.wait_for(self.__anext__(), timeout=window)
        except (TimeoutError, StopAsyncIteration):
            return 0
        taken = 1
        while not self._queue.empty():
            taken += 1
            self._queue.get_nowait()
        return taken

    def __aiter__(self) -> Subscription:
        return self

    async def __anext__(self) -> Event:
        while True:
            if not self._queue.empty():
                return self._queue.get_nowait()
            if self._closed:
                raise StopAsyncIteration
            getter = asyncio.ensure_future(self._queue.get())
            waiter = asyncio.ensure_future(self._wake.wait())
            done, pending = await asyncio.wait(
                (getter, waiter), return_when=asyncio.FIRST_COMPLETED
            )
            for task in pending:
                task.cancel()
            if getter in done:
                return getter.result()
            # Woken by close(). Loop: anything queued in the meantime is
            # drained by the check at the top before iteration ends.


class EventBus:
    """
    Process-local fan-out. One instance per application; see
    :func:`get_event_bus`.

    ``clock`` is injected so tests can assert timestamps without sleeping
    (GOV-016).
    """

    __slots__ = ("_clock", "_maxsize", "_sequence", "_subscribers", "_closed")

    def __init__(
        self,
        *,
        maxsize: int = DEFAULT_MAXSIZE,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if maxsize < 1:
            raise ValueError(f"maxsize must be >= 1, got {maxsize}")
        self._clock = clock
        self._maxsize = maxsize
        self._sequence = 0
        self._subscribers: list[Subscription] = []
        self._closed = False

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    @property
    def closed(self) -> bool:
        return self._closed

    def subscribe(
        self,
        topics: Iterable[str] | None = None,
        *,
        maxsize: int | None = None,
    ) -> Subscription:
        """
        Register a consumer. ``topics=None`` means every topic.

        Raises ``ValueError`` for a topic that is not in
        :data:`~src.eventbus.topics.KNOWN_TOPICS`: a subscriber that names
        a topic nobody publishes would wait forever, and the symptom is a
        panel that never updates rather than an error.
        """
        if self._closed:
            raise RuntimeError("cannot subscribe to a closed EventBus")
        wanted = frozenset({ALL} if topics is None else topics)
        unknown = {t for t in wanted if t != ALL and t not in KNOWN_TOPICS}
        if unknown:
            raise ValueError(f"unknown topic(s): {sorted(unknown)}")
        sub = Subscription(topics=wanted, maxsize=maxsize or self._maxsize)
        self._subscribers.append(sub)
        return sub

    def unsubscribe(self, sub: Subscription) -> None:
        """Idempotent. Closes the subscription so its consumer loop exits."""
        sub.close()
        with contextlib.suppress(ValueError):
            self._subscribers.remove(sub)

    def publish(self, topic: str, payload: Mapping[str, Any]) -> int:
        """
        Fan ``payload`` out to every matching subscription and return how
        many received it.

        Synchronous by contract: a caller inside the trading loop cannot be
        suspended here regardless of consumer behaviour.
        """
        if self._closed:
            return 0
        if topic not in KNOWN_TOPICS:
            raise ValueError(f"unknown topic: {topic!r}")

        self._sequence += 1
        event = Event(
            topic=topic,
            payload=payload,
            sequence=self._sequence,
            published_at=self._clock(),
        )

        delivered = 0
        stale: list[Subscription] = []
        for sub in self._subscribers:
            if sub.closed:
                stale.append(sub)
                continue
            if not sub.wants(topic):
                continue
            sub._offer(event)
            delivered += 1

        for sub in stale:
            self.unsubscribe(sub)
        return delivered

    def close(self) -> None:
        """Idempotent. Ends every subscription's iteration."""
        if self._closed:
            return
        self._closed = True
        for sub in tuple(self._subscribers):
            sub.close()
        self._subscribers.clear()

    def stats(self) -> dict[str, int]:
        """Observability snapshot; safe to call from a metrics handler."""
        return {
            "subscribers": len(self._subscribers),
            "published": self._sequence,
            "dropped": sum(s.dropped for s in self._subscribers),
            "pending": sum(s.pending for s in self._subscribers),
        }


_bus: EventBus | None = None


def get_event_bus() -> EventBus:
    """
    The application-wide bus, created on first use.

    A module-level accessor rather than a constructor argument threaded
    through every producer: the alternative is an optional parameter on
    each call site, and an optional bus is one that is ``None`` in
    production the first time somebody forgets to pass it.
    """
    global _bus
    if _bus is None or _bus.closed:
        _bus = EventBus()
    return _bus


def set_event_bus(bus: EventBus | None) -> None:
    """Install a bus (tests, or an application wiring its own). ``None`` resets."""
    global _bus
    _bus = bus
