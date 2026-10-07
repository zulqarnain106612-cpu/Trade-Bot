"""RES-009: runtime components have explicit identity and immutable versions;
desired state and actual state are distinct.

Decides: RES-009"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime

import pytest

from src.runtime.contracts import (
    OBSERVE_ONLY,
    ComponentRecord,
    ComponentSpec,
    ComponentType,
    ComponentVersion,
    DependencyDescriptor,
    DesiredState,
    HealthReport,
    LifecycleAction,
    LifecycleState,
    RuntimeContractError,
    TransitionRecord,
    make_component_id,
)

from ._support import spec, version

NOW = datetime(2026, 10, 7, tzinfo=UTC)


def test_component_id_is_type_prefixed() -> None:
    assert make_component_id(ComponentType.ENGINE, "E-09") == "engine:E-09"


@pytest.mark.parametrize("name", ["", "-x", "has space", "a:b"])
def test_component_name_alphabet_is_enforced(name: str) -> None:
    with pytest.raises(RuntimeContractError):
        make_component_id(ComponentType.ENGINE, name)


def test_spec_id_must_carry_its_own_type_prefix() -> None:
    with pytest.raises(RuntimeContractError, match="must be 'model:<name>'"):
        ComponentSpec("engine:x", ComponentType.MODEL, version(), "o")
    with pytest.raises(RuntimeContractError, match="invalid component name"):
        ComponentSpec("model:", ComponentType.MODEL, version(), "o")


def test_spec_needs_owner_tuple_dependencies_and_no_self_or_duplicate_edges() -> None:
    with pytest.raises(RuntimeContractError, match="owner"):
        ComponentSpec("model:m", ComponentType.MODEL, version(), "")
    with pytest.raises(RuntimeContractError, match="tuple"):
        ComponentSpec(
            "model:m",
            ComponentType.MODEL,
            version(),
            "o",
            OBSERVE_ONLY,
            [DependencyDescriptor("x:y")],  # type: ignore[arg-type]
        )
    with pytest.raises(RuntimeContractError, match="itself"):
        ComponentSpec(
            "model:m",
            ComponentType.MODEL,
            version(),
            "o",
            OBSERVE_ONLY,
            (DependencyDescriptor("model:m"),),
        )
    dup = (DependencyDescriptor("engine:a"), DependencyDescriptor("engine:a", "2"))
    with pytest.raises(RuntimeContractError, match="duplicate"):
        ComponentSpec("model:m", ComponentType.MODEL, version(), "o", OBSERVE_ONLY, dup)


def test_version_is_immutable_and_hands_out_copies() -> None:
    config = {"window": 20, "nested": {"a": 1}}
    v = ComponentVersion.create("3", "pkg.Model", config)
    config["window"] = 99  # the caller's mapping is not the version's
    copy = v.configuration
    copy["nested"]["a"] = 2  # nor is the copy it hands out
    assert v.configuration == {"window": 20, "nested": {"a": 1}}
    with pytest.raises(dataclasses.FrozenInstanceError):
        v.version = "4"  # type: ignore[misc]


def test_version_fingerprint_covers_version_implementation_and_configuration() -> None:
    base = ComponentVersion.create("1", "pkg.A", {"k": 1})
    assert base.fingerprint == ComponentVersion.create("1", "pkg.A", {"k": 1}).fingerprint
    others = [
        ComponentVersion.create("2", "pkg.A", {"k": 1}),
        ComponentVersion.create("1", "pkg.B", {"k": 1}),
        ComponentVersion.create("1", "pkg.A", {"k": 2}),
    ]
    assert len({base.fingerprint, *(o.fingerprint for o in others)}) == 4
    assert base.to_dict()["fingerprint"] == base.fingerprint


@pytest.mark.parametrize(
    ("args", "match"),
    [
        (("", "pkg.A", "{}"), "version"),
        (("1", "", "{}"), "implementation"),
        (("1", "pkg.A", "not json"), "not JSON"),
        (("1", "pkg.A", "[1]"), "canonical"),
        (("1", "pkg.A", '{"b":1, "a":2}'), "canonical"),
    ],
)
def test_version_rejects_malformed_fields(args: tuple[str, str, str], match: str) -> None:
    with pytest.raises(RuntimeContractError, match=match):
        ComponentVersion(*args)


def test_version_configuration_must_be_json() -> None:
    with pytest.raises(RuntimeContractError, match="JSON-serialisable"):
        ComponentVersion.create("1", "pkg.A", {"f": object()})


def test_desired_state_is_a_request_not_an_outcome() -> None:
    for outcome in (LifecycleState.FAILED, LifecycleState.DISCOVERED, LifecycleState.INITIALIZED):
        with pytest.raises(RuntimeContractError, match="outcome"):
            DesiredState(outcome, "op", "why", NOW)
    with pytest.raises(RuntimeContractError, match="requested_by"):
        DesiredState(LifecycleState.ACTIVE, "", "why", NOW)
    with pytest.raises(RuntimeContractError, match="timezone"):
        DesiredState(LifecycleState.ACTIVE, "op", "why", datetime(2026, 1, 1))
    d = DesiredState(LifecycleState.STOPPED, "op", "why", NOW, "2")
    assert d.to_dict() == {
        "target_state": "STOPPED",
        "target_version": "2",
        "requested_by": "op",
        "reason": "why",
        "requested_at": NOW.isoformat(),
    }


def test_record_keeps_desired_and_actual_apart_in_its_serialisation() -> None:
    s = spec("w", deps=(DependencyDescriptor("task:t", "1", required=False),))
    desired = DesiredState(LifecycleState.ACTIVE, "op", "why", NOW)
    transition = TransitionRecord(
        s.component_id, None, None, LifecycleState.STANDBY, None, "1", "a", NOW
    )
    record = ComponentRecord(
        spec=s,
        state=LifecycleState.STANDBY,
        created_at=NOW,
        updated_at=NOW,
        desired=desired,
        health=HealthReport(observed_at=NOW),
        last_transition=transition,
        previous_versions=(version("0"),),
    )
    out = record.to_dict(("model:m",))
    assert out["state"] == "STANDBY"
    assert out["desired_state"]["target_state"] == "ACTIVE"
    assert out["dependencies"] == [{"component_id": "task:t", "version": "1", "required": False}]
    assert out["dependents"] == ["model:m"]
    assert out["last_transition"]["action"] == "OBSERVED"
    assert out["health"]["observed_at"] == NOW.isoformat()
    assert out["previous_versions"] == ["0"]
    assert record.component_id == "worker:w" and record.version.version == "1"
    bare = ComponentRecord(s, LifecycleState.DISCOVERED, NOW, NOW).to_dict()
    assert bare["desired_state"] is None and bare["last_transition"] is None
    assert bare["health"] == {"state": "UNKNOWN", "detail": "", "observed_at": None}


def test_transition_record_serialises_actions_and_failures() -> None:
    t = TransitionRecord(
        "worker:w",
        LifecycleAction.START,
        LifecycleState.STOPPED,
        LifecycleState.FAILED,
        "1",
        "1",
        "op",
        NOW,
        ok=False,
        error="boom",
        change_id="chg-1",
    )
    assert t.to_dict() == {
        "component_id": "worker:w",
        "action": "START",
        "from_state": "STOPPED",
        "to_state": "FAILED",
        "from_version": "1",
        "to_version": "1",
        "actor": "op",
        "at": NOW.isoformat(),
        "ok": False,
        "error": "boom",
        "change_id": "chg-1",
    }


def test_with_version_keeps_identity() -> None:
    s = spec("w")
    moved = s.with_version(version("2"))
    assert (moved.component_id, moved.owner, moved.capabilities) == (
        s.component_id,
        s.owner,
        s.capabilities,
    )
    assert moved.version.version == "2" and s.version.version == "1"
