"""
INV-032 -- the in-process publish path, and the law that holds it safe.

The dashboard used to be fed by timers alone: one ``while True: sleep(5)``
per websocket client, and sixteen ``setInterval`` calls in the browser. A
timer cannot be faster than its period, so a fill that landed 10ms after a
tick waited out the rest of the heartbeat no matter how fast the executor
was. This module is the thing that lets a producer say "it happened" at the
moment it happens.

The load-bearing law, and the reason this file is written the way it is:

    **The GUI must never be able to apply backpressure to the trading loop.**

Every design choice below follows from it:

  publish() is sync      an ``async def`` would let a caller ``await`` on a
                         consumer, and the first caller to do that puts a
                         dashboard in the path of an order
  publish() never awaits nothing in it can yield to the event loop, so a
                         producer's latency does not depend on how many
                         subscribers exist or how slow they are
  publish() never raises a dashboard that has gone wrong is not a reason to
                         fail a fill; the producer is a risk gate, not a
                         transport, and must not learn about transport
  drop the oldest        a bounded buffer that blocks is backpressure wearing
                         a hat. When a consumer cannot keep up, the consumer
                         loses data -- never the producer
  count the drops        a starved client must be *visible*. Silently losing
                         the oldest frame and showing a confident dashboard is
                         worse than showing nothing, because the operator
                         cannot tell the difference

``collections.deque(maxlen=N)`` rather than ``asyncio.Queue(maxsize=N)`` is
deliberate. Queue's only bounded put is ``put_nowait``, which *raises*
``QueueFull`` -- so drop-oldest becomes catch, ``get_nowait()``, put again,
and that sequence has a race against a concurrent drain that can leave the
queue holding a gap it never counted. A deque with ``maxlen`` evicts the
leftmost element on append as a single operation, which is exactly the
semantics wanted, with no exception path to get wrong.

Layering (GOV-018): this package sits in ``foundation``. That is not
housekeeping -- it is the only layer that works. Producers live in ``data``
and ``diagnostics`` (io), ``tuning`` and ``intelligence`` (analytics),
``execution`` and ``risk`` (decision) and ``engine`` (orchestration), and the
consumer is ``api`` (edge). A bus placed in any of those is imported upward
by all the ones below it, and ``check_static_invariants`` refuses every one
of those edges. Only a package below all of them can be imported by all of
them.
"""

from __future__ import annotations

import asyncio
import collections
import time
from collections.abc import AsyncIterator, Iterable, Mapping
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

import structlog

log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)


# Every topic the bus will carry. A frozen set rather than free-form strings
# because both ends need it: a producer typo otherwise publishes into a topic
# nobody reads, and PR 3's client-driven subscribe has to be able to refuse an
# unknown topic explicitly instead of silently subscribing to nothing.
TOPICS: frozenset[str] = frozenset(
    {
        "position",  # executor open / close / partial fill
        "equity",  # mark_to_market
        "order",  # order FSM state transitions
        "approval",  # approval queue add / resolve
        "signal",  # orchestrator tick
        "regime",  # orchestrator tick
        "gate",  # evaluate_all_gates, *including* blocks
        "killswitch",  # strategy_kill_switch
        "capital_floor",  # capital_preservation_floor
        "selftuning",  # parameter change / rollback
        "drift",  # SignalDebugger
        "health",  # RuntimeMonitor
        "intel",  # provider health transitions
        "price",  # orderbook stream, coalesced
        "book",  # orderbook stream, coalesced
    }
)

# How many events one subscriber may fall behind by before it starts losing
# the oldest. Sized for the burst a single orchestrator tick produces (one
# signal + one regime + a gate result per strategy + a position event per
# fill), with room to spare: a consumer that cannot drain this between ticks
# is not briefly busy, it is broken, and the drop counter is how that gets
# noticed.
DEFAULT_MAXLEN = 256


def _now_ms() -> int:
    """Wall-clock milliseconds. Stamped at the producer, never at send."""
    return int(time.time() * 1000)


@dataclass(frozen=True, slots=True)
class Event:
    """
    One published fact.

    ``ts_ms`` is stamped when ``publish`` is called, not when the frame is
    written to a socket. That is the whole point of carrying it: the
    difference between this and the browser's clock is the end-to-end lag,
    and a timestamp applied at send time would report zero lag no matter how
    badly the transport was starved.

    ``mono_ns`` is the same instant on the monotonic clock, and it exists
    because the two consumers need different things. The browser can only
    compare against an epoch timestamp, so the frame has to carry wall clock.
    The server-side lag histogram is a *duration*, and a duration taken from
    wall clock is wrong the one time it matters: an NTP correction landing
    between the publish and the send makes the measurement negative or
    enormous, which is indistinguishable from the starvation the metric is
    watching for. Never serialized -- it is meaningless outside this process.
    """

    topic: str
    data: Mapping[str, Any]
    ts_ms: int
    mono_ns: int = field(default_factory=time.monotonic_ns)


@dataclass(eq=False)
class Subscription:
    """
    One consumer's view of the bus.

    Not reusable and not shared: each subscriber owns its own buffer so that
    one slow consumer cannot evict another's events. Iterate it with ``async
    for``; call ``close()`` (or use it as an async context manager) to
    unregister.
    """

    topics: frozenset[str]
    maxlen: int = DEFAULT_MAXLEN
    dropped: int = 0
    """Events discarded because this subscriber was not draining fast enough."""

    _queue: collections.deque[Event] = field(init=False, repr=False)
    _wake: asyncio.Event = field(init=False, repr=False)
    _closed: bool = field(default=False, init=False, repr=False)
    _unsubscribe: Any = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        self._queue = collections.deque(maxlen=self.maxlen)
        # Safe to build outside a running loop: since 3.10 asyncio.Event no
        # longer binds the loop at construction, and subscribe() is called
        # from synchronous setup code.
        self._wake = asyncio.Event()

    def _offer(self, event: Event) -> None:
        """
        Called by ``EventBus.publish`` only. Sync, non-blocking, total.

        The drop accounting happens *before* the append because a full deque
        evicts its leftmost element as part of ``append`` -- once it has
        happened there is nothing left to observe.
        """
        if len(self._queue) == self.maxlen:
            self.dropped += 1
        self._queue.append(event)
        self._wake.set()

    def close(self) -> None:
        """Unregister and wake the iterator so it can finish."""
        if self._closed:
            return
        self._closed = True
        if self._unsubscribe is not None:
            self._unsubscribe(self)
        self._wake.set()

    async def __aenter__(self) -> Subscription:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        self.close()

    async def __aiter__(self) -> AsyncIterator[Event]:
        """
        Yield events until closed.

        The clear-then-recheck below is not redundant. A publish that lands
        between the drain and the clear would otherwise have its wakeup
        erased, and the consumer would sleep on a non-empty queue until the
        *next* event arrived -- which, on a quiet topic, is a stall that
        looks exactly like a dropped connection.
        """
        while True:
            while self._queue:
                yield self._queue.popleft()
            if self._closed:
                return
            self._wake.clear()
            if self._queue or self._closed:
                continue
            await self._wake.wait()


class EventBus:
    """
    In-process topic bus. One instance per running system.

    Deliberately not thread-safe: every producer runs on the orchestrator's
    event loop, and making it thread-safe would mean a lock in ``publish``,
    which is a place a trading producer could block. If a producer ever needs
    to publish from a worker thread, it should hop to the loop first rather
    than this growing a lock.
    """

    def __init__(self, maxlen: int = DEFAULT_MAXLEN) -> None:
        self._subscribers: set[Subscription] = set()
        self._maxlen = maxlen
        self.published = 0
        self.unknown_topic_drops = 0

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    @property
    def total_dropped(self) -> int:
        """Drops across every live subscriber -- what the heartbeat reports."""
        return sum(sub.dropped for sub in self._subscribers)

    def publish(
        self,
        topic: str,
        payload: Mapping[str, Any],
        *,
        ts_ms: int | None = None,
    ) -> None:
        """
        Hand one event to every subscriber of ``topic``. Never blocks, never
        raises, never awaits.

        The blanket ``except`` is not laziness. This is called from inside
        risk gates and the executor, and the contract those callers rely on
        is that publishing cannot change what they do. An exception escaping
        here would turn a dashboard bug into a failed order, which is the one
        outcome this whole module exists to prevent.
        """
        try:
            if topic not in TOPICS:
                # Not raised: a typo'd topic in a risk gate must not take the
                # gate down with it. Counted and logged so it is still loud.
                self.unknown_topic_drops += 1
                log.error("eventbus.unknown_topic", topic=topic)
                return

            event = Event(
                topic=topic, data=payload, ts_ms=ts_ms if ts_ms is not None else _now_ms()
            )
            self.published += 1
            for sub in self._subscribers:
                if topic in sub.topics:
                    sub._offer(event)
        except Exception as exc:  # pragma: no cover - defence, not a path
            log.error("eventbus.publish_failed", topic=topic, error=type(exc).__name__)

    def subscribe(self, topics: Iterable[str]) -> Subscription:
        """
        Register a consumer for ``topics``.

        Raises on an unknown topic, unlike ``publish``. The asymmetry is the
        point: a publish comes from our own code on the trading path and must
        not be able to break it, while a subscribe comes from a client and is
        input to be validated. PR 3 turns this into the websocket's
        ``{"op":"subscribe"}`` refusal.
        """
        wanted = frozenset(topics)
        unknown = wanted - TOPICS
        if unknown:
            raise ValueError(f"unknown topic(s): {', '.join(sorted(unknown))}")

        sub = Subscription(topics=wanted, maxlen=self._maxlen)
        sub._unsubscribe = self._subscribers.discard
        self._subscribers.add(sub)
        return sub


@lru_cache(maxsize=1)
def get_event_bus() -> EventBus:
    """
    The process-wide bus.

    ``lru_cache`` matches ``get_settings``/``default_registry`` rather than a
    module-level global so it has a ``cache_clear()``. Tests should construct
    ``EventBus()`` directly instead of touching this -- REG-0005 is the entry
    for tests whose result depends on process state an earlier test left.
    """
    return EventBus()
