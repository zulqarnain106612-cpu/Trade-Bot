"""One tick's log lines must be linkable to each other, and not to another's.

Everything a tick reaches logs through structlog, and nothing tied those
lines together: reconstructing which order came from which signal meant
reading timestamps and guessing. The timeframe loops run concurrently, so
the guess is wrong exactly when it matters -- when two ticks overlap.

These tests drive _tick() with a stubbed body, because what is being pinned
is the binding and its isolation, not the signal cycle.
"""

from __future__ import annotations

import asyncio

import pytest
import structlog

from src.config import Timeframe


@pytest.fixture(autouse=True)
def _clean_context():
    structlog.contextvars.clear_contextvars()
    yield
    structlog.contextvars.clear_contextvars()


class _StubOrchestrator:
    """Only the two methods under test, lifted from Orchestrator."""

    def __init__(self) -> None:
        from src.engine.orchestrator import Orchestrator

        self._tick = Orchestrator._tick.__get__(self)
        self.seen: list[dict] = []

    async def _tick_traced(self, tf: Timeframe) -> None:
        self.seen.append(dict(structlog.contextvars.get_contextvars()))


async def test_tick_binds_a_trace_id_and_timeframe() -> None:
    orch = _StubOrchestrator()

    await orch._tick(Timeframe.INTRADAY)

    (bound,) = orch.seen
    assert bound["timeframe"] == Timeframe.INTRADAY.value
    assert len(bound["trace_id"]) == 16


async def test_each_tick_gets_its_own_trace_id() -> None:
    orch = _StubOrchestrator()

    await orch._tick(Timeframe.INTRADAY)
    await orch._tick(Timeframe.INTRADAY)

    first, second = orch.seen
    assert first["trace_id"] != second["trace_id"]


async def test_concurrent_ticks_do_not_share_a_trace_id() -> None:
    """The case the guessing-from-timestamps approach got wrong."""

    class _Overlapping(_StubOrchestrator):
        async def _tick_traced(self, tf: Timeframe) -> None:
            # Yield inside the tick so both are in flight simultaneously.
            await asyncio.sleep(0)
            self.seen.append(dict(structlog.contextvars.get_contextvars()))

    orch = _Overlapping()

    await asyncio.gather(
        orch._tick(Timeframe.INTRADAY),
        orch._tick(Timeframe.SWING),
    )

    ids = {s["trace_id"] for s in orch.seen}
    timeframes = {s["timeframe"] for s in orch.seen}
    assert len(ids) == 2, "concurrent ticks shared a trace id"
    assert timeframes == {Timeframe.INTRADAY.value, Timeframe.SWING.value}


async def test_tick_unbinds_even_when_the_body_raises() -> None:
    class _Boom(_StubOrchestrator):
        async def _tick_traced(self, tf: Timeframe) -> None:
            raise RuntimeError("boom")

    orch = _Boom()

    with pytest.raises(RuntimeError, match="boom"):
        await orch._tick(Timeframe.INTRADAY)

    assert "trace_id" not in structlog.contextvars.get_contextvars()


async def test_tick_leaves_an_outer_binding_alone() -> None:
    """Unbind, not clear: an outer scope's context must survive the tick."""
    orch = _StubOrchestrator()
    structlog.contextvars.bind_contextvars(deployment="paper")

    await orch._tick(Timeframe.INTRADAY)

    assert structlog.contextvars.get_contextvars()["deployment"] == "paper"
