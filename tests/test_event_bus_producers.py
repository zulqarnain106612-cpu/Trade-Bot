"""
INV-032, producer half -- the trading path publishes, and publishing cannot
hurt it.

A bus with no producers is an empty pipe that still passes all its own tests,
so this is the half that proves anything actually reaches the dashboard. The
risk gate is the one pinned here because it is both the most valuable and the
most dangerous: most valuable because a block had no push path at all before
(it reached the operator only via a 30s poll of /debug/audit), and most
dangerous because it sits directly on the order path, where an exception
escaping the publish would turn a dashboard fault into a failed trade.

evaluate_all_gates is synchronous, which is the reason publish() had to be
synchronous too. A test asserting that is a test asserting the design.
"""

from __future__ import annotations

import pytest

from src.config import TradingMode
from src.eventbus import EventBus, get_event_bus
from src.risk.gates import RiskGateContext, evaluate_all_gates


@pytest.fixture
def bus(monkeypatch) -> EventBus:
    """
    Swap the cached process bus for a fresh one.

    monkeypatch on the lru_cache rather than cache_clear(): clearing would
    leak a new global into whatever ran next, which REG-0005 exists to stop.
    """
    replacement = EventBus()
    monkeypatch.setattr("src.risk.gates.get_event_bus", lambda: replacement)
    return replacement


def _ctx(**overrides) -> RiskGateContext:
    """A context that passes every gate unless an override breaks one."""
    base = {
        "capital_preservation_halted": False,
        "expected_edge_bps": 50.0,
        "slippage_estimate": None,
        "daily_pnl_usd": 0.0,
        "starting_equity_usd": 10_000.0,
        "consecutive_loss_count": 0,
        "regime_state": 1,
        "notional_usd": 100.0,
        "capital_usd": 10_000.0,
        "trading_mode": TradingMode.PAPER,
        "paper_trading_days": 0,
        "direction_gate_pass": True,
        "meta_gate_pass": True,
    }
    base.update(overrides)
    return RiskGateContext(**base)


class TestGateBlocksArePublished:
    def test_a_block_reaches_the_bus(self, bus: EventBus) -> None:
        """
        The defect this closes: a risk gate blocking a trade was the single
        most operator-relevant thing the system did and the slowest to
        surface -- 30s of polling /debug/audit, if anyone thought to look.
        """
        subscription = bus.subscribe(["gate"])

        result = evaluate_all_gates(_ctx(capital_preservation_halted=True))

        assert not result.passed
        assert len(subscription._queue) == 1
        event = subscription._queue[0]
        assert event.topic == "gate"
        assert event.data["passed"] is False
        assert event.data["reason"]

    def test_a_clean_pass_publishes_nothing(self, bus: EventBus) -> None:
        """
        Deliberately silent. A pass happens on every tick of every timeframe,
        so publishing one would flood the bus with the least interesting fact
        it carries and push the events that matter out of every subscriber's
        buffer. Absence of news is the heartbeat's job.
        """
        subscription = bus.subscribe(["gate"])

        result = evaluate_all_gates(_ctx())

        assert result.passed
        assert len(subscription._queue) == 0

    def test_the_event_carries_which_gate_blocked(self, bus: EventBus) -> None:
        """A block with no attributable gate is not actionable."""
        subscription = bus.subscribe(["gate"])

        evaluate_all_gates(_ctx(consecutive_loss_count=99))

        assert subscription._queue[0].data["status"]


class TestPublishingCannotBreakTheGate:
    def test_a_broken_subscriber_does_not_reach_the_gate(self, monkeypatch) -> None:
        """
        The load-bearing guarantee, at the seam that matters most: a consumer
        that has gone wrong must not turn a risk evaluation on the order path
        into a failed one.

        The failure is injected *inside* a real EventBus -- a subscriber whose
        delivery raises -- rather than by swapping the bus for one that
        throws. Swapping it would bypass publish()'s own guard and prove
        nothing about the production path, which is precisely that guard.
        """
        real = EventBus()
        broken = real.subscribe(["gate"])

        def _explode(_event):
            raise RuntimeError("subscriber is on fire")

        monkeypatch.setattr(broken, "_offer", _explode)
        monkeypatch.setattr("src.risk.gates.get_event_bus", lambda: real)

        result = evaluate_all_gates(_ctx(capital_preservation_halted=True))

        # The gate reached its verdict regardless.
        assert not result.passed
        assert result.reason

    def test_the_real_bus_swallows_a_bad_payload(self) -> None:
        """
        The production path: EventBus.publish is total, so nothing a producer
        hands it can escape into the caller.
        """
        real = EventBus()
        real.subscribe(["gate"])

        real.publish("gate", {"unserializable": object()})

        assert real.published == 1


class TestTheProcessBusIsShared:
    def test_producers_and_the_api_reach_the_same_instance(self) -> None:
        """
        A producer publishing into one bus while the websocket drains another
        is a silent no-op that looks exactly like a quiet market.
        """
        from src.eventbus.bus import get_event_bus as bus_module_accessor

        assert get_event_bus() is bus_module_accessor()
