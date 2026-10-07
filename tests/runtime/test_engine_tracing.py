"""RES-024: every ensemble cycle leaves per-engine causal evidence without
touching the trading path.

One ``engine`` event per cycle carries every engine's outcome, latency and
vote, then consensus, the risk quantifier and the signal gate, under the
tick's trace_id -- so a decision trace shows the ``engines`` stage between
features and signal. Each engine's run record (EngineRunStats) is its runtime
health: a timeout or an exception is recorded by engine id, and three in a row
make it UNHEALTHY. Building or publishing that evidence can never raise into
the cycle.

Decides: RES-024"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path

import pytest
import structlog

import src.engines.orchestrator as engines_module
from src.diagnostics.decision_trace import STAGE_ORDER, DecisionTraceIndex
from src.engines.orchestrator import ENGINE_IDS, STAGE_IDS, EngineOrchestrator
from src.eventbus import Event, get_event_bus
from src.runtime.adapters import run_health
from src.runtime.contracts import HealthState


@pytest.fixture
def ensemble(tmp_path: Path) -> Iterator[EngineOrchestrator]:
    # Each cycle appends its outputs to a parquet audit file under data_root.
    yield EngineOrchestrator(data_root=tmp_path)


async def _cycle(orchestrator: EngineOrchestrator, trace_id: str) -> tuple[object, Event]:
    sub = get_event_bus().subscribe(["engine"])
    try:
        with structlog.contextvars.bound_contextvars(trace_id=trace_id):
            result = await orchestrator.run("BTC/USDT", {})
        event = await asyncio.wait_for(anext(aiter(sub)), timeout=1.0)
    finally:
        sub.close()
    return result, event


async def test_a_cycle_publishes_one_event_with_every_engine(ensemble: EngineOrchestrator) -> None:
    result, event = await _cycle(ensemble, "tick-1")
    assert event.topic == "engine" and event.context["trace_id"] == "tick-1"
    rows = event.data["engines"]
    assert [r["engine_id"] for r in rows] == list(ENGINE_IDS)  # positional, SIG-004
    assert all(isinstance(r["latency_ms"], float) for r in rows)
    assert event.data["failed"] == result.failed_engines
    assert event.data["signal"]["direction"] == result.trade_signal.direction
    assert set(event.data) >= {"consensus", "risk", "signal", "elapsed_ms"}
    index = DecisionTraceIndex()
    assert index.record(event)
    trace = index.trace("tick-1")
    assert trace is not None and trace.stages == ("engines",)
    assert STAGE_ORDER.index("features") < STAGE_ORDER.index("engines") < STAGE_ORDER.index(
        "signal"
    )


async def test_run_records_are_kept_per_engine_and_per_stage(
    ensemble: EngineOrchestrator,
) -> None:
    before = ensemble.engine_health()
    assert set(before) == {*ENGINE_IDS, *STAGE_IDS}
    assert run_health(before["E-01"]).state is HealthState.UNKNOWN
    await ensemble.run("BTC/USDT", {})
    after = ensemble.engine_health()
    assert {after[cid]["runs"] for cid in after} == {1}
    assert run_health(after["consensus"]).state is HealthState.HEALTHY


async def test_a_failing_engine_degrades_then_turns_unhealthy(
    ensemble: EngineOrchestrator, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def broken(symbol: str, data: dict[str, object]) -> object:
        raise ValueError("bad input")

    monkeypatch.setattr(ensemble._engines[15], "run", broken)  # E-16
    states = []
    for _ in range(3):
        result = await ensemble.run("BTC/USDT", {})
        assert "E-16" in result.failed_engines  # degraded, never raised
        states.append(run_health(ensemble.engine_health()["E-16"]).state)
    assert states == [HealthState.DEGRADED, HealthState.DEGRADED, HealthState.UNHEALTHY]
    record = ensemble.engine_health()["E-16"]
    assert record["last_error"] == "ValueError: bad input" and record["consecutive_failures"] == 3


async def test_a_timeout_is_recorded_as_one(
    ensemble: EngineOrchestrator, monkeypatch: pytest.MonkeyPatch
) -> None:
    never = asyncio.Event()

    async def hangs(symbol: str, data: dict[str, object]) -> object:
        await never.wait()
        raise AssertionError("unreachable")

    monkeypatch.setitem(engines_module._ENGINE_SLAS, "E-01", 0.0)
    monkeypatch.setattr(ensemble._engines[0], "run", hangs)
    result = await ensemble.run("BTC/USDT", {})
    assert "E-01" in result.failed_engines
    assert ensemble.engine_health()["E-01"]["last_error"] == "timeout"


async def test_unbuildable_evidence_never_reaches_the_cycle(
    ensemble: EngineOrchestrator, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = ensemble._risk.quantify

    def quantify(**kwargs: object) -> dict[str, object]:
        risk = dict(original(**kwargs))
        risk["tail_risk_score"] = object()  # float() of it raises inside the payload
        return risk

    monkeypatch.setattr(ensemble._risk, "quantify", quantify)
    monkeypatch.setattr(engines_module, "consensus_to_signal", _signal_ignoring_tail_risk)
    sub = get_event_bus().subscribe(["engine"])
    try:
        result = await ensemble.run("BTC/USDT", {})
        assert len(sub._queue) == 0  # evidence dropped
    finally:
        sub.close()
    assert result.trade_signal is not None  # the cycle's signal is intact


def _signal_ignoring_tail_risk(**kwargs: object) -> object:
    from src.engines.signal_gate import consensus_to_signal

    return consensus_to_signal(**{**kwargs, "tail_risk": 0.0})
