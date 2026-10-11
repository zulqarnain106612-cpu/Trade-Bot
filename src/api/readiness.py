"""
What GET /readyz answers: how far this process has come, and why it is not
further.

Four layers, each true only when it is:

* liveness -- the server answers at all (GET /livez; nothing here);
* the API -- the lifespan finished: the security controls are declared up,
  the stored execution mode is restored, the runtime platform has restored
  the operator's desired state and run its first reconcile, and the
  background tasks are running;
* the runtime platform -- built, or unavailable (a failed build leaves the
  control plane off and trading on, so it is reported, never assumed);
* the models and trading loops -- the orchestrator's warm-up (history,
  training, engines) per timeframe, optional-component degradation warnings,
  whether the timeframe loops have started, and each timeframe's first tick.

``ready`` is the conjunction: an API that is up, a runtime platform that
enforces desired state, every active timeframe with a built engine and the
loops started. A first tick is reported, not required -- a 4h timeframe's
first one can be hours away. Pure, so every combination is testable without
a server.
"""

from __future__ import annotations

from typing import Any

# Warm-up states (Orchestrator.readiness()["warm_up"]) still on their way.
_WARMING = frozenset({"pending", "deferred", "running"})
_ENDED_EARLY = frozenset({"failed", "cancelled"})
_RUNTIME_UNAVAILABLE = "runtime platform unavailable: stored operator intents are not enforced"
_NOT_STARTED: dict[str, Any] = {"warm_up": "pending", "error": None, "loops_started": False}


def _reasons_for_models(
    warm_up: str, error: Any, timeframes: dict[str, dict[str, Any]]
) -> list[str]:
    not_ready = [tf for tf, t in timeframes.items() if t["model"] != "ready"]
    if warm_up == "failed":
        return [f"model warm-up failed: {error}"]
    if warm_up == "cancelled":
        return ["model warm-up was cancelled"]
    if warm_up in _WARMING:
        progress = ", ".join(f"{tf} {timeframes[tf]['model']}" for tf in not_ready)
        return [f"model warm-up in progress: {progress or 'queued'}"]
    return [f"{tf}: {timeframes[tf]['detail'] or 'no engine'}" for tf in not_ready]


def readiness_report(
    *,
    api_ready: bool,
    stopping: bool,
    runtime_ready: bool,
    reconcile_passes: int | None,
    orchestrator: dict[str, Any] | None,
    trading_mode: str,
) -> dict[str, Any]:
    reasons: list[str] = []
    if stopping:
        api_state = "stopping"
        reasons.append("the API is shutting down")
    elif api_ready:
        api_state = "ready"
    else:
        api_state = "starting"
        reasons.append("the API is starting")

    runtime: dict[str, Any]
    if runtime_ready:
        runtime = {"state": "ready", "reconcile_passes": reconcile_passes}
    elif api_ready:
        runtime = {"state": "unavailable", "detail": _RUNTIME_UNAVAILABLE}
        reasons.append(_RUNTIME_UNAVAILABLE)
    else:
        runtime = {"state": "starting"}

    orch = orchestrator if orchestrator is not None else _NOT_STARTED
    warm_up = str(orch["warm_up"])
    loops_started = bool(orch["loops_started"])
    timeframes: dict[str, dict[str, Any]] = orch.get("timeframes", {})
    reasons.extend(_reasons_for_models(warm_up, orch["error"], timeframes))
    warnings = [
        f"{tf}: {t['detail']}"
        for tf, t in timeframes.items()
        if t["model"] == "ready" and t.get("detail")
    ]
    all_models_ready = all(t["model"] == "ready" for t in timeframes.values())
    if warm_up == "complete" and not loops_started:
        reasons.append("the timeframe loops are starting")

    if stopping:
        phase = "stopping"
    elif not api_ready:
        phase = "starting"
    elif warm_up in _ENDED_EARLY:
        phase = "failed"
    elif warm_up in _WARMING or (all_models_ready and not loops_started):
        phase = "warming_up"
    elif reasons:
        phase = "degraded"
    else:
        phase = "ready"

    models = {
        "state": warm_up,
        "error": orch["error"],
        "timeframes": {
            tf: {"state": t["model"], "detail": t["detail"]} for tf, t in timeframes.items()
        },
    }
    loops = {
        "state": "running" if loops_started else "waiting",
        "timeframes": {
            tf: {
                "loop": t["loop"],
                "ticks": t["ticks"],
                "first_tick": t["ticks"] > 0,
                "last_tick_age_s": t["last_tick_age_s"],
                "bar_seconds": t["bar_seconds"],
            }
            for tf, t in timeframes.items()
        },
    }
    return {
        "ready": phase == "ready",
        "phase": phase,
        "trading_mode": trading_mode,
        "reasons": reasons,
        "warnings": warnings,
        "components": {
            "api": {"state": api_state},
            "runtime_platform": runtime,
            "models": models,
            "trading_loops": loops,
        },
    }
