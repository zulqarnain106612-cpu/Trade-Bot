"""
The runtime platform's face on the existing control API.

Not a second control API: the routes live in ``src/api/main.py`` beside
``/controls``, behind the same API key, permission and rate-limit
dependencies, and every mutation needs the same operator second factor
(SEC-007). This module only turns request bodies into change-manager calls
and change-manager refusals into HTTP statuses.

The path is always

    API -> operator second factor -> ChangeManager (classify, dependencies,
    policy, approval) -> SupervisorSet -> controller -> component

and nothing here touches a supervisor, a controller or a subsystem
directly. ``actor_kind`` says who is asking: an AI client (an agent behind
an MCP tool, say) may request changes and execute approved ones, but the
change manager refuses it approval, stage results and promotion. Whoever
holds OPERATOR_SECRET is treated as the human operator, so that secret must
never be given to an automated client.
"""

from __future__ import annotations

import hmac
import os
from typing import Any, Literal

from pydantic import BaseModel, Field

from src.runtime.changes import (
    ROLE_APPROVER,
    ROLE_REQUESTER,
    STAGE_FOR,
    Actor,
    ActorKind,
    ChangeError,
    ChangeRecord,
    ChangeRequest,
    ChangeStatus,
    UnauthorizedError,
)
from src.runtime.contracts import (
    ComponentVersion,
    LifecycleAction,
    LifecycleState,
    RuntimeContractError,
)
from src.runtime.platform import RuntimePlatform
from src.runtime.registry import UnknownComponentError

# What /runtime/components/{id}/{action} accepts: the operator's direct
# levers. Version changes and activations go through /runtime/changes, where
# the target version and the classification are explicit.
COMPONENT_ACTIONS: frozenset[str] = frozenset(
    {"pause", "resume", "drain", "stop", "deactivate", "quarantine", "rollback", "start"}
)


class RuntimeControlError(Exception):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


class OperatorFactor(BaseModel):
    operator: str = Field(min_length=1, max_length=64)
    operator_secret: str
    actor_kind: Literal["human", "ai"] = "human"


class RuntimeChangeBody(OperatorFactor):
    component_id: str = Field(min_length=1, max_length=200)
    action: str
    reason: str = Field(min_length=1, max_length=500)
    target_version: str | None = Field(default=None, max_length=100)
    target_implementation: str | None = Field(default=None, max_length=200)
    target_configuration: dict[str, Any] | None = None
    expected_version: str | None = Field(default=None, max_length=100)


class RuntimeDecisionBody(OperatorFactor):
    reason: str = Field(default="", max_length=500)


class RuntimeActionBody(OperatorFactor):
    reason: str = Field(min_length=1, max_length=500)
    expected_version: str | None = Field(default=None, max_length=100)


class RuntimeDesiredBody(OperatorFactor):
    component_id: str = Field(min_length=1, max_length=200)
    target_state: str
    reason: str = Field(min_length=1, max_length=500)
    target_version: str | None = Field(default=None, max_length=100)


def check_operator_secret(secret: str) -> None:
    """SEC-007: the same second factor as /controls and /execution-mode."""
    expected = os.environ.get("OPERATOR_SECRET", "").strip()
    if not expected:
        raise RuntimeControlError(503, "OPERATOR_SECRET is not configured.")
    if not hmac.compare_digest(secret.encode("utf-8"), expected.encode("utf-8")):
        raise RuntimeControlError(401, "Invalid operator secret.")


def actor_for(body: OperatorFactor) -> Actor:
    check_operator_secret(body.operator_secret)
    if body.actor_kind == "ai":
        return Actor(body.operator, ActorKind.AI, frozenset({ROLE_REQUESTER}))
    return Actor(body.operator, ActorKind.HUMAN, frozenset({ROLE_REQUESTER, ROLE_APPROVER}))


def _action(name: str) -> LifecycleAction:
    try:
        return LifecycleAction(name.upper())
    except ValueError:
        raise RuntimeControlError(422, f"unknown lifecycle action {name!r}") from None


def _state(name: str) -> LifecycleState:
    try:
        return LifecycleState(name.upper())
    except ValueError:
        raise RuntimeControlError(422, f"unknown lifecycle state {name!r}") from None


def component(platform: RuntimePlatform, component_id: str) -> dict[str, Any]:
    record = platform.registry.find(component_id)
    if record is None:
        raise RuntimeControlError(404, f"unknown component {component_id}")
    return {
        **record.to_dict(platform.registry.dependents(component_id)),
        "history": [t.to_dict() for t in platform.registry.history(component_id)],
        "changes": [c.to_dict() for c in platform.changes.changes(component_id)],
    }


def change(platform: RuntimePlatform, change_id: str) -> dict[str, Any]:
    try:
        record = platform.changes.get(change_id)
    except ChangeError:
        raise RuntimeControlError(404, f"unknown change {change_id}") from None
    audit = platform.changes.audit_log(change_id)
    return {**record.to_dict(), "audit": [e.to_dict() for e in audit]}


def _submit(platform: RuntimePlatform, request: ChangeRequest) -> ChangeRecord:
    submitted = platform.changes.submit(request)
    # Taking risk off never waits: a LIVE_SAFE change is APPROVED on
    # submission and executes now, by the actor who asked.
    if submitted.status is ChangeStatus.APPROVED:
        return platform.changes.execute(submitted.change_id, request.actor)
    return submitted


def submit_change(platform: RuntimePlatform, body: RuntimeChangeBody) -> dict[str, Any]:
    actor = actor_for(body)
    action = _action(body.action)
    target = None
    if body.target_version is not None:
        current = platform.registry.find(body.component_id)
        if current is None:
            raise RuntimeControlError(404, f"unknown component {body.component_id}")
        implementation = body.target_implementation or current.version.implementation
        try:
            target = ComponentVersion.create(
                body.target_version, implementation, body.target_configuration
            )
        except RuntimeContractError as exc:
            raise RuntimeControlError(422, str(exc)) from exc
    request = ChangeRequest(
        component_id=body.component_id,
        action=action,
        actor=actor,
        reason=body.reason,
        target_version=target,
        expected_version=body.expected_version,
    )
    return _submit(platform, request).to_dict()


def component_action(
    platform: RuntimePlatform, component_id: str, action: str, body: RuntimeActionBody
) -> dict[str, Any]:
    if action not in COMPONENT_ACTIONS:
        raise RuntimeControlError(422, f"{action!r} is not a component action")
    request = ChangeRequest(
        component_id=component_id,
        action=_action(action),
        actor=actor_for(body),
        reason=body.reason,
        expected_version=body.expected_version,
    )
    return _submit(platform, request).to_dict()


def decide(
    platform: RuntimePlatform, change_id: str, verb: str, body: RuntimeDecisionBody
) -> dict[str, Any]:
    actor = actor_for(body)
    changes = platform.changes
    try:
        if verb == "approve":
            result = changes.approve(change_id, actor)
            # A staged class waits for its shadow/canary result; anything
            # else approved by a human runs now.
            if result.classification not in STAGE_FOR:
                result = changes.execute(change_id, actor)
        elif verb == "execute":
            result = changes.execute(change_id, actor)
        elif verb == "promote":
            result = changes.promote(change_id, actor)
        elif verb == "rollback":
            result = changes.rollback(change_id, actor, body.reason or "operator rollback")
        elif verb == "cancel":
            result = changes.cancel(change_id, actor)
        else:
            raise RuntimeControlError(404, f"unknown decision {verb!r}")
    except UnauthorizedError as exc:
        raise RuntimeControlError(403, str(exc)) from exc
    except ChangeError as exc:
        status = 404 if str(exc).startswith("unknown change") else 409
        raise RuntimeControlError(status, str(exc)) from exc
    return result.to_dict()


def set_desired(platform: RuntimePlatform, body: RuntimeDesiredBody) -> dict[str, Any]:
    actor = actor_for(body)
    try:
        desired = platform.changes.request_desired(
            body.component_id, _state(body.target_state), actor, body.reason, body.target_version
        )
    except UnknownComponentError:
        raise RuntimeControlError(404, f"unknown component {body.component_id}") from None
    except RuntimeContractError as exc:
        raise RuntimeControlError(422, str(exc)) from exc
    return desired.to_dict()


def reconcile(platform: RuntimePlatform, body: OperatorFactor) -> list[dict[str, Any]]:
    actor_for(body)
    return [o.to_dict() for o in platform.reconciler.reconcile_once()]


def trace(platform: RuntimePlatform, trace_id: str) -> dict[str, Any]:
    found = platform.traces.trace(trace_id)
    if found is None:
        raise RuntimeControlError(404, f"no trace {trace_id}")
    return found.to_dict()
