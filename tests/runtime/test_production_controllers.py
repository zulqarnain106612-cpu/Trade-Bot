"""RES-022: the production controllers do only what their subsystem owns.

* QUARANTINE on a strategy disables it through its kill switch -- LIVE_SAFE,
  executed at once, the same decision-log record and ``killswitch`` event as
  a drift trip -- and nothing in the runtime can re-enable it: only the
  gauntlet endpoint does, and that endpoint records the operator's intent so
  the loop does not re-quarantine it.
* A strategy without a kill switch, the live-model slot and every engine are
  observe-only: any action on them is FORBIDDEN.
* STOP discards a timeframe's shadow candidate in the signal engine; a
  superseded candidate or a held model lock is refused with nothing changed.
* PAUSE of self-tuning is LIVE_SAFE; RESUME waits for a human approver.
* A risk-reducing change is never undone because a probe calls the
  component unhealthy afterwards.

Decides: RES-022"""

from __future__ import annotations

import pytest

from src.api import runtime_control
from src.runtime.changes import (
    Actor,
    ChangeRecord,
    ChangeRequest,
    ChangeStatus,
    UnauthorizedError,
)
from src.runtime.contracts import ChangeClass, HealthReport, HealthState, LifecycleState
from src.runtime.platform import RuntimePlatform, build_runtime_platform
from src.runtime.production import RuntimeLoop, production_controllers, production_discoveries

from ._production import STRATEGY, World, orchestrator, signal_engine, with_shadow, world
from ._support import AI, HUMAN, A, build_platform, spec

S = LifecycleState


def _platform(w: World) -> tuple[RuntimePlatform, RuntimeLoop]:
    sources = w.sources()
    platform = build_runtime_platform(
        bus=w.bus,
        discoveries=production_discoveries(sources),
        controllers=production_controllers(sources),
    )
    return platform, RuntimeLoop(platform, lambda: production_discoveries(sources))


def _submit(platform: RuntimePlatform, cid: str, action: A, actor: Actor = HUMAN) -> ChangeRecord:
    return runtime_control._submit(platform, ChangeRequest(cid, action, actor, "test"))


async def test_quarantine_disables_the_strategy_through_its_kill_switch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    w = world(monkeypatch)
    platform, loop = _platform(w)
    change = _submit(platform, f"strategy:{STRATEGY}", A.QUARANTINE)
    assert (change.status, change.classification) == (ChangeStatus.PROMOTED, ChangeClass.LIVE_SAFE)
    assert not w.kill_switch.is_enabled(STRATEGY)
    assert w.decisions[-1]["change_type"] == "strategy_disabled"
    assert w.decisions[-1]["evidence"]["metric"] == "operator"
    record = platform.registry.get(f"strategy:{STRATEGY}")
    assert record.state is S.QUARANTINED
    assert record.desired is not None and record.desired.target_state is S.QUARANTINED
    # Re-enabling is not an action the runtime has: nothing gets it back out.
    for action in (A.START, A.ACTIVATE, A.RESUME):
        refused = _submit(platform, f"strategy:{STRATEGY}", action)
        assert refused.status is ChangeStatus.REJECTED
    loop.step()
    assert not w.kill_switch.is_enabled(STRATEGY)


async def test_a_gauntlet_re_enable_is_kept_not_reverted(monkeypatch: pytest.MonkeyPatch) -> None:
    w = world(monkeypatch)
    platform, loop = _platform(w)
    _submit(platform, f"strategy:{STRATEGY}", A.QUARANTINE)
    w.kill_switch.re_enable(STRATEGY, force=True)  # POST /strategies/{id}/re-enable
    problem = runtime_control.note_operator_intent(
        platform, f"strategy:{STRATEGY}", S.ACTIVE, "alice", "gauntlet"
    )
    assert problem is None
    outcomes = loop.step()
    assert outcomes == [] and w.kill_switch.is_enabled(STRATEGY)
    assert platform.registry.get(f"strategy:{STRATEGY}").state is S.ACTIVE
    assert runtime_control.note_operator_intent(None, "x:y", S.ACTIVE, "a", "b") is not None


async def test_observe_only_components_refuse_every_action(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    w = world(monkeypatch, strategies=(STRATEGY, "unswitched"), switched=(STRATEGY,))
    w.orch = orchestrator(monkeypatch, engines={"15m": signal_engine()}, crypto_box=True)
    platform, _ = _platform(w)
    for cid in ("strategy:unswitched", "model:15m/live", "engine:E-16", "engine:ensemble"):
        for action in (A.QUARANTINE, A.PAUSE, A.STOP, A.ACTIVATE):
            change = _submit(platform, cid, action)
            assert change.status is ChangeStatus.REJECTED, (cid, action)
            assert change.classification is ChangeClass.FORBIDDEN, (cid, action)


async def test_stop_discards_the_shadow_candidate_and_nothing_else(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    w = world(monkeypatch)
    engine = await with_shadow(signal_engine())
    w.orch = orchestrator(monkeypatch, engines={"15m": engine})
    platform, loop = _platform(w)
    change = _submit(platform, "model:15m/candidate/m-2", A.STOP)
    assert change.status is ChangeStatus.PROMOTED
    assert engine.shadow_model_id is None and engine.live_model_id == "initial"
    report = loop.sync_once()
    assert report.retired == ("model:15m/candidate/m-2",)
    assert platform.registry.find("model:15m/candidate/m-2") is None


async def test_a_superseded_candidate_or_a_held_lock_is_refused_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    w = world(monkeypatch)
    engine = await with_shadow(signal_engine())
    w.orch = orchestrator(monkeypatch, engines={"15m": engine})
    platform, _ = _platform(w)
    async with engine._model_lock:  # a tick mid-evaluation
        held = _submit(platform, "model:15m/candidate/m-2", A.STOP)
    assert held.status is ChangeStatus.FAILED and "model lock" in (held.result or "")
    assert platform.registry.get("model:15m/candidate/m-2").state is S.STANDBY
    assert engine.shadow_model_id == "m-2"
    await with_shadow(engine, "m-3")
    stale = _submit(platform, "model:15m/candidate/m-2", A.STOP)
    assert stale.status is ChangeStatus.FAILED and "no longer" in (stale.result or "")
    assert engine.shadow_model_id == "m-3"


async def test_pause_is_live_safe_and_resume_waits_for_a_human(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    w = world(monkeypatch)
    platform, _ = _platform(w)
    paused = _submit(platform, "tuning:self_tuning", A.PAUSE, AI)
    assert paused.status is ChangeStatus.PROMOTED and w.pause.paused
    assert w.pause_audit == [True]
    resume = _submit(platform, "tuning:self_tuning", A.RESUME, AI)
    assert (resume.status, resume.classification) == (
        ChangeStatus.AWAITING_APPROVAL,
        ChangeClass.LIVE_GATED,
    )
    with pytest.raises(UnauthorizedError):
        platform.changes.approve(resume.change_id, AI)
    assert w.pause.paused
    platform.changes.approve(resume.change_id, HUMAN)
    done = platform.changes.execute(resume.change_id, HUMAN)
    assert done.status is ChangeStatus.EXECUTED and not w.pause.paused  # under observation
    assert w.pause_audit == [True, False]


@pytest.mark.parametrize("action", [A.PAUSE, A.DEACTIVATE, A.QUARANTINE])
def test_a_risk_reduction_is_never_undone_on_health(action: A) -> None:
    p = build_platform()
    p.registry.register(spec("w"), observed_state=S.ACTIVE)
    p.controller.health = HealthReport(HealthState.UNHEALTHY, "out of service")
    submitted = p.changes.submit(ChangeRequest("worker:w", action, HUMAN, "take risk off"))
    done = p.changes.execute(submitted.change_id, HUMAN)
    assert done.status is ChangeStatus.PROMOTED
    assert p.registry.get("worker:w").state is not S.ACTIVE
    assert [c[1] for c in p.controller.calls] == [action]  # no inverse was run
    events = [e.event for e in p.changes.audit_log(submitted.change_id)]
    assert "unhealthy_kept" in events


def test_the_self_tuning_endpoints_record_the_operators_intent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """/self-tuning/pause and /resume change what the loop enforces: the act
    becomes the desired state, so the loop keeps it rather than reverting."""
    import os
    from unittest.mock import AsyncMock, patch

    from fastapi.testclient import TestClient

    from src.api import main

    w = world(monkeypatch)
    platform, _ = _platform(w)
    state = main.AppState()
    state.ready = True
    state.storage = AsyncMock()
    state.runtime = platform
    monkeypatch.setattr(main.tuning_audit_log, "record", lambda *args: None)
    monkeypatch.setattr(main.tuning_pause_state, "_paused", False)
    overrides = {
        main.api_key_header: lambda: None,
        main.resolve_role: lambda: main.Role.TRADE_AUTHORIZING,
        main.require_ready: lambda: None,
    }
    body = {"operator": "alice", "operator_secret": "s" * 32}
    with (
        patch.dict(os.environ, {"OPERATOR_SECRET": "s" * 32}),
        patch.object(main, "_state", state),
        patch.dict(main.app.dependency_overrides, overrides),
    ):
        client = TestClient(main.app, raise_server_exceptions=False)
        assert client.post("/self-tuning/pause", json=body).status_code == 200
        paused = platform.registry.get("tuning:self_tuning").desired
        assert client.post("/self-tuning/resume", json=body).status_code == 200
        resumed = platform.registry.get("tuning:self_tuning").desired
    assert paused is not None and (paused.target_state, paused.requested_by) == (S.PAUSED, "alice")
    assert resumed is not None and resumed.target_state is S.ACTIVE
    assert resumed.reason == "/self-tuning/resume"
