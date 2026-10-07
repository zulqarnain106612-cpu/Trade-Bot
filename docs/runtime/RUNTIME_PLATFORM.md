# Universal runtime platform -- architecture and runbook

`src/runtime` is one model for every long-lived thing the bot runs. It answers
what exists, at which version, in which state, how healthy, depending on what,
able to do what, and whether that is what was asked for -- and it is the only
path through which runtime state is changed. Registry ids: RES-009 .. RES-019
(`config/quality_registry.json`, traced in
`docs/quality/REQUIREMENTS_TRACEABILITY.md`).

Agent-side execution (task manifests, checkpoints, completion, branch audits,
PAUSED / BLOCKED / FAILED / STALE / ABANDONED) is documented separately in
[`docs/agent/AGENT_EXECUTION_PROTOCOL.md`](../agent/AGENT_EXECUTION_PROTOCOL.md).

## Architecture

```
operator / AI client
        |  HTTPS, API key (VIEW_STATUS | CHANGE_RUNTIME) + operator second factor
        v
src/api/main.py  /runtime*  ->  src/api/runtime_control.py
        |
        v
ChangeManager (classify -> dependency impact -> validate -> policy -> approval
        |      -> shadow/canary stage -> execute -> observe -> promote | rollback)
        v
SupervisorSet -> per-type Supervisor -> Controller -> the subsystem's own API
        |
        v
RuntimeRegistry (identity, versions, lifecycle state, desired state, health,
                 transition history) -- events on the bus, audit in AuditTrail
                 and storage audit_log, desired state in runtime_desired_state
```

| Module | Responsibility |
|---|---|
| `contracts.py` | `<type>:<name>` identity, immutable versions, `CapabilitySet`, the lifecycle table, `DesiredState`, `ChangeClass` |
| `registry.py` | the bookkeeping of record; refuses anything the table or capabilities refuse |
| `adapters.py` | read the existing strategy, model, tuning, upgrade, engine, worker, task, provider and event-bus registries -- none is replaced |
| `supervisor.py` | preview, per-component lock, controller call, FAILED / quarantine policy, health probes |
| `dependencies.py`, `classification.py` | dependency graph, cycles, impact analysis, rollout class |
| `changes.py` | the change manager |
| `reconcile.py` | desired-versus-actual diff and the reconciler |
| `persistence.py` | desired state and change audit into the existing storage backends |
| `adaptive.py` | candidates from optimizers, trainers and AI agents through every validation stage |
| `events.py` | runtime transitions and change audit on the `runtime` bus topic and in the hash-chained audit trail |
| `platform.py` | the composition root the API holds |

Correlation and decision tracing live below the runtime layer:
`src/eventbus/envelope.py` (every `Event` carries the correlation ids bound
where it was published) and `src/diagnostics/decision_trace.py`.

## Lifecycle

States: `DISCOVERED, VALIDATED, INITIALIZED, STANDBY, ACTIVE, PAUSED, DRAINED,
STOPPED, FAILED, QUARANTINED`. An action is legal only when the transition
table has the edge **and** the component declares the action. Notable rules:

* an `ACTIVE` component is never stopped directly -- deactivate or drain first;
* `QUARANTINE` is reachable from every state; quarantine is left only by `STOP`;
* `REPLACE` / `ROLLBACK` while running need `supports_hot_swap`, otherwise the
  component must be drained (`DRAIN_REQUIRED`) or stopped (`RESTART_REQUIRED`);
* a version string names one content forever; rolling forward again to a
  version that was rolled back is allowed only with identical content.

What each existing subsystem actually supports is declared by its adapter.
Strategies, engines, tuning parameters, upgrade artifacts, providers, workers,
tasks and the event bus are **observe-only**: a strategy is disabled by its
kill switch and re-enabled only by the promotion gauntlet; a tuning parameter
moves only through the tuning runner's promotion. A shadow model declares
`ACTIVATE` (promotion, gated by `ModelRegistry.evaluate_shadow`) and `STOP`
(discard).

## Requesting a runtime change

Every write needs a trade-authorizing API key, the `CHANGE_RUNTIME`
permission, and the operator second factor (`operator`, `operator_secret`,
SEC-007). `actor_kind` is `human` (default) or `ai`.

| Route | Effect |
|---|---|
| `GET /runtime` | counts by type and state, desired/actual mismatches, open changes, dependency issues |
| `GET /runtime/components[?component_type=]`, `GET /runtime/components/{id}` | component state, desired state, health, history, changes |
| `GET /runtime/dependencies` | edges, start order, cycle, issues |
| `GET /runtime/changes`, `GET /runtime/changes/{id}` | change records with their audit |
| `GET /runtime/traces[?limit=]`, `GET /runtime/traces/{trace_id}` | decision traces |
| `GET /runtime/agent` | Claude session task manifests and the branch-audit ledger (read-only) |
| `POST /runtime/changes` | request a change: `component_id`, `action`, `reason`, optional `target_version`, `target_implementation`, `target_configuration`, `expected_version` |
| `POST /runtime/changes/{id}/{approve,execute,promote,rollback,cancel}` | decide an open change |
| `POST /runtime/components/{id}/{pause,resume,drain,stop,deactivate,quarantine,rollback,start}` | shorthand for a request on one component |
| `POST /runtime/desired` | record intent (`component_id`, `target_state`, `reason`, optional `target_version`) |
| `POST /runtime/reconcile` | one reconciliation pass |

Classification decides what happens next:

| Class | Meaning | What the operator does |
|---|---|---|
| `LIVE_SAFE` | takes risk off (pause, deactivate, drain, stop, quarantine) | nothing -- executes on submission |
| `LIVE_GATED` | changes live behaviour | a human approver approves; it executes, then promote after observation |
| `SHADOW_REQUIRED` | model promotion | approve, then the shadow result is recorded by the evaluator (system), then execute |
| `CANARY_REQUIRED` | strategy/engine activation, hot replace on the decision path | approve, canary result recorded, then execute |
| `DRAIN_REQUIRED` / `RESTART_REQUIRED` | the component cannot hot-swap | drain or stop it first, then request again |
| `FORBIDDEN` | the table, the capabilities or an unmet dependency refuse it | read the reasons; nothing to approve |

An AI actor can request changes and execute approved ones; it can never
approve, record a stage result or promote. Whoever holds `OPERATOR_SECRET` is
treated as the human operator, so that secret must never be given to an
automated client.

One open change per component; a request carrying `expected_version` is
refused once the component has moved (stale version).

## Rollback

* **Automatic** -- after execution, and again at promotion, the supervisor
  probes the component; an `UNHEALTHY` result rolls the change back through
  its inverse action (`REPLACE`->`ROLLBACK`, `ACTIVATE`<->`DEACTIVATE`,
  `PAUSE`<->`RESUME`, `START`->`STOP`) and restores the previous desired state.
* **Manual** -- `POST /runtime/changes/{id}/rollback` on an `EXECUTED` change.
* **Outcomes** -- `ROLLED_BACK`, or `ROLLBACK_FAILED` with the reason when no
  supported inverse exists or the inverse itself failed. `ROLLBACK_FAILED`
  needs a person: inspect `GET /runtime/components/{id}`, then quarantine or
  stop the component (both `LIVE_SAFE`).

## Adaptive lifecycle

A tuned parameter, retrained model or generated strategy becomes live only as
a candidate (`AdaptiveLifecycle`): `BACKTEST -> STRESS -> RED_TEAM -> SHADOW ->
CANARY -> PROMOTION_GATE`, in order, each recorded by the system or a human
approver and never by the proposer. The promotion gate is
`tuning.promotion_gauntlet.evaluate_gauntlet`; a model's shadow stage is
`ModelRegistry.evaluate_shadow`. Promotion is a change request that waits for
a human approver; probation ends by `tuning.watchdog.WatchdogOutcome`
(`CLEARED` promotes, `ROLLED_BACK` rolls back).

## Decision traces

The orchestrator binds a `trace_id` per tick; every event published inside
the tick carries it (`Event.context`). `GET /runtime/traces/{trace_id}` gives
the stages reached (`market_data, features, signal, risk, approval, order,
fill`), the first blocking condition (the first refusing risk gate, or the
signal's skip reason) and the final decision (`FILLED, ORDERED, REJECTED,
NO_TRADE, INCOMPLETE`). The index keeps the latest 500 traces, 200 events
each, and can fall behind (bus drop counters) but never slows a producer.

## Recovery

* **Process restart** -- the lifespan rebuilds the platform from discovery,
  restores desired state from `runtime_desired_state` (unusable rows are
  logged per entry, never guessed). A reconciliation pass
  (`POST /runtime/reconcile`) then drives the gaps: risk-off steps execute at
  once, anything else becomes a change awaiting approval. Change
  audit written before the restart is in `audit_log` (`event_type =
  runtime_change`).
* **Storage unavailable** -- changes still execute; desired state and audit
  stay queued in order and are written by the next flush (every mutating
  `/runtime` call flushes).
* **Platform build failure** -- trading starts anyway; every `/runtime` route
  answers 503 (`api.runtime_platform_unavailable` in the log).
* **Agent interruption, compaction, stop before commit/push** -- see the
  agent execution protocol (Stop, PreCompact and SessionStart hooks).

## Known limitations

* Change records, adaptive candidates and decision traces are in memory; after
  a restart the change *history* is in `audit_log`, but an open change or an
  in-flight candidate must be requested again.
* Live discovery covers what the process exposes publicly: the default
  strategy registry with its kill switch, the tuning parameter registry and the
  event bus. Model registries, engines, worker pools and orchestrator tasks are
  registered by whoever holds them (adapters and controllers exist for each);
  the API lifespan does not reach into the orchestrator's private state.
* Engine-level outputs (E-01..E-18) and consensus appear in a decision trace
  only as the signal stage; a per-engine trace needs those producers to
  publish.
* Reconciliation runs when requested (`POST /runtime/reconcile`); there is no
  periodic reconcile loop.
* Component resource usage, latency and event rate are not reported; health
  comes from adapters and controller probes.
* A probe has no timeout of its own; a controller whose probe can hang must
  bound it.
