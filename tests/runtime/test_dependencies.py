"""GOV-069: dependency edges come from the component specs; dependencies,
dependents, order and unmet requirements are computed from them."""

from __future__ import annotations

from src.runtime.contracts import ComponentType, DependencyDescriptor, LifecycleState
from src.runtime.dependencies import DependencyGraph
from src.runtime.registry import RuntimeRegistry

from ._support import Clock, spec

S = LifecycleState
D = DependencyDescriptor


def _registry() -> RuntimeRegistry:
    """Strategy:v8 -> engine E09, model, feature task; model -> feature task."""
    r = RuntimeRegistry(clock=Clock())
    r.register(spec("features", ComponentType.TASK, v="12"), observed_state=S.ACTIVE)
    r.register(spec("E-09", ComponentType.ENGINE, v="4"), observed_state=S.ACTIVE)
    r.register(
        spec("BTC", ComponentType.MODEL, v="31", deps=(D("task:features", "12"),)),
        observed_state=S.ACTIVE,
    )
    r.register(
        spec(
            "trend",
            ComponentType.STRATEGY,
            v="8",
            deps=(D("engine:E-09", "4"), D("model:BTC"), D("task:features")),
        ),
        observed_state=S.ACTIVE,
    )
    return r


def test_direct_and_transitive_dependencies_and_dependents() -> None:
    g = DependencyGraph.from_registry(_registry())
    assert g.dependencies_of("strategy:trend") == ("engine:E-09", "model:BTC", "task:features")
    assert g.dependencies_of("model:BTC", transitive=True) == ("task:features",)
    assert g.dependents_of("task:features") == ("model:BTC", "strategy:trend")
    assert g.dependents_of("task:features", transitive=True) == ("model:BTC", "strategy:trend")
    assert g.dependents_of("strategy:trend") == ()
    assert g.dependents_of("not:registered") == ()


def test_topological_order_puts_dependencies_first() -> None:
    order = DependencyGraph.from_registry(_registry()).topological_order()
    assert order.index("task:features") < order.index("model:BTC") < order.index("strategy:trend")
    assert order.index("engine:E-09") < order.index("strategy:trend")
    assert len(order) == 4


def test_a_missing_dependency_is_skipped_by_ordering_and_reported_as_an_issue() -> None:
    r = RuntimeRegistry(clock=Clock())
    r.register(spec("w", deps=(D("task:gone"), D("task:optional", required=False))))
    g = DependencyGraph.from_registry(r)
    assert g.topological_order() == ("worker:w",)
    assert g.issues() == ["worker:w needs task:gone, which is not registered"]


def test_version_pins_are_checked() -> None:
    r = _registry()
    r.register(spec("pinned", deps=(D("model:BTC", "30"),)))
    assert DependencyGraph.from_registry(r).issues() == [
        "worker:pinned pins model:BTC at 30, which runs 31"
    ]


def test_a_clean_graph_has_no_issues() -> None:
    assert DependencyGraph.from_registry(_registry()).issues() == []
