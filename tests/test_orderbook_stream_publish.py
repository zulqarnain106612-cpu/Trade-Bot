"""
INV-032 at the fastest producer -- the sub-second price path.

src/data/orderbook_stream.py was a working Binance depth + aggTrade client
that nothing in src/ imported: the one genuine sub-second price source in the
tree fed nothing while mark-to-market waited on a 5s REST poll. Wiring it into
the runtime is what makes its costs real, and these tests pin the two things
that wiring must not get wrong.

  coalescing   the raw feed is 100ms. Unthrottled, every message would build
               a DataFrame for the provider cache and push a frame to every
               connected dashboard -- ten times a second, multiplied by the
               client count. That is the starvation the drop-oldest law
               exists to prevent, arriving through the front door.
  staleness    a disconnected socket leaves the last snapshot sitting there
               looking exactly like a price. Marking a position against a
               four-minute-old number is worse than not marking it: it is
               wrong with nothing to say so.

No sleeps (GOV-016): the clock is injected.
"""

from __future__ import annotations

import pytest

from src.data.orderbook_stream import OrderbookStream
from src.eventbus import EventBus


@pytest.fixture
def clock(monkeypatch):
    """A monotonic clock the test advances by hand."""

    class _Clock:
        now = 1000.0

        def advance(self, seconds: float) -> None:
            self.now += seconds

    c = _Clock()
    monkeypatch.setattr("src.data.orderbook_stream.time.monotonic", lambda: c.now)
    return c


@pytest.fixture
def bus(monkeypatch) -> EventBus:
    replacement = EventBus()
    monkeypatch.setattr("src.data.orderbook_stream.get_event_bus", lambda: replacement)
    return replacement


def _depth(bid: float, ask: float) -> dict:
    return {"bids": [[str(bid), "1.0"]] * 20, "asks": [[str(ask), "1.0"]] * 20}


@pytest.fixture
def stream(tmp_path, monkeypatch):
    s = OrderbookStream(symbol="btcusdt", data_root=tmp_path)
    # The provider cache is a separate concern with its own global; this test
    # is about what reaches the bus.
    monkeypatch.setattr(
        "src.data.provider_cache.get_provider_cache",
        lambda: type("_C", (), {"set_orderbook": lambda *a, **k: None})(),
    )
    return s


class TestCoalescing:
    def test_a_burst_of_messages_publishes_once(self, stream, bus, clock) -> None:
        """
        Ten messages inside one interval is what the real 100ms feed delivers
        in a quarter second. It must cost one fan-out, not ten.
        """
        sub = bus.subscribe(["price"])

        for i in range(10):
            stream._handle_depth(_depth(100.0 + i, 101.0 + i))

        assert len(sub._queue) == 1

    def test_the_next_interval_publishes_again(self, stream, bus, clock) -> None:
        sub = bus.subscribe(["price"])

        stream._handle_depth(_depth(100.0, 101.0))
        clock.advance(0.3)  # past the 250ms interval
        stream._handle_depth(_depth(200.0, 201.0))

        assert len(sub._queue) == 2
        assert sub._queue[-1].data["mid"] == pytest.approx(200.5)

    def test_every_message_is_still_recorded(self, stream, bus, clock) -> None:
        """
        Coalescing throttles the *fan-out*, not the record. The parquet file
        and the engines that read it depend on the full stream, so dropping
        snapshots to save a broadcast would quietly degrade E-02 and E-16/17.
        """
        for i in range(10):
            stream._handle_depth(_depth(100.0 + i, 101.0 + i))

        assert len(stream._snapshots) == 10


class TestPublishedShape:
    def test_price_and_book_both_go_out(self, stream, bus, clock) -> None:
        prices = bus.subscribe(["price"])
        books = bus.subscribe(["book"])

        stream._handle_depth(_depth(100.0, 101.0))

        assert len(prices._queue) == 1
        assert len(books._queue) == 1
        assert prices._queue[0].data["mid"] == pytest.approx(100.5)

    def test_the_book_is_truncated(self, stream, bus, clock) -> None:
        """
        Twenty levels multiplied by every connected client is bytes spent on
        depth nobody is rendering.
        """
        books = bus.subscribe(["book"])

        stream._handle_depth(_depth(100.0, 101.0))

        data = books._queue[0].data
        assert len(data["bids"]) == 5
        assert len(data["asks"]) == 5

    def test_a_wide_spread_is_rejected_before_publishing(self, stream, bus, clock) -> None:
        """The existing quality gate must still bite; it now also gates the bus."""
        sub = bus.subscribe(["price"])

        stream._handle_depth(_depth(100.0, 500.0))

        assert len(sub._queue) == 0


class TestStaleness:
    def test_a_fresh_mid_is_returned(self, stream, bus, clock) -> None:
        stream._handle_depth(_depth(100.0, 101.0))

        assert stream.latest_mid() == pytest.approx(100.5)

    def test_a_stale_mid_is_withheld(self, stream, bus, clock) -> None:
        """
        The caller falls back to REST on None. Returning the old number
        instead would mark positions against a price the market left minutes
        ago, with nothing anywhere saying so.
        """
        stream._handle_depth(_depth(100.0, 101.0))
        clock.advance(11.0)  # past the 10s default bound

        assert stream.latest_mid() is None

    def test_no_data_is_not_an_error(self, stream, bus, clock) -> None:
        assert stream.latest_mid() is None

    def test_the_bound_is_configurable(self, stream, bus, clock) -> None:
        stream._handle_depth(_depth(100.0, 101.0))
        clock.advance(11.0)

        assert stream.latest_mid(max_age_s=30.0) == pytest.approx(100.5)


class TestPublishingCannotBreakTheStream:
    def test_a_broken_subscriber_does_not_reach_the_feed(self, stream, bus, clock) -> None:
        """
        Same law as every other producer: the market data path must not learn
        that a dashboard has gone wrong.
        """
        broken = bus.subscribe(["price"])
        broken._offer = lambda _event: (_ for _ in ()).throw(RuntimeError("boom"))

        stream._handle_depth(_depth(100.0, 101.0))

        assert len(stream._snapshots) == 1
        assert stream.latest_mid() == pytest.approx(100.5)
