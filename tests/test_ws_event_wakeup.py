"""
Seam contract for GOV-032: /ws consumes the bus.

The bus and ``Subscription.drain`` are covered behaviourally in
tests/test_event_bus.py. What that cannot catch is the seam coming
undone: a future edit restoring ``await asyncio.sleep(heartbeat)``, or
dropping the ``unsubscribe`` in ``finally``, leaves every bus test green
while the GUI silently returns to being a timer -- or leaks a
subscription per disconnect until the bus fans out to dead queues.

Asserted against the source of the endpoint rather than by driving a
socket: importing and running the API for this costs seconds per run
(GOV-016) and would assert the same three facts less directly.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

API_MAIN = Path(__file__).resolve().parent.parent / "src" / "api" / "main.py"


@pytest.fixture(scope="module")
def ws_endpoint_source() -> str:
    """Parsed once for the module -- the file is large and read-only here."""
    # GOV-038: the send loop moved out of the per-connection endpoint into
    # one process-wide broadcaster, so that is where this seam now lives.
    tree = ast.parse(API_MAIN.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_tick_broadcaster":
            return ast.unparse(node)
    pytest.fail("_tick_broadcaster not found in src/api/main.py")


def test_the_loop_waits_on_the_bus(ws_endpoint_source: str) -> None:
    assert "events.drain(heartbeat)" in ws_endpoint_source


def test_the_loop_does_not_sleep_the_heartbeat(ws_endpoint_source: str) -> None:
    """
    The defect this change removes: sleeping first means a fill landing one
    millisecond later is invisible for the rest of the period.
    """
    assert "asyncio.sleep(heartbeat)" not in ws_endpoint_source


def test_the_subscription_is_released_on_disconnect(ws_endpoint_source: str) -> None:
    """
    A leaked subscription is not inert: publish() fans out to its queue on
    every event for the life of the process.
    """
    assert "bus.unsubscribe(events)" in ws_endpoint_source


def test_the_subscription_is_topic_scoped(ws_endpoint_source: str) -> None:
    """
    Not a wildcard: a socket that receives every topic wakes on traffic it
    does not render, which is the timer's cost paid a different way.
    """
    assert "bus.subscribe([PORTFOLIO, RISK])" in ws_endpoint_source


@pytest.mark.parametrize("module", ["src.execution.paper", "src.execution.live"])
def test_both_executors_publish_after_persisting_equity(module: str) -> None:
    """
    SIG-004: a producer/consumer pair is only a contract if both ends are
    asserted. A consumer parked on the bus with nobody publishing is the
    same dead-in-production shape as src/data/orderbook_stream.py.

    The publish must follow the insert: the values are captured inside the
    lock and persisted first, so a consumer that reacts by reading storage
    cannot observe a state older than the event that woke it.
    """
    import importlib

    source = inspect.getsource(importlib.import_module(module))
    insert = source.index("await self._storage.insert_equity(record)")
    publish = source.index("get_event_bus().publish(")
    assert insert < publish, "publish must follow the persisted snapshot"
    assert "PORTFOLIO," in source[publish : publish + 200]


def test_the_risk_topic_has_a_producer() -> None:
    """
    /ws subscribes to RISK, so a RISK topic nobody publishes is a socket
    parked on silence -- the same dead-in-production shape as
    src/data/orderbook_stream.py, and the reason kill-switch trips were
    one of the two things the dashboard never saw at all.

    The publish must follow the state change: a consumer must not be able
    to observe the trip before capital has been pulled.
    """
    import importlib

    source = inspect.getsource(importlib.import_module("src.risk.strategy_kill_switch"))
    disabled = source.index("state.enabled = False")
    publish = source.index("get_event_bus().publish(")
    assert disabled < publish, "publish must follow the disable"
    assert "RISK," in source[publish : publish + 120]
    assert '"event": "strategy_disabled"' in source[publish : publish + 300]
