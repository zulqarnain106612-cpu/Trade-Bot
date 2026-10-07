"""RES-023: desired state is enforced continuously, through the change
manager, across a restart.

* The loop's first pass runs inside start_runtime_platform, before the
  orchestrator's first tick: a strategy quarantine and a self-tuning pause
  stored before a restart (real SQLite in tmp_path) are back in force when it
  returns.
* An intent stored for a component that registers after startup is applied
  when it appears, not dropped.
* A gated step is queued once (AWAITING_APPROVAL) and waits; an unreachable
  or rejected mismatch is alerted once and not resubmitted every pass, but is
  retried once it changes or after the retry interval.
* A discovery that raises does not stop the loop; ``run`` exits on its stop
  event, flushing what it queued.

Decides: RES-023"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import patch

import pytest

from src.data.storage import StorageBackend
from src.runtime.changes import ChangeRequest, ChangeStatus
from src.runtime.contracts import LifecycleState
from src.runtime.platform import RuntimePlatform, build_runtime_platform
from src.runtime.production import RuntimeLoop, production_controllers, production_discoveries
from src.runtime.reconcile import Reconciler
from src.tuning.registry import TunableParameter

from ._production import STRATEGY, World, world
from ._support import HUMAN, REQUESTER, A, build_platform, spec

S = LifecycleState
PARAM = "hmm.entropy_threshold"


def _request(cid: str, action: A) -> ChangeRequest:
    return ChangeRequest(cid, action, HUMAN, "operator")


async def _start(w: World, storage: StorageBackend) -> RuntimePlatform:
    """src.api.main.start_runtime_platform over ``w`` and a real backend."""
    from src.api import main

    state = main.AppState()
    state.storage = storage
    sources = w.sources()
    with (
        patch.object(main, "_state", state),
        patch.object(main, "runtime_sources", lambda: sources),
    ):
        platform = await main.start_runtime_platform()
    assert platform is not None
    return platform


async def test_a_quarantine_and_a_pause_survive_a_restart(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    storage = StorageBackend(str(tmp_path / "runtime.db"))
    await storage.initialize()
    try:
        before = world(monkeypatch)
        platform = await _start(before, storage)
        changes = platform.changes
        for cid, action in (
            (f"strategy:{STRATEGY}", A.QUARANTINE),
            ("tuning:self_tuning", A.PAUSE),
        ):
            submitted = changes.submit(_request(cid, action))
            assert changes.execute(submitted.change_id, HUMAN).status is ChangeStatus.PROMOTED
        await platform.flush()

        # The process restarts: a fresh kill switch (enabled) and pause switch.
        after = world(monkeypatch)
        assert after.kill_switch.is_enabled(STRATEGY) and not after.pause.paused
        restarted = await _start(after, storage)
        assert not after.kill_switch.is_enabled(STRATEGY)
        assert after.pause.paused
        assert restarted.registry.get(f"strategy:{STRATEGY}").state is S.QUARANTINED
        reconciler_changes = [
            c for c in restarted.changes.changes() if c.request.actor.name == "reconciler"
        ]
        assert {c.status for c in reconciler_changes} == {ChangeStatus.PROMOTED}
    finally:
        await storage.close()


class _Store:
    def __init__(self, rows: dict[str, dict[str, object]]) -> None:
        self.rows = rows

    async def upsert_runtime_desired_state(self, cid: str, desired: object, ms: int) -> None:
        if desired is None:
            self.rows.pop(cid, None)
        else:
            self.rows[cid] = desired  # type: ignore[assignment]

    async def fetch_runtime_desired_states(self) -> dict[str, dict[str, object]]:
        return dict(self.rows)


async def test_an_intent_for_a_late_component_is_applied_when_it_appears(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    w = world(monkeypatch)
    stored = {
        f"tuning:{PARAM}": {
            "target_state": "ACTIVE",
            "target_version": None,
            "requested_by": "alice",
            "reason": "keep",
            "requested_at": "2026-10-07T00:00:00+00:00",
        }
    }
    sources = w.sources()
    platform = build_runtime_platform(
        bus=w.bus,
        discoveries=production_discoveries(sources),
        controllers=production_controllers(sources),
        desired_backend=_Store(stored),
    )
    problems = await platform.restore()
    assert problems == [f"tuning:{PARAM}: stored desired state for an unknown component"]
    assert platform.persister is not None and platform.persister.deferred == (f"tuning:{PARAM}",)
    w.parameters.register(TunableParameter(PARAM, "t", 0.1, 0.9, 0.5, "cpcv_oos_sharpe"))
    RuntimeLoop(platform, lambda: production_discoveries(sources)).step()
    desired = platform.registry.get(f"tuning:{PARAM}").desired
    assert desired is not None and desired.requested_by == "alice"
    assert platform.persister.deferred == ()


async def test_a_gated_step_is_queued_once_and_waits(monkeypatch: pytest.MonkeyPatch) -> None:
    w = world(monkeypatch)
    w.pause.set_paused_now(True)
    sources = w.sources()
    platform = build_runtime_platform(
        bus=w.bus,
        discoveries=production_discoveries(sources),
        controllers=production_controllers(sources),
    )
    loop = RuntimeLoop(platform, lambda: production_discoveries(sources))
    platform.changes.request_desired("tuning:self_tuning", S.ACTIVE, REQUESTER, "resume")
    first = loop.step()
    assert [o.outcome for o in first] == ["awaiting approval"]
    for _ in range(3):
        (outcome,) = loop.step()
        assert outcome.outcome == "pending: AWAITING_APPROVAL"
    assert len(platform.changes.changes("tuning:self_tuning")) == 1
    assert w.pause.paused  # nobody approved


def test_an_alerted_mismatch_is_not_resubmitted_until_it_changes() -> None:
    p = build_platform()
    p.registry.register(spec("w"), observed_state=S.ACTIVE)
    p.changes.request_desired("worker:w", S.PAUSED, REQUESTER, "pause it")
    p.controller.fail_with = RuntimeError("refused by the subsystem")
    reconciler = Reconciler(p.registry, p.changes, retry_alerted_after=3)
    assert reconciler.reconcile_once(suppress_repeats=True)[0].outcome == "executed: FAILED"
    # Now FAILED: the next pass tries the path from FAILED once more.
    reconciler.reconcile_once(suppress_repeats=True)
    count = len(p.changes.changes("worker:w"))
    suppressed = reconciler.reconcile_once(suppress_repeats=True)
    assert suppressed[0].outcome == "suppressed: alerted, unchanged"
    assert len(p.changes.changes("worker:w")) == count
    # A manual pass always retries; so does the loop after the interval.
    reconciler.reconcile_once()
    assert len(p.changes.changes("worker:w")) == count + 1
    outcomes = [reconciler.reconcile_once(suppress_repeats=True)[0].outcome for _ in range(3)]
    assert outcomes[:2] == ["suppressed: alerted, unchanged"] * 2
    assert outcomes[2] != "suppressed: alerted, unchanged"


async def test_a_broken_discovery_does_not_stop_the_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    w = world(monkeypatch)
    sources = w.sources()
    platform = build_runtime_platform(bus=w.bus, discoveries=production_discoveries(sources))
    calls = 0

    def discover() -> list[object]:
        nonlocal calls
        calls += 1
        raise RuntimeError("a view broke")

    loop = RuntimeLoop(platform, discover)
    report = loop.sync_once()
    assert report.problems == ("discovery failed: a view broke",)
    assert loop.step() == [] and calls == 2
    # Nothing it knew was dropped because one pass could not see it.
    assert platform.registry.get(f"strategy:{STRATEGY}").state is S.ACTIVE


async def test_run_exits_on_stop_after_flushing(monkeypatch: pytest.MonkeyPatch) -> None:
    w = world(monkeypatch)
    sources = w.sources()
    stored: dict[str, dict[str, object]] = {}
    platform = build_runtime_platform(
        bus=w.bus,
        discoveries=production_discoveries(sources),
        controllers=production_controllers(sources),
        desired_backend=_Store(stored),
    )
    stop = asyncio.Event()
    refreshed: list[int] = []

    async def refresh() -> None:
        refreshed.append(1)

    def discover() -> list[object]:
        stop.set()  # one pass, then the stop the lifespan sets at shutdown
        return production_discoveries(sources)

    platform.changes.request_desired("tuning:self_tuning", S.PAUSED, REQUESTER, "pause")
    loop = RuntimeLoop(platform, discover, interval_s=3600.0, slow_refresh=refresh)
    await asyncio.wait_for(loop.run(stop), timeout=5.0)
    assert loop.passes == 1 and refreshed == [1]
    assert w.pause.paused
    assert stored["tuning:self_tuning"]["target_state"] == "PAUSED"
