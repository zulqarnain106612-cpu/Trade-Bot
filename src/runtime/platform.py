"""
The runtime platform's composition root.

``build_runtime_platform`` wires one registry, one supervisor set, one change
manager, one reconciler, one adaptive lifecycle and one decision-trace index
together, with the change audit mirrored onto the event bus and into the
hash-chained audit trail, and desired state persisted through the storage
backend when one is given. The API holds exactly one of these; it is the
only way the control plane reaches runtime state.

``live_discoveries`` reads what the running process exposes publicly: the
default strategy registry with its kill switch, the tuning parameter
registry and the event bus. Model registries, engine orchestrators, worker
pools and tasks are added by whoever holds them (``extra`` discoveries and
controllers); nothing here reaches into another package's private state.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from src.diagnostics.audit_trail import AuditTrail
from src.diagnostics.decision_trace import STAGE_OF_TOPIC, DecisionTraceIndex
from src.eventbus import EventBus
from src.risk.strategy_kill_switch import get_strategy_kill_switch_manager
from src.runtime.adapters import (
    Discovery,
    eventbus_discovery,
    strategy_discoveries,
    tuning_discoveries,
)
from src.runtime.adaptive import AdaptiveLifecycle
from src.runtime.changes import TERMINAL, ChangeManager
from src.runtime.contracts import ComponentType
from src.runtime.dependencies import DependencyGraph
from src.runtime.events import audit_trail_sink, eventbus_audit_sink, transition_publisher
from src.runtime.persistence import DesiredStateBackend, DesiredStatePersister
from src.runtime.reconcile import Reconciler, diff
from src.runtime.registry import RuntimeRegistry
from src.runtime.supervisor import Controller, RestartPolicy, SupervisorSet
from src.strategies.registry import get_default_registry
from src.tuning.registry import parameter_registry

# The bus topics a decision trace is built from.
TRACE_TOPICS: frozenset[str] = frozenset(STAGE_OF_TOPIC)


@dataclass
class RuntimePlatform:
    registry: RuntimeRegistry
    supervisors: SupervisorSet
    changes: ChangeManager
    reconciler: Reconciler
    adaptive: AdaptiveLifecycle
    traces: DecisionTraceIndex
    persister: DesiredStatePersister | None = None

    async def flush(self) -> int:
        """Persist queued desired-state changes; 0 without a backend."""
        return 0 if self.persister is None else await self.persister.flush()

    async def restore(self) -> list[str]:
        """Load stored desired states; the problems with any that could not be."""
        return [] if self.persister is None else await self.persister.restore(self.registry)

    def summary(self) -> dict[str, Any]:
        records = self.registry.components()
        by_type: dict[str, int] = {}
        by_state: dict[str, int] = {}
        for r in records:
            by_type[r.spec.component_type.value] = by_type.get(r.spec.component_type.value, 0) + 1
            by_state[r.state.value] = by_state.get(r.state.value, 0) + 1
        open_changes = [c for c in self.changes.changes() if c.status not in TERMINAL]
        return {
            "components": len(records),
            "by_type": dict(sorted(by_type.items())),
            "by_state": dict(sorted(by_state.items())),
            "mismatches": [m.to_dict() for m in diff(self.registry)],
            "open_changes": [c.to_dict() for c in open_changes],
            "dependency_issues": DependencyGraph.from_registry(self.registry).issues(),
        }

    def dependencies(self) -> dict[str, Any]:
        graph = DependencyGraph.from_registry(self.registry)
        cycle = graph.find_cycle()
        return {
            "edges": [
                {"from": r.component_id, "to": d.component_id, "version": d.version}
                for r in self.registry.components()
                for d in r.spec.dependencies
            ],
            "order": [] if cycle is not None else list(graph.topological_order()),
            "cycle": None if cycle is None else list(cycle),
            "issues": graph.issues(),
        }


def live_discoveries(bus: EventBus) -> list[Discovery]:
    return [
        *strategy_discoveries(get_default_registry(), get_strategy_kill_switch_manager()),
        *tuning_discoveries(parameter_registry),
        eventbus_discovery(bus),
    ]


def build_runtime_platform(
    *,
    bus: EventBus,
    discoveries: Iterable[Discovery],
    trail: AuditTrail | None = None,
    controllers: Mapping[ComponentType, Controller] | None = None,
    desired_backend: DesiredStateBackend | None = None,
    policy: RestartPolicy | None = None,
) -> RuntimePlatform:
    registry = RuntimeRegistry()
    registry.add_transition_listener(transition_publisher(bus))
    rows = list(discoveries)
    registry.register_all([(d.spec, d.state) for d in rows])
    for d in rows:
        registry.record_health(d.spec.component_id, d.health)
    supervisors = SupervisorSet(registry, controllers, policy=policy)
    sinks = [eventbus_audit_sink(bus)]
    if trail is not None:
        sinks.append(audit_trail_sink(trail))
    changes = ChangeManager(registry, supervisors, audit_sinks=sinks)
    persister = None
    if desired_backend is not None:
        persister = DesiredStatePersister(desired_backend)
        persister.attach(registry)
    return RuntimePlatform(
        registry=registry,
        supervisors=supervisors,
        changes=changes,
        reconciler=Reconciler(registry, changes),
        adaptive=AdaptiveLifecycle(changes),
        traces=DecisionTraceIndex(),
        persister=persister,
    )
