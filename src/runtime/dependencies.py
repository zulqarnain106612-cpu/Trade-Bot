"""
Dependency graph and impact analysis.

Edges come from the components' own specs (``ComponentSpec.dependencies``),
read from the registry each time a graph is built -- there is no stored copy
of the edges to drift from what the components declare.

``impact`` answers, for one proposed action: which components are affected
(transitive dependents), which validations the change needs, which rollout it
needs, and which failure domains it can reach.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from src.runtime.classification import DECISION_PATH_TYPES, INTO_SERVICE, classify
from src.runtime.contracts import (
    RUNNING_STATES,
    ChangeClass,
    ComponentRecord,
    ComponentType,
    ComponentVersion,
    LifecycleAction,
    RuntimeContractError,
)
from src.runtime.registry import RuntimeRegistry

REQUIRED_VALIDATIONS: dict[ComponentType, tuple[str, ...]] = {
    ComponentType.STRATEGY: ("backtest", "risk-gate regression"),
    ComponentType.MODEL: ("shadow evaluation",),
    ComponentType.ENGINE: ("engine seam contract (SIG-004)",),
    ComponentType.TUNING: ("promotion gauntlet",),
    ComponentType.UPGRADE: ("artifact verification",),
    ComponentType.WORKER: ("health probe",),
    ComponentType.TASK: ("health probe",),
    ComponentType.PROVIDER: ("health probe",),
    ComponentType.EVENTBUS: ("health probe",),
}


class DependencyCycleError(RuntimeContractError):
    def __init__(self, cycle: tuple[str, ...]) -> None:
        self.cycle = cycle
        super().__init__(f"dependency cycle: {' -> '.join(cycle)}")


@dataclass(frozen=True, slots=True)
class ImpactReport:
    component_id: str
    action: LifecycleAction
    affected: tuple[str, ...]
    required_validations: tuple[str, ...]
    rollout_mode: ChangeClass
    reasons: tuple[str, ...]
    failure_domains: tuple[str, ...]
    issues: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "component_id": self.component_id,
            "action": self.action.value,
            "affected": list(self.affected),
            "required_validations": list(self.required_validations),
            "rollout_mode": self.rollout_mode.value,
            "reasons": list(self.reasons),
            "failure_domains": list(self.failure_domains),
            "issues": list(self.issues),
        }


class DependencyGraph:
    def __init__(self, records: Iterable[ComponentRecord]) -> None:
        self._records = {r.component_id: r for r in records}
        self._edges: dict[str, tuple[str, ...]] = {
            cid: tuple(d.component_id for d in r.spec.dependencies)
            for cid, r in self._records.items()
        }
        self._reverse: dict[str, list[str]] = {cid: [] for cid in self._records}
        for cid, deps in sorted(self._edges.items()):
            for dep in deps:
                self._reverse.setdefault(dep, []).append(cid)

    @classmethod
    def from_registry(cls, registry: RuntimeRegistry) -> DependencyGraph:
        return cls(registry.components())

    def find_cycle(self) -> tuple[str, ...] | None:
        """One cycle as a closed path (first id repeated at the end), or None."""
        white, grey, black = 0, 1, 2
        colour = dict.fromkeys(self._edges, white)
        stack: list[str] = []

        def visit(node: str) -> tuple[str, ...] | None:
            colour[node] = grey
            stack.append(node)
            for dep in self._edges.get(node, ()):
                if colour.get(dep, black) == grey:
                    return (*stack[stack.index(dep) :], dep)
                if colour.get(dep, black) == white:
                    found = visit(dep)
                    if found is not None:
                        return found
            stack.pop()
            colour[node] = black
            return None

        for node in sorted(self._edges):
            if colour[node] == white:
                found = visit(node)
                if found is not None:
                    return found
        return None

    def check_acyclic(self) -> None:
        cycle = self.find_cycle()
        if cycle is not None:
            raise DependencyCycleError(cycle)

    def topological_order(self) -> tuple[str, ...]:
        """Dependencies before dependents; ties by id. Raises on a cycle."""
        self.check_acyclic()
        order: list[str] = []
        done: set[str] = set()

        def place(node: str) -> None:
            if node in done:
                return
            done.add(node)
            for dep in sorted(self._edges.get(node, ())):
                if dep in self._edges:
                    place(dep)
            order.append(node)

        for node in sorted(self._edges):
            place(node)
        return tuple(order)

    def dependencies_of(self, component_id: str, *, transitive: bool = False) -> tuple[str, ...]:
        return self._walk(component_id, self._edges, transitive)

    def dependents_of(self, component_id: str, *, transitive: bool = False) -> tuple[str, ...]:
        reverse = {k: tuple(v) for k, v in self._reverse.items()}
        return self._walk(component_id, reverse, transitive)

    @staticmethod
    def _walk(start: str, edges: dict[str, tuple[str, ...]], transitive: bool) -> tuple[str, ...]:
        seen: set[str] = set()
        frontier = list(edges.get(start, ()))
        while frontier:
            node = frontier.pop()
            if node in seen or node == start:
                continue
            seen.add(node)
            if transitive:
                frontier.extend(edges.get(node, ()))
        return tuple(sorted(seen))

    def issues(self) -> list[str]:
        """Every unmet required dependency and version pin, graph-wide."""
        problems = []
        for cid in sorted(self._records):
            problems.extend(self._unmet(cid, check_running=False))
        cycle = self.find_cycle()
        if cycle is not None:
            problems.append(str(DependencyCycleError(cycle)))
        return problems

    def _unmet(self, component_id: str, *, check_running: bool) -> list[str]:
        problems = []
        for dep in self._records[component_id].spec.dependencies:
            target = self._records.get(dep.component_id)
            if target is None:
                if dep.required:
                    problems.append(f"{component_id} needs {dep.component_id}, which is not registered")
                continue
            if dep.version is not None and target.version.version != dep.version:
                problems.append(
                    f"{component_id} pins {dep.component_id} at {dep.version}, "
                    f"which runs {target.version.version}"
                )
            if check_running and dep.required and target.state not in RUNNING_STATES:
                problems.append(
                    f"{component_id} needs {dep.component_id} running; it is {target.state.value}"
                )
        return problems

    def impact(
        self,
        component_id: str,
        action: LifecycleAction,
        target_version: ComponentVersion | None = None,
    ) -> ImpactReport:
        record = self._records.get(component_id)
        if record is None:
            raise KeyError(component_id)
        affected = self.dependents_of(component_id, transitive=True)
        issues: list[str] = []
        cycle = self.find_cycle()
        if cycle is not None and component_id in cycle:
            issues.append(str(DependencyCycleError(cycle)))
        if action in INTO_SERVICE:
            issues.extend(self._unmet(component_id, check_running=True))
        if action is LifecycleAction.REPLACE and target_version is not None:
            for dependent in self.dependents_of(component_id):
                for dep in self._records[dependent].spec.dependencies:
                    if (
                        dep.component_id == component_id
                        and dep.version is not None
                        and dep.version != target_version.version
                    ):
                        issues.append(
                            f"{dependent} pins {component_id} at {dep.version}; "
                            f"replacing with {target_version.version} breaks it"
                        )
        rollout, reasons = classify(record, action, issues)
        types = {record.spec.component_type} | {
            self._records[a].spec.component_type for a in affected
        }
        domains = sorted(t.value for t in types)
        if types & DECISION_PATH_TYPES:
            domains.append("decision-path")
        validations = sorted({v for t in types for v in REQUIRED_VALIDATIONS[t]})
        return ImpactReport(
            component_id=component_id,
            action=action,
            affected=affected,
            required_validations=tuple(validations),
            rollout_mode=rollout,
            reasons=reasons,
            failure_domains=tuple(domains),
            issues=tuple(issues),
        )
