"""GOV-071: desired state persists through the existing storage backend and
is restored into a fresh registry; the newest intent is never lost to a
failed or overlapping write."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from src.data.storage import StorageBackend
from src.runtime.contracts import (
    DesiredState,
    LifecycleAction,
    LifecycleState,
    RuntimeContractError,
)
from src.runtime.persistence import DesiredStatePersister, desired_from_dict
from src.runtime.registry import RuntimeRegistry

from ._support import Clock, spec, version

S = LifecycleState
NOW = datetime(2026, 10, 7, tzinfo=UTC)


def test_desired_state_dict_round_trip() -> None:
    d = DesiredState(S.DRAINED, "op", "maintenance", NOW, "2")
    assert desired_from_dict(d.to_dict()) == d
    bare = DesiredState(S.ACTIVE, "op", "x", NOW)
    assert desired_from_dict(bare.to_dict()) == bare


@pytest.mark.parametrize(
    "data",
    [
        {},
        {"target_state": "NOPE", "requested_by": "a", "reason": "b", "requested_at": "x"},
        {"target_state": "ACTIVE", "requested_by": "a", "reason": "b", "requested_at": "nope"},
    ],
)
def test_malformed_rows_are_refused(data: dict[str, Any]) -> None:
    with pytest.raises(RuntimeContractError, match="malformed"):
        desired_from_dict(data)


def _registry() -> RuntimeRegistry:
    r = RuntimeRegistry(clock=Clock())
    r.register(spec("w"), observed_state=S.ACTIVE)
    r.register(spec("v"), observed_state=S.ACTIVE)
    return r


async def test_desired_state_survives_a_restart(tmp_path: Path) -> None:
    db = tmp_path / "runtime.db"
    storage = StorageBackend(db_path=db)
    await storage.initialize()
    registry = _registry()
    registry.apply("worker:w", LifecycleAction.REPLACE, actor="op", target_version=version("2"))
    persister = DesiredStatePersister(storage, clock_ms=lambda: 1)
    persister.attach(registry)
    registry.set_desired("worker:w", DesiredState(S.DRAINED, "op", "maintenance", NOW, "1"))
    registry.set_desired("worker:v", DesiredState(S.STOPPED, "op", "retire", NOW))
    registry.set_desired("worker:v", None)
    registry.register(spec("u"), observed_state=S.ACTIVE)
    registry.set_desired("worker:u", DesiredState(S.STOPPED, "op", "shutdown", NOW))
    assert persister.pending == ("worker:u", "worker:v", "worker:w")
    assert await persister.flush() == 3
    assert persister.pending == ()
    await storage.close()

    # A fresh process: new connection, new registry rebuilt by discovery.
    reopened = StorageBackend(db_path=db)
    await reopened.initialize()
    fresh = RuntimeRegistry(clock=Clock())
    fresh.register(spec("w", v="2"), observed_state=S.ACTIVE)
    fresh.register(spec("v"), observed_state=S.ACTIVE)
    fresh.register(spec("u"), observed_state=S.ACTIVE)
    problems = await DesiredStatePersister(reopened).restore(fresh)
    await reopened.close()
    # Version 1 is not a version the rebuilt component has run: reported, not guessed.
    assert problems == [
        "worker:w: worker:w: desired version 1 is not a version this component has run"
    ]
    assert fresh.get("worker:v").desired is None
    restored = fresh.get("worker:u").desired
    assert restored == DesiredState(S.STOPPED, "op", "shutdown", NOW)
    assert fresh.get("worker:u").state is S.ACTIVE  # restored intent, not action


class _MemoryBackend:
    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}
        self.fail = False
        self.during_write: Any = None

    async def upsert_runtime_desired_state(
        self, component_id: str, desired: dict[str, Any] | None, updated_ms: int
    ) -> None:
        if self.during_write is not None:
            hook, self.during_write = self.during_write, None
            hook()
        if self.fail:
            raise ConnectionError("db down")
        if desired is None:
            self.rows.pop(component_id, None)
        else:
            self.rows[component_id] = desired

    async def fetch_runtime_desired_states(self) -> dict[str, dict[str, Any]]:
        return dict(self.rows)


async def test_a_failed_write_stays_queued_for_the_next_flush() -> None:
    backend = _MemoryBackend()
    persister = DesiredStatePersister(backend)
    desired = DesiredState(S.STOPPED, "op", "x", NOW)
    persister.enqueue("worker:w", desired)
    backend.fail = True
    assert await persister.flush() == 0
    assert persister.pending == ("worker:w",)
    backend.fail = False
    assert await persister.flush() == 1
    assert backend.rows["worker:w"]["target_state"] == "STOPPED"


async def test_a_newer_intent_queued_during_a_write_is_not_cleared() -> None:
    backend = _MemoryBackend()
    persister = DesiredStatePersister(backend)
    older = DesiredState(S.STOPPED, "op", "old", NOW)
    newer = DesiredState(S.ACTIVE, "op", "new", NOW)
    persister.enqueue("worker:w", older)
    backend.during_write = lambda: persister.enqueue("worker:w", newer)
    assert await persister.flush() == 1
    assert persister.pending == ("worker:w",)
    await persister.flush()
    assert backend.rows["worker:w"]["reason"] == "new"


async def test_restore_reports_unknown_components_and_bad_rows() -> None:
    backend = _MemoryBackend()
    backend.rows = {
        "worker:gone": DesiredState(S.STOPPED, "op", "x", NOW).to_dict(),
        "worker:w": {"target_state": "BOGUS"},
        "worker:v": DesiredState(S.DRAINED, "op", "keep", NOW).to_dict(),
    }
    registry = _registry()
    problems = await DesiredStatePersister(backend).restore(registry)
    assert problems[0] == "worker:gone: stored desired state for an unknown component"
    assert problems[1].startswith("worker:w: malformed desired state")
    restored = registry.get("worker:v").desired
    assert restored is not None and restored.reason == "keep"
