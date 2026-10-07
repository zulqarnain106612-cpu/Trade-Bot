"""
Desired-state persistence over the existing storage backends.

The desired state is the operator's intent; it has to survive a restart so
the reconciler can compare it with what actually runs after the process comes
back. It is stored in ``runtime_desired_state`` (migration v9 of both
``src/data/storage.py`` and ``src/data/timescale_storage.py``); there is no
second persistence mechanism.

The registry is synchronous and storage is async, so the registry's desired
-state listener only queues the latest value per component and ``flush``
writes the queue. A failed write leaves the entry queued for the next flush:
the newest intent is never dropped and never overtaken by an older one.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, Protocol

import structlog

from src.runtime.contracts import DesiredState, LifecycleState, RuntimeContractError
from src.runtime.registry import RuntimeRegistry, UnknownComponentError

log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)


class DesiredStateBackend(Protocol):
    async def upsert_runtime_desired_state(
        self, component_id: str, desired: dict[str, Any] | None, updated_ms: int
    ) -> None: ...

    async def fetch_runtime_desired_states(self) -> dict[str, dict[str, Any]]: ...


def desired_from_dict(data: dict[str, Any]) -> DesiredState:
    """Inverse of ``DesiredState.to_dict``; raises RuntimeContractError on bad data."""
    try:
        return DesiredState(
            target_state=LifecycleState(data["target_state"]),
            requested_by=str(data["requested_by"]),
            reason=str(data["reason"]),
            requested_at=datetime.fromisoformat(str(data["requested_at"])),
            target_version=None if data.get("target_version") is None else str(data["target_version"]),
        )
    except (KeyError, ValueError, TypeError) as exc:
        raise RuntimeContractError(f"malformed desired state: {exc}") from exc


def _utc_ms() -> int:
    return int(datetime.now(UTC).timestamp() * 1000)


class DesiredStatePersister:
    def __init__(self, backend: DesiredStateBackend, *, clock_ms: Callable[[], int] = _utc_ms) -> None:
        self._backend = backend
        self._clock_ms = clock_ms
        self._lock = threading.Lock()
        self._pending: dict[str, DesiredState | None] = {}

    def attach(self, registry: RuntimeRegistry) -> None:
        registry.add_desired_listener(self.enqueue)

    def enqueue(self, component_id: str, desired: DesiredState | None) -> None:
        with self._lock:
            self._pending[component_id] = desired

    @property
    def pending(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(sorted(self._pending))

    async def flush(self) -> int:
        """Write every queued desired state; returns how many were written."""
        with self._lock:
            batch = dict(self._pending)
        written = 0
        for component_id, desired in sorted(batch.items()):
            payload = None if desired is None else desired.to_dict()
            try:
                await self._backend.upsert_runtime_desired_state(
                    component_id, payload, self._clock_ms()
                )
            except Exception as exc:  # stays queued; the next flush retries it
                log.error("runtime.desired_persist_failed", component_id=component_id, error=str(exc))
                continue
            with self._lock:
                # Only clear what was written: a newer value queued meanwhile stays.
                if component_id in self._pending and self._pending[component_id] is desired:
                    del self._pending[component_id]
            written += 1
        return written

    async def restore(self, registry: RuntimeRegistry) -> list[str]:
        """
        Load stored desired states into the registry. Returns one problem per
        entry that could not be applied (unknown component, malformed row,
        version the component never ran); those are reported, not guessed at.
        """
        problems: list[str] = []
        stored = await self._backend.fetch_runtime_desired_states()
        for component_id, data in sorted(stored.items()):
            try:
                registry.set_desired(component_id, desired_from_dict(data), notify=False)
            except UnknownComponentError:
                problems.append(f"{component_id}: stored desired state for an unknown component")
            except RuntimeContractError as exc:
                problems.append(f"{component_id}: {exc}")
        return problems
