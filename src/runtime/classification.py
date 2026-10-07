"""
Change classification: which rollout a runtime change needs.

Pure function of the component's record, the action and the dependency
issues the impact analysis found. The order of the rules is the policy:

1. A hot version swap on a component that cannot hot-swap needs a drain
   (when it can drain), a restart (when it can stop) or is forbidden.
2. Anything the lifecycle table or the capabilities refuse is FORBIDDEN.
3. Actions that reduce exposure (pause, deactivate, drain, stop, quarantine)
   are LIVE_SAFE -- an operator or the watchdog must always be able to take
   risk off, so dependency issues never block them.
4. Putting something into service with an unmet dependency is FORBIDDEN.
5. Promoting a model needs a passed shadow stage; activating a strategy or
   engine, or hot-replacing anything on the decision path, needs a canary.
6. Everything else that changes live behaviour is LIVE_GATED (approval).
"""

from __future__ import annotations

from collections.abc import Sequence

from src.runtime.contracts import (
    RUNNING_STATES,
    ChangeClass,
    ComponentRecord,
    ComponentType,
    InvalidTransitionError,
    LifecycleAction,
    UnsupportedActionError,
    next_state,
)

A = LifecycleAction

RISK_REDUCING: frozenset[LifecycleAction] = frozenset(
    {A.PAUSE, A.DEACTIVATE, A.DRAIN, A.STOP, A.QUARANTINE}
)
INTO_SERVICE: frozenset[LifecycleAction] = frozenset(
    {A.START, A.ACTIVATE, A.RESUME, A.RELOAD, A.REPLACE, A.ROLLBACK}
)
NOT_LIVE: frozenset[LifecycleAction] = frozenset({A.VALIDATE, A.INITIALIZE})
DECISION_PATH_TYPES: frozenset[ComponentType] = frozenset(
    {ComponentType.STRATEGY, ComponentType.MODEL, ComponentType.ENGINE, ComponentType.TUNING}
)


def classify(
    record: ComponentRecord, action: LifecycleAction, issues: Sequence[str] = ()
) -> tuple[ChangeClass, tuple[str, ...]]:
    """(class, reasons). Reasons always say why the class was chosen."""
    caps = record.spec.capabilities
    cid = record.component_id
    swap = action in (A.REPLACE, A.ROLLBACK)
    if swap and record.state in RUNNING_STATES and not caps.supports_hot_swap:
        if A.DRAIN in caps.actions:
            return ChangeClass.DRAIN_REQUIRED, (f"{cid} cannot hot-swap; drain it first",)
        if A.STOP in caps.actions:
            return ChangeClass.RESTART_REQUIRED, (f"{cid} cannot hot-swap; stop it first",)
        return ChangeClass.FORBIDDEN, (f"{cid} cannot hot-swap, drain or stop",)
    try:
        next_state(cid, record.state, action, caps)
    except (InvalidTransitionError, UnsupportedActionError) as exc:
        return ChangeClass.FORBIDDEN, (str(exc),)
    if action in RISK_REDUCING:
        return ChangeClass.LIVE_SAFE, (f"{action.value} reduces exposure",)
    if action in NOT_LIVE:
        return ChangeClass.LIVE_SAFE, (f"{action.value} does not touch live behaviour",)
    if issues:
        return ChangeClass.FORBIDDEN, tuple(issues)
    ctype = record.spec.component_type
    if action is A.ACTIVATE and ctype is ComponentType.MODEL:
        return ChangeClass.SHADOW_REQUIRED, ("model promotion needs a passed shadow stage",)
    if action is A.ACTIVATE and ctype in (ComponentType.STRATEGY, ComponentType.ENGINE):
        return ChangeClass.CANARY_REQUIRED, (f"activating a {ctype.value} needs a canary",)
    if action is A.REPLACE and ctype in DECISION_PATH_TYPES and record.state in RUNNING_STATES:
        return ChangeClass.CANARY_REQUIRED, ("hot replace on the decision path needs a canary",)
    return ChangeClass.LIVE_GATED, (f"{action.value} changes live behaviour; approval required",)
