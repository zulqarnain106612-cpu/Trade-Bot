"""RES-021: the registry stays true to what the owners report, and gives up
a component only when nothing still needs it.

* ``resync`` records an owner's new capabilities or dependencies without a
  transition, and a health change without one either; the type cannot change
  under an id, because the id carries it.
* ``retire`` keeps a component that is not STOPPED, that something depends
  on, or that an operator wants in another state; retiring one clears its
  stored desired state, and a failing listener does not stop the retire.
* Deferred intents: one that became unusable is reported, one superseded by
  a newer intent is dropped, one whose component never appeared waits.
* Discovery of the provider loops and the thread pools: every combination
  of loop states maps to one lifecycle state, and a poisoned pool is
  UNHEALTHY.

Decides: RES-021"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from src.runtime.adapters import (
    provider_task_discoveries,
    run_health,
    worker_pool_discoveries,
)
from src.runtime.contracts import (
    OBSERVE_ONLY,
    CapabilitySet,
    ComponentSpec,
    ComponentType,
    DependencyDescriptor,
    HealthReport,
    HealthState,
    LifecycleAction,
    LifecycleState,
    RuntimeContractError,
)
from src.runtime.persistence import DesiredStatePersister
from src.runtime.registry import RuntimeRegistry

from ._support import REQUESTER, build_platform, spec, version

S = LifecycleState


def test_an_id_cannot_carry_another_type() -> None:
    """resync relies on this: the id names the type, so it cannot change."""
    with pytest.raises(RuntimeContractError, match="must be 'task:<name>'"):
        ComponentSpec("worker:x", ComponentType.TASK, version("1"), "tests")


def test_resync_takes_new_capabilities_and_health_without_a_transition() -> None:
    registry = RuntimeRegistry()
    registry.register(spec("x", caps=OBSERVE_ONLY), observed_state=S.ACTIVE)
    before = len(registry.history("worker:x"))
    quarantinable = CapabilitySet(frozenset({LifecycleAction.QUARANTINE}))
    deps = (DependencyDescriptor("eventbus:main", required=False),)
    registry.resync(spec("x", caps=quarantinable, deps=deps), S.ACTIVE, HealthReport())
    record = registry.get("worker:x")
    assert record.spec.capabilities == quarantinable and record.spec.dependencies == deps
    sick = HealthReport(HealthState.DEGRADED, "slow")
    assert registry.resync(spec("x", caps=quarantinable, deps=deps), S.ACTIVE, sick).health == sick
    assert len(registry.history("worker:x")) == before  # neither is a transition


def test_retire_keeps_anything_still_needed() -> None:
    p = build_platform()
    registry = p.registry
    registry.register(spec("running"), observed_state=S.ACTIVE)
    registry.register(spec("needed"), observed_state=S.STOPPED)
    user = spec("user", deps=(DependencyDescriptor("worker:needed"),))
    registry.register(user, observed_state=S.STOPPED)
    registry.register(spec("wanted"), observed_state=S.STOPPED)
    p.changes.request_desired("worker:wanted", S.ACTIVE, REQUESTER, "bring it back")
    assert not registry.retire("worker:running")  # not STOPPED
    assert not registry.retire("worker:needed")  # worker:user depends on it
    assert not registry.retire("worker:wanted")  # an operator wants it ACTIVE
    assert {r.component_id for r in registry.components()} >= {
        "worker:running",
        "worker:needed",
        "worker:wanted",
    }
    assert registry.retire("worker:user") and registry.retire("worker:needed")


def test_retire_clears_the_stored_intent_even_if_a_listener_fails() -> None:
    p = build_platform()
    registry = p.registry
    seen: list[tuple[str, Any]] = []

    def broken(component_id: str, desired: Any) -> None:
        raise ConnectionError("store down")

    registry.add_desired_listener(broken)
    registry.add_desired_listener(lambda cid, desired: seen.append((cid, desired)))
    registry.register(spec("done"), observed_state=S.STOPPED)
    p.changes.request_desired("worker:done", S.STOPPED, REQUESTER, "keep it stopped")
    seen.clear()
    assert registry.retire("worker:done")
    assert seen == [("worker:done", None)] and registry.find("worker:done") is None


class _Store:
    def __init__(self, rows: dict[str, dict[str, Any]]) -> None:
        self.rows = rows

    async def upsert_runtime_desired_state(self, cid: str, desired: Any, ms: int) -> None:
        self.rows[cid] = desired

    async def fetch_runtime_desired_states(self) -> dict[str, dict[str, Any]]:
        return dict(self.rows)


def _row(target: str, version_: str | None = None) -> dict[str, Any]:
    return {
        "target_state": target,
        "target_version": version_,
        "requested_by": "alice",
        "reason": "stored",
        "requested_at": "2026-10-07T00:00:00+00:00",
    }


async def test_deferred_intents_wait_are_superseded_or_reported() -> None:
    p = build_platform()
    registry = p.registry
    persister = DesiredStatePersister(
        _Store(
            {
                "worker:late": _row("PAUSED"),
                "worker:newer": _row("PAUSED"),
                "worker:pinned": _row("ACTIVE", "9"),
                "worker:never": _row("ACTIVE"),
            }
        )
    )
    assert len(await persister.restore(registry)) == 4
    for name in ("late", "newer", "pinned"):
        registry.register(spec(name), observed_state=S.ACTIVE)
    p.changes.request_desired("worker:newer", S.STOPPED, REQUESTER, "decided since")
    problems = persister.apply_deferred(registry)
    unusable = "worker:pinned: desired version 9 is not a version this component has run"
    assert problems == [f"worker:pinned: {unusable}"]
    late, newer = registry.get("worker:late").desired, registry.get("worker:newer").desired
    assert late is not None and late.target_state is S.PAUSED
    assert newer is not None and newer.target_state is S.STOPPED  # the newer intent wins
    assert registry.get("worker:pinned").desired is None
    assert persister.deferred == ("worker:never",)


async def _done(result: str) -> str:
    return result


async def _raises() -> None:
    raise OSError("feed down")


async def test_provider_state_follows_its_loops() -> None:
    gate = asyncio.Event()
    running = [asyncio.create_task(gate.wait(), name=f"live_{i}") for i in range(2)]
    returned = asyncio.create_task(_done("ok"), name="returned")
    failed = asyncio.create_task(_raises(), name="failed")
    await asyncio.gather(returned, failed, return_exceptions=True)
    try:
        rows = {
            d.spec.component_id: d
            for d in provider_task_discoveries(
                {
                    "all_up": running,
                    "half_up": [running[0], returned],
                    "down": [returned],
                    "broken": [running[1], failed],
                    "empty": [],
                }
            )
        }
    finally:
        gate.set()
        await asyncio.gather(*running)
    assert (rows["provider:all_up"].state, rows["provider:all_up"].health.state) == (
        S.ACTIVE,
        HealthState.HEALTHY,
    )
    assert rows["provider:half_up"].health.state is HealthState.DEGRADED
    assert rows["provider:half_up"].health.detail == "1/2 loops running"
    assert rows["provider:down"].state is S.STOPPED
    assert rows["provider:broken"].state is S.FAILED
    assert "feed down" in rows["provider:broken"].health.detail
    assert rows["provider:empty"].state is S.STOPPED
    assert rows["provider:all_up"].spec.version.configuration == {"tasks": ["live_0", "live_1"]}


def test_a_poisoned_pool_is_unhealthy_and_an_empty_one_stopped() -> None:
    rows = {
        d.spec.component_id: d
        for d in worker_pool_discoveries(
            {
                "ensemble": {"workers": 1, "available": False, "detail": "poisoned"},
                "idle": {"workers": 0, "available": True},
            },
            owner="src.engine",
        )
    }
    assert rows["worker:ensemble"].state is S.ACTIVE
    assert rows["worker:ensemble"].health.state is HealthState.UNHEALTHY
    assert rows["worker:idle"].state is S.STOPPED
    assert rows["worker:idle"].health.detail == "0 workers"


def test_run_health_without_a_measured_latency() -> None:
    health = run_health({"runs": 2, "failures": 1, "last_ok": True, "last_latency_ms": None})
    assert (health.state, health.detail) == (HealthState.HEALTHY, "ok; 1/2 cycles failed")
