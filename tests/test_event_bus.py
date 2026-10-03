"""
INV-032 -- the GUI must never be able to apply backpressure to the trading loop.

These tests assert the *law*, not the latency. GOV-016 forbids sleeping for
synchronisation, and a bus test that waits on a wall clock would be both slow
and flaky; worse, it would assert the wrong thing. "Publishing was fast on
this run" is not the property. The properties are:

  - publish() returns without awaiting, so no producer can be suspended by a
    consumer that never drains;
  - a full subscriber loses its own oldest events and counts them, rather
    than blocking the publisher or silently lying;
  - publish() is total -- an unknown topic, a broken subscriber, a closed
    consumer all leave the caller unaffected.

Every ordering assertion is driven by the queue's own state or an
asyncio.Event handshake, never by elapsed time.
"""

from __future__ import annotations

import asyncio

import pytest

from src.eventbus import TOPICS, Event, EventBus, Subscription, get_event_bus


@pytest.fixture
def bus() -> EventBus:
    """
    A fresh bus per test.

    Constructed directly rather than via get_event_bus() on purpose: REG-0005
    is the entry for tests whose result depends on process-global state an
    earlier test left behind, and the cached accessor is exactly that.
    """
    return EventBus()


class TestPublishNeverBlocksTheProducer:
    """The load-bearing law."""

    def test_publish_is_not_a_coroutine(self, bus: EventBus) -> None:
        """
        An ``async def publish`` is the defect, not a style choice: it lets a
        caller await a consumer, and the first caller to do so puts a
        dashboard in the path of an order.
        """
        sub = bus.subscribe(["position"])
        result = bus.publish("position", {"symbol": "BTCUSDT"})

        assert result is None
        assert not asyncio.iscoroutine(result)
        assert len(sub._queue) == 1

    def test_publish_completes_with_no_running_event_loop(self, bus: EventBus) -> None:
        """
        Nothing in publish() may touch the loop. Running it outside one at all
        is the strongest available form of that assertion -- if it ever grows
        an await, a create_task or a lock, this raises instead of passing.
        """
        bus.subscribe(["gate"])

        with pytest.raises(RuntimeError):
            asyncio.get_running_loop()

        bus.publish("gate", {"blocked": True})
        assert bus.published == 1

    def test_a_subscriber_that_never_drains_cannot_stall_the_producer(self, bus: EventBus) -> None:
        """
        The starvation case, stated directly: a consumer that reads nothing at
        all still does not stop the producer publishing far past its buffer.
        """
        sub = bus.subscribe(["equity"])

        for i in range(sub.maxlen * 3):
            bus.publish("equity", {"n": i})

        assert bus.published == sub.maxlen * 3
        assert len(sub._queue) == sub.maxlen


class TestDropOldest:
    """A bounded buffer that blocks is backpressure wearing a hat."""

    def test_the_oldest_event_is_the_one_discarded(self) -> None:
        """
        Newest-wins matters for a dashboard: the operator wants the current
        price, not the price from when the tab was backgrounded.
        """
        bus = EventBus(maxlen=3)
        sub = bus.subscribe(["price"])

        for i in range(5):
            bus.publish("price", {"n": i})

        assert [event.data["n"] for event in sub._queue] == [2, 3, 4]

    def test_every_discarded_event_is_counted(self) -> None:
        """
        A starved client must be visible. Showing a confident dashboard built
        from a buffer that quietly lost frames is worse than showing nothing,
        because the operator cannot tell the difference.
        """
        bus = EventBus(maxlen=4)
        sub = bus.subscribe(["book"])

        for i in range(10):
            bus.publish("book", {"n": i})

        assert sub.dropped == 6
        assert bus.total_dropped == 6

    def test_one_slow_subscriber_does_not_evict_another(self) -> None:
        """
        Buffers are per subscriber. A shared one means the slowest client
        decides what every other client is allowed to see.
        """
        bus = EventBus(maxlen=2)
        slow = bus.subscribe(["order"])
        fast = bus.subscribe(["order"])

        for i in range(4):
            bus.publish("order", {"n": i})
            # The fast one keeps up; the slow one never reads.
            while fast._queue:
                fast._queue.popleft()

        assert slow.dropped == 2
        assert fast.dropped == 0


class TestPublishIsTotal:
    """A transport problem must never reach a risk gate."""

    def test_an_unknown_topic_is_dropped_and_counted_not_raised(self, bus: EventBus) -> None:
        """
        Raising here would turn a typo in a risk gate into a failed order.
        Counted and logged instead, so it is still loud.
        """
        bus.publish("postion", {"typo": True})  # codespell:ignore postion

        assert bus.unknown_topic_drops == 1
        assert bus.published == 0

    def test_publishing_with_no_subscribers_is_a_no_op(self, bus: EventBus) -> None:
        assert bus.subscriber_count == 0
        bus.publish("health", {"ok": True})
        assert bus.published == 1

    def test_a_subscriber_only_receives_its_own_topics(self, bus: EventBus) -> None:
        positions = bus.subscribe(["position"])
        prices = bus.subscribe(["price"])

        bus.publish("position", {"symbol": "ETHUSDT"})

        assert len(positions._queue) == 1
        assert len(prices._queue) == 0

    def test_subscribe_refuses_an_unknown_topic(self, bus: EventBus) -> None:
        """
        The asymmetry with publish() is deliberate. A publish comes from our
        own code on the trading path and must not be able to break it; a
        subscribe comes from a client and is input to be validated. PR 3 turns
        this into the websocket's refusal.
        """
        with pytest.raises(ValueError, match="unknown topic"):
            bus.subscribe(["position", "nonsense"])

    def test_closing_a_subscription_unregisters_it(self, bus: EventBus) -> None:
        sub = bus.subscribe(["intel"])
        assert bus.subscriber_count == 1

        sub.close()
        assert bus.subscriber_count == 0

        # And a publish after the fact still cannot fail.
        bus.publish("intel", {"provider": "dune", "healthy": False})
        assert bus.published == 1

    def test_close_is_idempotent(self, bus: EventBus) -> None:
        sub = bus.subscribe(["drift"])
        sub.close()
        sub.close()
        assert bus.subscriber_count == 0


class TestProducerTimestamp:
    def test_ts_ms_is_stamped_at_publish(self, bus: EventBus) -> None:
        """
        Carrying the producer's clock is the entire reason the field exists:
        stamping at send time reports zero lag no matter how starved the
        transport is.
        """
        sub = bus.subscribe(["signal"])
        bus.publish("signal", {"side": "long"}, ts_ms=1_700_000_000_123)

        event = sub._queue[0]
        assert event.ts_ms == 1_700_000_000_123
        assert event.topic == "signal"

    def test_ts_ms_defaults_to_now(self, bus: EventBus) -> None:
        sub = bus.subscribe(["regime"])
        bus.publish("regime", {"state": 1})

        assert sub._queue[0].ts_ms > 0


@pytest.mark.asyncio
class TestAsyncIteration:
    """Ordering and wakeup, driven by handshakes rather than the clock."""

    async def test_events_arrive_in_publication_order(self, bus: EventBus) -> None:
        sub = bus.subscribe(["position"])
        for i in range(3):
            bus.publish("position", {"n": i})
        sub.close()

        seen = [event.data["n"] async for event in sub]

        assert seen == [0, 1, 2]

    async def test_a_waiting_consumer_is_woken_by_a_publish(self, bus: EventBus) -> None:
        """
        The wakeup path, with no sleep anywhere: the consumer signals that it
        has reached the wait, the producer publishes, and the consumer's own
        completion is the assertion.
        """
        sub = bus.subscribe(["equity"])
        consuming = asyncio.Event()
        received: list[int] = []

        async def consume() -> None:
            consuming.set()
            async for event in sub:
                received.append(event.data["n"])
                break

        task = asyncio.create_task(consume())
        await consuming.wait()

        bus.publish("equity", {"n": 42})
        await asyncio.wait_for(task, timeout=5)

        assert received == [42]

    async def test_close_releases_a_blocked_consumer(self, bus: EventBus) -> None:
        """
        A consumer parked on an empty queue must be able to shut down. Without
        the wake in close(), a disconnecting websocket leaks its drain task
        for as long as the topic stays quiet.
        """
        sub = bus.subscribe(["health"])
        consuming = asyncio.Event()

        async def consume() -> list[Event]:
            consuming.set()
            return [event async for event in sub]

        task = asyncio.create_task(consume())
        await consuming.wait()

        sub.close()
        assert await asyncio.wait_for(task, timeout=5) == []

    async def test_a_publish_racing_the_clear_is_not_lost(self, bus: EventBus) -> None:
        """
        The clear-then-recheck in __aiter__. A publish landing between the
        drain and the clear would otherwise have its wakeup erased, stalling
        the consumer until the next event -- on a quiet topic, indistinguishable
        from a dropped connection.

        Simulated deterministically by clearing the wake flag underneath a
        subscription that already holds an event, which is the state that
        interleaving produces.
        """
        sub = bus.subscribe(["gate"])
        bus.publish("gate", {"blocked": True})
        sub._wake.clear()
        sub.close()

        seen = [event async for event in sub]

        assert len(seen) == 1


class TestTopicRegistry:
    def test_every_topic_the_plan_names_exists(self) -> None:
        """
        The topic set is a contract between producer and consumer. Losing one
        silently is a panel that stops updating with nothing in the logs.
        """
        assert {
            "position",
            "equity",
            "order",
            "approval",
            "signal",
            "regime",
            "gate",
            "killswitch",
            "capital_floor",
            "selftuning",
            "drift",
            "health",
            "intel",
            "price",
            "book",
        } == set(TOPICS)

    def test_the_cached_accessor_returns_one_instance(self) -> None:
        assert get_event_bus() is get_event_bus()


class TestSubscriptionContextManager:
    @pytest.mark.asyncio
    async def test_exiting_the_context_unregisters(self, bus: EventBus) -> None:
        async with bus.subscribe(["approval"]) as sub:
            assert isinstance(sub, Subscription)
            assert bus.subscriber_count == 1

        assert bus.subscriber_count == 0
