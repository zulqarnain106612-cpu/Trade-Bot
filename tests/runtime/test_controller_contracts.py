"""RES-022: each production controller, called directly, does only its one
operation and reports health from the subsystem it drives.

The change manager never hands a controller an action its component does
not declare; these tests hold the controllers to the same boundary on their
own, so a future caller that skips the change manager still cannot make a
kill switch, a signal engine or the pause switch do anything else. Refusals
(``ActionRefusedError``) leave the subsystem exactly as it was; the probes
map the subsystem's own state onto health. The subsystem entry points the
controllers use are covered here too: ``disable`` (no reason, already off),
``discard_shadow_now`` (nothing to discard) and ``set_paused_now`` (lock
held).

Decides: RES-022"""

from __future__ import annotations

import asyncio

import pytest

from src.runtime.contracts import (
    ComponentRecord,
    HealthState,
    LifecycleAction,
    UnsupportedActionError,
)
from src.runtime.controllers import (
    RoutingController,
    SelfTuningController,
    ShadowCandidateController,
    StrategyKillSwitchController,
)
from src.runtime.platform import RuntimePlatform, build_runtime_platform
from src.runtime.production import production_controllers, production_discoveries
from src.runtime.supervisor import ActionRefusedError, ObserveOnlyController

from ._production import STRATEGY, World, orchestrator, signal_engine, with_shadow, world

A = LifecycleAction


def _platform(w: World) -> RuntimePlatform:
    sources = w.sources()
    return build_runtime_platform(
        bus=w.bus,
        discoveries=production_discoveries(sources),
        controllers=production_controllers(sources),
    )


def _record(w: World, cid: str) -> ComponentRecord:
    return _platform(w).registry.get(cid)


def _kill_switch_controller(w: World) -> StrategyKillSwitchController:
    return StrategyKillSwitchController(w.strategies, w.kill_switch, clock_ms=lambda: 7)


@pytest.mark.parametrize("action", [A.STOP, A.PAUSE, A.ACTIVATE, A.RESUME])
def test_the_kill_switch_controller_does_nothing_but_quarantine(
    monkeypatch: pytest.MonkeyPatch, action: LifecycleAction
) -> None:
    w = world(monkeypatch)
    record = _record(w, f"strategy:{STRATEGY}")
    with pytest.raises(UnsupportedActionError):
        _kill_switch_controller(w).perform(record, action, None)
    assert w.kill_switch.is_enabled(STRATEGY) and w.decisions == []


def test_quarantine_is_refused_for_a_strategy_without_a_switch_or_gone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    w = world(monkeypatch, strategies=(STRATEGY, "loose"), switched=(STRATEGY,))
    platform = _platform(w)
    controller = _kill_switch_controller(w)
    loose = platform.registry.get("strategy:loose")
    with pytest.raises(ActionRefusedError, match="no kill switch"):
        controller.perform(loose, A.QUARANTINE, None)
    assert controller.probe(loose).state is HealthState.HEALTHY  # nothing can disable it
    trend = platform.registry.get(f"strategy:{STRATEGY}")
    w.strategies.unregister(STRATEGY)  # the owner dropped it
    with pytest.raises(ActionRefusedError, match="no longer a registered strategy"):
        controller.perform(trend, A.QUARANTINE, None)
    assert controller.probe(trend).state is HealthState.UNKNOWN
    assert w.kill_switch.is_enabled(STRATEGY) and w.decisions == []


def test_quarantine_disables_once_and_probes_the_switch(monkeypatch: pytest.MonkeyPatch) -> None:
    w = world(monkeypatch)
    record = _record(w, f"strategy:{STRATEGY}")
    controller = _kill_switch_controller(w)
    assert controller.probe(record).state is HealthState.HEALTHY
    controller.perform(record, A.QUARANTINE, None)
    controller.perform(record, A.QUARANTINE, None)  # already out: no second record
    assert len(w.decisions) == 1 and w.decisions[0]["evidence"]["disabled_at_ms"] == 7
    health = controller.probe(record)
    assert health.state is HealthState.UNHEALTHY
    assert health.detail == "quarantined through the runtime platform"


def test_the_kill_switch_refuses_a_disable_without_a_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    w = world(monkeypatch)
    with pytest.raises(ValueError, match="reason"):
        w.kill_switch.disable(STRATEGY, "  ")
    assert w.kill_switch.disable(STRATEGY, "first") is True
    assert w.kill_switch.disable(STRATEGY, "second") is False
    assert w.kill_switch.disabled_reason(STRATEGY) == "first"


async def test_the_shadow_controller_does_nothing_but_discard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    w = world(monkeypatch)
    engine = await with_shadow(signal_engine())
    w.orch = orchestrator(monkeypatch, engines={"15m": engine})
    record = _record(w, "model:15m/candidate/m-2")
    controller = ShadowCandidateController(lambda: {"15m": engine})
    for action in (A.ACTIVATE, A.REPLACE, A.QUARANTINE):
        with pytest.raises(UnsupportedActionError):
            controller.perform(record, action, None)
    assert engine.shadow_model_id == "m-2" and engine.live_model_id == "initial"
    assert controller.probe(record).detail == "shadow evaluation"


async def test_the_shadow_controller_refuses_a_timeframe_with_no_engine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    w = world(monkeypatch)
    engine = await with_shadow(signal_engine())
    w.orch = orchestrator(monkeypatch, engines={"15m": engine})
    record = _record(w, "model:15m/candidate/m-2")
    controller = ShadowCandidateController(dict)  # the orchestrator holds no engines
    with pytest.raises(ActionRefusedError, match="no signal engine"):
        controller.perform(record, A.STOP, None)
    health = controller.probe(record)
    assert health.state is HealthState.UNKNOWN and "no signal engine" in health.detail
    assert engine.shadow_model_id == "m-2"


async def test_the_shadow_probe_follows_the_candidate_into_the_live_slot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    w = world(monkeypatch)
    engine = await with_shadow(signal_engine())
    w.orch = orchestrator(monkeypatch, engines={"15m": engine})
    record = _record(w, "model:15m/candidate/m-2")
    # The signal engine's own promotion path records the swap in its registry.
    await engine.swap_models(engine._direction_model, engine._meta_model, None, model_id="m-2")
    health = ShadowCandidateController(lambda: {"15m": engine}).probe(record)
    assert (health.state, health.detail) == (HealthState.HEALTHY, "promoted to live")


def test_discarding_with_no_shadow_changes_nothing() -> None:
    engine = signal_engine()
    assert engine.shadow_model_id is None and engine.shadow_evaluations() == 0
    assert engine.discard_shadow_now("nothing there") is False
    assert engine.live_model_id == "initial"


@pytest.mark.parametrize("action", [A.STOP, A.ACTIVATE, A.QUARANTINE])
def test_the_self_tuning_controller_does_nothing_but_pause_and_resume(
    monkeypatch: pytest.MonkeyPatch, action: LifecycleAction
) -> None:
    w = world(monkeypatch)
    record = _record(w, "tuning:self_tuning")
    with pytest.raises(UnsupportedActionError):
        SelfTuningController(w.pause, w.pause_audit.append).perform(record, action, None)
    assert not w.pause.paused and w.pause_audit == []


async def test_a_held_pause_lock_is_refused_and_nothing_is_recorded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    w = world(monkeypatch)
    record = _record(w, "tuning:self_tuning")
    controller = SelfTuningController(w.pause, w.pause_audit.append)
    holder_entered = asyncio.Event()
    release = asyncio.Event()

    async def hold() -> None:
        async with w.pause._get_lock():
            holder_entered.set()
            await release.wait()

    task = asyncio.create_task(hold())
    await holder_entered.wait()
    try:
        with pytest.raises(ActionRefusedError, match="being changed"):
            controller.perform(record, A.PAUSE, None)
    finally:
        release.set()
        await task
    assert not w.pause.paused and w.pause_audit == []
    controller.perform(record, A.PAUSE, None)
    assert w.pause.paused and w.pause_audit == [True]
    assert controller.probe(record).detail.startswith("paused")


def test_routing_falls_back_to_observe_only(monkeypatch: pytest.MonkeyPatch) -> None:
    w = world(monkeypatch)
    record = _record(w, "eventbus:main")
    routing = RoutingController(lambda r: None)
    with pytest.raises(UnsupportedActionError):
        routing.perform(record, A.STOP, None)
    assert routing.probe(record) == ObserveOnlyController().probe(record) == record.health
