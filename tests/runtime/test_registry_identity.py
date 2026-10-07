"""GOV-067: duplicate identities are rejected and a version string names one
content forever, across replace and rollback."""

from __future__ import annotations

import pytest

from src.runtime.contracts import LifecycleAction, LifecycleState, RuntimeContractError
from src.runtime.registry import DuplicateComponentError, RuntimeRegistry

from ._support import Clock, spec, version

A = LifecycleAction
S = LifecycleState


@pytest.fixture
def registry() -> RuntimeRegistry:
    r = RuntimeRegistry(clock=Clock())
    r.register(spec("w"), observed_state=S.ACTIVE)
    return r


def test_duplicate_id_is_rejected_whatever_the_version(registry: RuntimeRegistry) -> None:
    with pytest.raises(DuplicateComponentError, match="already registered at version 1"):
        registry.register(spec("w", v="2"))


def test_batch_registration_is_atomic() -> None:
    registry = RuntimeRegistry(clock=Clock())
    registry.register(spec("taken"))
    with pytest.raises(DuplicateComponentError, match="worker:a, worker:taken"):
        registry.register_all(
            [(spec("a"), S.ACTIVE), (spec("a"), S.ACTIVE), (spec("taken"), S.ACTIVE)]
        )
    assert [r.component_id for r in registry.components()] == ["worker:taken"]
    created = registry.register_all([(spec("b"), S.STANDBY)], actor="adapter")
    assert created[0].state is S.STANDBY


def test_replace_keeps_the_previous_version(registry: RuntimeRegistry) -> None:
    t = registry.apply("worker:w", A.REPLACE, actor="op", target_version=version("2", k=1))
    assert (t.from_version, t.to_version) == ("1", "2")
    record = registry.get("worker:w")
    assert record.version.version == "2"
    assert [v.version for v in record.previous_versions] == ["1"]
    assert [v.version for v in registry.versions("worker:w")] == ["1", "2"]


def test_replace_needs_a_new_version(registry: RuntimeRegistry) -> None:
    with pytest.raises(RuntimeContractError, match="needs a new version"):
        registry.apply("worker:w", A.REPLACE, actor="op")
    with pytest.raises(RuntimeContractError, match="needs a new version"):
        registry.apply("worker:w", A.REPLACE, actor="op", target_version=version("1"))


def test_a_version_string_cannot_name_new_content(registry: RuntimeRegistry) -> None:
    registry.apply("worker:w", A.REPLACE, actor="op", target_version=version("2", k=1))
    registry.apply("worker:w", A.ROLLBACK, actor="op")
    with pytest.raises(RuntimeContractError, match="immutable"):
        registry.apply("worker:w", A.REPLACE, actor="op", target_version=version("2", k=999))
    # The same content under the same name may come back (roll forward again).
    registry.apply("worker:w", A.REPLACE, actor="op", target_version=version("2", k=1))
    assert registry.get("worker:w").version == version("2", k=1)


def test_rollback_returns_to_the_latest_or_a_named_previous_version(
    registry: RuntimeRegistry,
) -> None:
    for v in ("2", "3"):
        registry.apply("worker:w", A.REPLACE, actor="op", target_version=version(v))
    registry.apply("worker:w", A.ROLLBACK, actor="op")
    assert registry.get("worker:w").version.version == "2"
    registry.apply("worker:w", A.REPLACE, actor="op", target_version=version("3"))
    registry.apply("worker:w", A.ROLLBACK, actor="op", target_version=version("1"))
    record = registry.get("worker:w")
    assert record.version.version == "1" and record.previous_versions == ()


def test_rollback_without_or_outside_history_is_refused(registry: RuntimeRegistry) -> None:
    with pytest.raises(RuntimeContractError, match="no previous version"):
        registry.apply("worker:w", A.ROLLBACK, actor="op")
    registry.apply("worker:w", A.REPLACE, actor="op", target_version=version("2"))
    with pytest.raises(RuntimeContractError, match="not a previous version"):
        registry.apply("worker:w", A.ROLLBACK, actor="op", target_version=version("7"))
