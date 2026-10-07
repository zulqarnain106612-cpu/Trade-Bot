"""RES-012: dependency cycles are detected and reported as a closed path.

Decides: RES-012"""

from __future__ import annotations

import pytest

from src.runtime.contracts import DependencyDescriptor, LifecycleAction, LifecycleState
from src.runtime.dependencies import DependencyCycleError, DependencyGraph
from src.runtime.registry import RuntimeRegistry

from ._support import Clock, spec

D = DependencyDescriptor


def _graph(edges: dict[str, tuple[str, ...]]) -> DependencyGraph:
    r = RuntimeRegistry(clock=Clock())
    for name, deps in edges.items():
        r.register(
            spec(name, deps=tuple(D(f"worker:{d}") for d in deps)),
            observed_state=LifecycleState.STANDBY,
        )
    return DependencyGraph.from_registry(r)


def test_acyclic_graph_has_no_cycle() -> None:
    g = _graph({"a": ("b", "c"), "b": ("c",), "c": ()})
    assert g.find_cycle() is None
    g.check_acyclic()


def test_two_node_cycle() -> None:
    g = _graph({"a": ("b",), "b": ("a",)})
    assert g.find_cycle() == ("worker:a", "worker:b", "worker:a")
    with pytest.raises(DependencyCycleError, match="worker:a -> worker:b -> worker:a") as err:
        g.check_acyclic()
    assert err.value.cycle == ("worker:a", "worker:b", "worker:a")
    with pytest.raises(DependencyCycleError):
        g.topological_order()


def test_cycle_reached_through_an_acyclic_prefix() -> None:
    g = _graph({"a": ("b",), "b": ("c",), "c": ("d",), "d": ("b",), "e": ("a",)})
    assert g.find_cycle() == ("worker:b", "worker:c", "worker:d", "worker:b")


def test_cycle_found_after_a_fully_explored_component() -> None:
    g = _graph({"a": ("b",), "b": (), "x": ("y",), "y": ("x",)})
    assert g.find_cycle() == ("worker:x", "worker:y", "worker:x")


def test_a_cycle_is_a_graph_issue_and_forbids_changes_on_its_members() -> None:
    g = _graph({"a": ("b",), "b": ("a",), "c": ()})
    assert g.issues()[-1].startswith("dependency cycle")
    assert g.dependents_of("worker:a", transitive=True) == ("worker:b",)
    assert g.impact("worker:a", LifecycleAction.ACTIVATE).issues[0].startswith("dependency cycle")
    assert g.impact("worker:c", LifecycleAction.ACTIVATE).issues == ()
