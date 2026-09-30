"""
Deciding tests for GOV-032 / GOV-033 / GOV-034 — the in-process event bus.

No test sleeps (GOV-016): ordering is established with ``asyncio.Event``
handshakes and the bus clock is injected, so timestamps are asserted
exactly rather than approximately.
"""

from __future__ import annotations

import asyncio
import inspect

import pytest

from src.eventbus import (
    DEFAULT_MAXSIZE,
    EVENT_BUS,
    PORTFOLIO,
    RISK,
    Event,
    EventBus,
    get_event_bus,
)
from src.eventbus import topics as T


@pytest.fixture
def bus() -> EventBus:
    """A bus on a frozen clock; ticks are advanced explicitly where relevant."""
    return EventBus(maxsize=3, clock=lambda: 100.0)


# --------------------------------------------------------------------------
# GOV-033: publishing can never block the producer
# --------------------------------------------------------------------------


def test_publish_is_not_a_coroutine_function() -> None:
    """
    Structural, not behavioural, and deliberately so.

    Every behavioural test below would still pass if ``publish`` became
    ``async def`` and awaited a full queue -- they would just await it. The
    property that matters is that a caller inside the trading loop has no
    await point here at all, and only the signature can assert that.
    """
    assert not inspect.iscoroutinefunction(EventBus.publish)
    assert not inspect.isasyncgenfunction(EventBus.publish)


def test_publish_to_a_full_queue_returns_without_awaiting(bus: EventBus) -> None:
    """A wedged consumer cannot suspend the producer."""
    bus.subscribe([T.PORTFOLIO])
    for i in range(bus._maxsize * 4):
        assert bus.publish(T.PORTFOLIO, {"i": i}) == 1


# --------------------------------------------------------------------------
# GOV-034: a full queue drops the OLDEST event
# --------------------------------------------------------------------------


def test_overflow_evicts_oldest_and_keeps_newest(bus: EventBus) -> None:
    sub = bus.subscribe([T.PORTFOLIO])
    for i in range(5):  # maxsize is 3, so 0 and 1 are evicted
        bus.publish(T.PORTFOLIO, {"i": i})

    assert sub.dropped == 2
    assert sub.pending == 3
    assert [sub._queue.get_nowait().payload["i"] for _ in range(3)] == [2, 3, 4]


def test_drops_are_counted_not_silent(bus: EventBus) -> None:
    """
    A dropped frame and a quiet market look identical on screen. The count
    is the only thing that separates them, so it is part of the contract.
    """
    sub = bus.subscribe([T.PORTFOLIO])
    for i in range(4):
        bus.publish(T.PORTFOLIO, {"i": i})
    assert sub.dropped == 1
    assert bus.stats()["dropped"] == 1


def test_a_slow_subscriber_does_not_starve_a_fast_one(bus: EventBus) -> None:
    slow = bus.subscribe([T.PORTFOLIO])
    fast = bus.subscribe([T.PORTFOLIO])
    for i in range(4):
        bus.publish(T.PORTFOLIO, {"i": i})
        if i < 3:
            fast._queue.get_nowait()

    assert slow.dropped == 1
    assert fast.dropped == 0


# --------------------------------------------------------------------------
# Routing
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("subscribed", "published", "expect_delivery"),
    [
        ([T.PORTFOLIO], T.PORTFOLIO, True),
        ([T.PORTFOLIO], T.RISK, False),
        ([T.PORTFOLIO, T.RISK], T.RISK, True),
        (None, T.ORDERS, True),
        ([T.ALL], T.APPROVALS, True),
    ],
)
def test_topic_routing(
    bus: EventBus, subscribed: list[str] | None, published: str, expect_delivery: bool
) -> None:
    sub = bus.subscribe(subscribed)
    assert bus.publish(published, {}) == int(expect_delivery)
    assert sub.pending == int(expect_delivery)


def test_publishing_an_unknown_topic_raises(bus: EventBus) -> None:
    """A typo'd literal must fail loudly, not report zero subscribers."""
    with pytest.raises(ValueError, match="unknown topic"):
        bus.publish("portfolios", {})


def test_publishing_to_the_wildcard_raises(bus: EventBus) -> None:
    """``ALL`` is a subscribe-side filter; publishing to it is a mistake."""
    with pytest.raises(ValueError, match="unknown topic"):
        bus.publish(T.ALL, {})


def test_subscribing_to_an_unknown_topic_raises(bus: EventBus) -> None:
    with pytest.raises(ValueError, match="unknown topic"):
        bus.subscribe([T.PORTFOLIO, "positions"])


# --------------------------------------------------------------------------
# Event immutability and fan-out sharing
# --------------------------------------------------------------------------


def test_payload_is_snapshotted_at_publish(bus: EventBus) -> None:
    sub = bus.subscribe([T.PORTFOLIO])
    source = {"equity": 1.0}
    bus.publish(T.PORTFOLIO, source)
    source["equity"] = 2.0
    assert sub._queue.get_nowait().payload["equity"] == 1.0


def test_one_consumer_cannot_edit_what_another_receives(bus: EventBus) -> None:
    a = bus.subscribe([T.PORTFOLIO])
    b = bus.subscribe([T.PORTFOLIO])
    bus.publish(T.PORTFOLIO, {"equity": 1.0})

    event_a = a._queue.get_nowait()
    event_b = b._queue.get_nowait()
    assert event_a is event_b  # fan-out shares one object
    with pytest.raises(TypeError):
        event_a.payload["equity"] = 2.0  # type: ignore[index]


def test_event_is_frozen(bus: EventBus) -> None:
    bus.subscribe([T.PORTFOLIO])
    with pytest.raises(AttributeError):
        Event(topic=T.PORTFOLIO, payload={}, sequence=1, published_at=0.0).topic = "x"  # type: ignore[misc]


def test_sequence_is_monotonic_and_clock_is_injected() -> None:
    ticks = iter([10.0, 11.0])
    bus = EventBus(clock=lambda: next(ticks))
    sub = bus.subscribe(None)
    bus.publish(T.PORTFOLIO, {})
    bus.publish(T.RISK, {})

    first = sub._queue.get_nowait()
    second = sub._queue.get_nowait()
    assert (first.sequence, first.published_at) == (1, 10.0)
    assert (second.sequence, second.published_at) == (2, 11.0)


# --------------------------------------------------------------------------
# Lifecycle
# --------------------------------------------------------------------------


def test_maxsize_must_be_positive() -> None:
    with pytest.raises(ValueError, match="maxsize must be >= 1"):
        EventBus(maxsize=0)


def test_per_subscription_maxsize_overrides_the_default(bus: EventBus) -> None:
    sub = bus.subscribe([T.PORTFOLIO], maxsize=1)
    bus.publish(T.PORTFOLIO, {"i": 0})
    bus.publish(T.PORTFOLIO, {"i": 1})
    assert (sub.pending, sub.dropped) == (1, 1)


def test_default_maxsize_is_used_when_unset() -> None:
    assert EventBus().subscribe(None).maxsize == DEFAULT_MAXSIZE


def test_unsubscribe_is_idempotent(bus: EventBus) -> None:
    sub = bus.subscribe([T.PORTFOLIO])
    bus.unsubscribe(sub)
    bus.unsubscribe(sub)
    assert bus.subscriber_count == 0
    assert sub.closed


def test_a_closed_subscription_is_pruned_on_next_publish(bus: EventBus) -> None:
    sub = bus.subscribe([T.PORTFOLIO])
    sub.close()
    assert bus.publish(T.PORTFOLIO, {}) == 0
    assert bus.subscriber_count == 0


def test_close_is_idempotent_and_stops_delivery(bus: EventBus) -> None:
    sub = bus.subscribe([T.PORTFOLIO])
    bus.close()
    bus.close()
    assert bus.closed and sub.closed
    assert bus.publish(T.PORTFOLIO, {}) == 0


def test_subscribing_to_a_closed_bus_raises(bus: EventBus) -> None:
    bus.close()
    with pytest.raises(RuntimeError, match="closed EventBus"):
        bus.subscribe(None)


def test_stats_reports_the_whole_bus(bus: EventBus) -> None:
    a = bus.subscribe([T.PORTFOLIO])
    bus.subscribe([T.RISK])
    bus.publish(T.PORTFOLIO, {})
    assert bus.stats() == {
        "subscribers": 2,
        "published": 1,
        "dropped": 0,
        "pending": 1,
    }
    assert (a.delivered, a.pending) == (1, 1)


# --------------------------------------------------------------------------
# Async iteration -- handshakes only, no sleeps (GOV-016)
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_iteration_delivers_queued_events_in_order(bus: EventBus) -> None:
    sub = bus.subscribe([T.PORTFOLIO])
    bus.publish(T.PORTFOLIO, {"i": 0})
    bus.publish(T.PORTFOLIO, {"i": 1})
    sub.close()

    assert [e.payload["i"] async for e in sub] == [0, 1]


@pytest.mark.asyncio
async def test_a_parked_consumer_is_woken_by_publish(bus: EventBus) -> None:
    """The point of the bus: the consumer resumes on the event, not a timer."""
    sub = bus.subscribe([T.PORTFOLIO])
    parked = asyncio.Event()
    received: list[int] = []

    async def consume() -> None:
        parked.set()
        async for event in sub:
            received.append(event.payload["i"])
            return

    task = asyncio.create_task(consume())
    await parked.wait()
    await asyncio.sleep(0)  # scheduler yield: let consume() reach the queue

    bus.publish(T.PORTFOLIO, {"i": 7})
    await asyncio.wait_for(task, timeout=5)
    assert received == [7]


@pytest.mark.asyncio
async def test_close_wakes_a_parked_consumer_and_ends_iteration(bus: EventBus) -> None:
    """Closing must end the loop; a cancelled task would leak the client slot."""
    sub = bus.subscribe([T.PORTFOLIO])
    parked = asyncio.Event()
    ended = asyncio.Event()

    async def consume() -> None:
        parked.set()
        async for _ in sub:
            pass
        ended.set()

    task = asyncio.create_task(consume())
    await parked.wait()
    await asyncio.sleep(0)

    bus.close()
    await asyncio.wait_for(task, timeout=5)
    assert ended.is_set()


@pytest.mark.asyncio
async def test_queued_events_are_drained_before_close_ends_iteration(
    bus: EventBus,
) -> None:
    sub = bus.subscribe([T.PORTFOLIO])
    bus.publish(T.PORTFOLIO, {"i": 1})
    bus.close()
    assert [e.payload["i"] async for e in sub] == [1]
    with pytest.raises(StopAsyncIteration):
        await sub.__anext__()


# --------------------------------------------------------------------------
# drain(): the coalescing wait /ws parks on
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_returns_immediately_when_an_event_is_already_queued(
    bus: EventBus,
) -> None:
    events = bus.subscribe([PORTFOLIO, RISK])
    bus.publish(PORTFOLIO, {"equity_usd": 1.0})

    await asyncio.wait_for(events.drain(60.0), timeout=5)
    assert events.pending == 0


@pytest.mark.asyncio
async def test_a_burst_collapses_to_one_wakeup(bus: EventBus) -> None:
    """
    The socket carries current state, not a change log. Three closes in one
    cycle must cost the client one frame, not three it would overwrite.
    """
    events = bus.subscribe([PORTFOLIO])
    for i in range(3):
        bus.publish(PORTFOLIO, {"equity_usd": float(i)})

    await asyncio.wait_for(events.drain(60.0), timeout=5)
    assert events.pending == 0


@pytest.mark.asyncio
async def test_a_parked_waiter_is_woken_by_a_later_publish(bus: EventBus) -> None:
    """The latency claim: the wake comes from the producer, not the clock."""
    events = bus.subscribe([PORTFOLIO])
    parked = asyncio.Event()

    async def wait() -> None:
        parked.set()
        # A heartbeat long enough that a timeout return would fail the test.
        await events.drain(3600.0)

    task = asyncio.create_task(wait())
    await parked.wait()
    await asyncio.sleep(0)  # scheduler yield, not a delay

    bus.publish(PORTFOLIO, {"equity_usd": 1.0})
    await asyncio.wait_for(task, timeout=5)


@pytest.mark.asyncio
async def test_the_heartbeat_still_fires_with_no_events(bus: EventBus) -> None:
    """Pre-existing behaviour is preserved: an idle socket still ticks."""
    events = bus.subscribe([PORTFOLIO])
    await asyncio.wait_for(events.drain(0.0), timeout=5)


@pytest.mark.asyncio
async def test_a_closed_subscription_does_not_spin(bus: EventBus) -> None:
    """
    Disconnect races teardown: the bus can close before the loop's next
    pass. Returning (rather than raising) lets the caller fall through to
    its own disconnect handling.
    """
    events = bus.subscribe([PORTFOLIO])
    bus.close()
    await asyncio.wait_for(events.drain(3600.0), timeout=5)


@pytest.mark.asyncio
async def test_risk_events_wake_the_socket_too(bus: EventBus) -> None:
    """Kill-switch trips and gate blocks were never pushed at all before."""
    events = bus.subscribe([PORTFOLIO, RISK])
    bus.publish(RISK, {"blocked": True})
    await asyncio.wait_for(events.drain(60.0), timeout=5)
    assert events.pending == 0


# --------------------------------------------------------------------------
# Module-level accessor
# --------------------------------------------------------------------------


def test_get_event_bus_returns_one_shared_instance() -> None:
    """
    Bound at import, so there is no window in which two callers each build
    a bus and then talk past each other -- a producer publishing into one
    while the consumer is parked on the other.
    """
    assert get_event_bus() is get_event_bus() is EVENT_BUS


def test_the_application_bus_is_open_and_stays_open() -> None:
    """
    Nothing closes the process-wide bus: closing it would end every
    consumer's iteration with no way to reopen it. Tests that need
    isolation build their own EventBus, as every test above does.
    """
    assert not EVENT_BUS.closed


def test_the_module_exposes_no_way_to_rebind_the_bus() -> None:
    """
    LAW2: a setter is a `global` rebinding by another name, and a bus
    swapped mid-flight strands every subscriber already parked on the old
    one. The accessor is read-only by construction.
    """
    import src.eventbus as pkg
    import src.eventbus.bus as bus_module

    assert not hasattr(bus_module, "set_event_bus")
    assert not hasattr(pkg, "set_event_bus")
