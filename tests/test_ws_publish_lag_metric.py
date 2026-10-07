"""
API-010 -- the number that decides whether any of this worked.

Every other test in this effort asserts that a frame goes out. None of them
can say how long it took, and "realtime" without a measured lag is a claim
about the code rather than an observation of the running system: a starved
socket, a wedged fan-out task and a healthy push path all look identical from
the outside, and the dashboard says "connected" through all three.

So the lag is measured at the one place that knows both ends -- the fan-out
loop, which holds the producer's own timestamp and has just finished writing
to the sockets -- and it is measured *after* the write, because the write to a
slow peer is the failure worth catching.
"""

from __future__ import annotations

import time

import pytest

from src.eventbus import EventBus

_COUNT = "tradebot_ws_publish_lag_seconds_count"
_SUM = "tradebot_ws_publish_lag_seconds_sum"


def _sample(name: str, topic: str) -> float:
    """
    One histogram sample, or 0.0 before anything has been observed.

    Read as a delta around the call under test rather than an absolute: the
    registry is process-wide by design (a per-test registry would not be the
    one /metrics serves), so another test having already observed this topic
    must not be able to change the answer here.
    """
    from src.diagnostics.metrics import _REGISTRY

    return _REGISTRY.get_sample_value(name, {"topic": topic}) or 0.0


class _FakeWS:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send_text(self, message: str) -> None:
        self.sent.append(message)


class TestObserveWsPublishLag:
    def test_an_observation_lands_under_its_topic(self) -> None:
        from src.diagnostics.metrics import observe_ws_publish_lag

        before = _sample(_COUNT, "equity")
        observe_ws_publish_lag("equity", time.monotonic_ns() - 250_000_000)

        assert _sample(_COUNT, "equity") == before + 1

    def test_the_recorded_lag_is_the_age_of_the_event(self) -> None:
        from src.diagnostics.metrics import observe_ws_publish_lag

        before = _sample(_SUM, "position")
        observe_ws_publish_lag("position", time.monotonic_ns() - 500_000_000)
        observed = _sample(_SUM, "position") - before

        # Half a second, plus however long this test took to get here. An
        # exact assertion would be a test of the clock; the point is that the
        # magnitude comes from the event's own stamp and not from zero.
        assert 0.4 <= observed < 2.0

    def test_a_topic_is_measured_separately_from_every_other(self) -> None:
        """
        Labelled by topic because the topics fail differently: `book` is
        coalesced at 250ms and `capital_floor` fires once in the life of an
        incident, and a single unlabelled histogram lets the fast, chatty
        topic bury the slow one that matters.
        """
        from src.diagnostics.metrics import observe_ws_publish_lag

        before_gate = _sample(_COUNT, "gate")
        before_book = _sample(_COUNT, "book")
        observe_ws_publish_lag("gate", time.monotonic_ns())

        assert _sample(_COUNT, "gate") == before_gate + 1
        assert _sample(_COUNT, "book") == before_book

    def test_an_event_carries_a_separate_stamp_for_the_duration(self) -> None:
        """
        Why the event carries two clocks rather than one.

        The frame needs wall clock: the browser has nothing else to compare
        against. The histogram needs monotonic: wall clock moves under a
        running process -- NTP corrects it, an operator sets it -- and a
        duration taken across that step reads negative or hours long, which is
        indistinguishable from the starvation the metric exists to find. Both
        are stamped at publish, so they describe the same instant.
        """
        bus = EventBus()
        subscription = bus.subscribe(["regime"])
        before_mono = time.monotonic_ns()
        before_wall_ms = int(time.time() * 1000)

        bus.publish("regime", {"state": "trending"})
        event = subscription._queue[0]

        assert before_mono <= event.mono_ns <= time.monotonic_ns()
        assert event.ts_ms >= before_wall_ms

    def test_a_broken_metric_does_not_raise_into_the_fanout(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        The fan-out loop is the dashboard's only push path. A metrics bug that
        escaped this call would take it down, which trades the thing being
        measured for the measurement -- so this fails closed on the metric and
        open on the transport, exactly as update_metrics does.
        """
        from src.diagnostics import metrics

        class _Broken:
            def labels(self, **_kwargs: object) -> object:
                raise RuntimeError("collector is wedged")

        monkeypatch.setattr(metrics, "ws_publish_lag_seconds", _Broken())

        metrics.observe_ws_publish_lag("health", int(time.time() * 1000))


class TestTheFanoutLoopMeasuresItself:
    async def test_draining_one_event_records_one_observation(self) -> None:
        """
        Drives the real loop rather than asserting the call exists: the loop
        skips the send entirely when nobody is connected, and a lag recorded
        on a frame that was never written would report a healthy transport for
        a dashboard receiving nothing.
        """
        from src.api import main as api_main

        bus = EventBus()
        subscription = bus.subscribe(["equity"])
        client = _FakeWS()
        api_main._state._ws_clients.add(client)
        api_main._state._ws_topics[client] = frozenset({"equity"})
        before = _sample(_COUNT, "equity")
        try:
            bus.publish("equity", {"equity_usd": 1234.5})
            # Closed before draining, so the iterator yields what is queued
            # and then finishes. No sleep, no timeout, no scheduler race
            # (GOV-016).
            subscription.close()
            await api_main._event_fanout_loop(subscription)
        finally:
            api_main._state._ws_clients.discard(client)
            api_main._state._ws_topics.pop(client, None)

        assert len(client.sent) == 1
        assert _sample(_COUNT, "equity") == before + 1

    async def test_nothing_is_recorded_when_nobody_is_connected(self) -> None:
        """
        No send, no measurement. A lag recorded for a frame that was never
        written would report a healthy transport on an idle server, and the
        histogram would look best exactly when nobody could see anything.
        """
        from src.api import main as api_main

        bus = EventBus()
        subscription = bus.subscribe(["killswitch"])
        before = _sample(_COUNT, "killswitch")
        # Emptied rather than assumed empty: _state is module-level, so a test
        # elsewhere that left a client registered would otherwise decide this
        # one's result.
        registered = set(api_main._state._ws_clients)
        api_main._state._ws_clients.clear()
        try:
            bus.publish("killswitch", {"strategy": "breakout", "halted": True})
            subscription.close()
            await api_main._event_fanout_loop(subscription)
        finally:
            api_main._state._ws_clients.update(registered)

        assert _sample(_COUNT, "killswitch") == before
