# Universal runtime platform -- architecture and runbook

`src/runtime` is one model for every long-lived thing the bot runs. It answers
what exists, at which version, in which state, how healthy, depending on what,
able to do what, and whether that is what was asked for -- and it is the only
path through which runtime state is changed. Registry ids: RES-009 .. RES-024
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
| `controllers.py` | the production controllers: strategy QUARANTINE (kill switch `disable`), shadow-candidate STOP (`SignalEngine.discard_shadow_now`), self-tuning PAUSE / RESUME |
| `production.py` | discovery over the running process, the controller wiring, and `RuntimeLoop` (continuous discovery and reconciliation) |
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

## Production integration

What the platform registers in the running process, the dependency edges and
their sources, which components have a real controller and why the others are
observe-only: [`INTEGRATION_AUDIT.md`](INTEGRATION_AUDIT.md).

`RuntimeLoop` (started in the API lifespan, task `runtime_loop`) runs one pass
every 5 s: re-discover, apply stored intents whose component has appeared,
then one reconcile pass through the change manager. A mismatch that was
alerted on (unreachable, rejected, failed) is not resubmitted while it stays
the same, and is retried after 60 passes or on `POST /runtime/reconcile`. A
pass runs on the event loop between ticks and awaits nothing but the flush;
the MLflow listing runs on its own thread. A pass slower than 50 ms is logged
(`runtime.loop_step_slow`).

Subsystem endpoints that change what the loop enforces record the operator's
act as desired state: `POST /strategies/{id}/re-enable` (ACTIVE) and
`/self-tuning/pause` / `resume` (PAUSED / ACTIVE).

## Decision traces

The orchestrator binds a `trace_id` per tick; every event published inside
the tick carries it (`Event.context`). `GET /runtime/traces/{trace_id}` gives
the stages reached (`market_data, features, engines, signal, risk, approval,
order, fill`), the first blocking condition (the first refusing risk gate, or the
signal's skip reason) and the final decision (`FILLED, ORDERED, REJECTED,
NO_TRADE, INCOMPLETE`). The index keeps the latest 500 traces, 200 events
each, and can fall behind (bus drop counters) but never slows a producer.

With `CRYPTO_BOX=true` the `engines` stage is one `engine` event per cycle:
each engine's outcome, latency, direction and confidence (or its error,
`timeout` for a missed SLA), then consensus, the risk quantifier and the
signal gate. The same run record is each engine's health in the registry
(three consecutive failures: UNHEALTHY).

## Recovery

* **Process restart** -- the lifespan rebuilds the platform from discovery,
  restores desired state from `runtime_desired_state` (unusable rows are
  logged per entry, never guessed; rows for components that register later
  wait for them) and runs the first reconcile pass before the orchestrator's
  first tick: risk-off steps (a strategy quarantine, a self-tuning pause)
  execute at once, anything else becomes a change awaiting approval. Change
  audit written before the restart is in `audit_log` (`event_type =
  runtime_change`).
* **Storage unavailable** -- changes still execute; desired state and audit
  stay queued in order and are written by the next flush (every mutating
  `/runtime` call flushes).
* **Platform build failure** -- trading starts anyway; every `/runtime` route
  answers 503 (`api.runtime_platform_unavailable` in the log) and `GET
  /readyz` stays 503 with phase `degraded`: stored operator intents are not
  being enforced.
* **Agent interruption, compaction, stop before commit/push** -- see the
  agent execution protocol (Stop, PreCompact and SessionStart hooks).

## Startup and readiness

`uv run python -m src.api` (host, port and reload from `API_HOST`,
`API_PORT`, `API_RELOAD`) runs the lifespan in `src/api/main.py`, and uvicorn
opens the port only when the lifespan yields. The lifespan therefore does
only what is bounded: settings and secrets, strategy registration, storage,
`Orchestrator.startup(defer_warm_up=True)` (executor, runtime monitor,
pipeline self-test), the security controls, the stored execution mode, the
runtime platform (restore desired state, first reconcile) and the background
tasks. Then `_state.ready` is set and the server listens.

The model warm-up -- history for every active timeframe, the model fits,
the signal engines -- runs first thing in the orchestrator's `run()` task.
The loops that need no model (position monitor, midnight reset, allocation
rebalance, order-book stream) start before it, so open positions are guarded
while models train; the timeframe loops start only after it, so nothing
ticks without its engine. `stop()` ends a warm-up in progress; a warm-up
that fails starts no timeframe loop and says why.

Before this, the lifespan awaited the whole warm-up: a cold PAPER start
measured on 2026-10-11 kept port 8000 closed for 27.7 minutes (REG-0026).

| Layer | Where | True when |
|---|---|---|
| liveness | `GET /livez` | the process answers; constant, touches nothing |
| API | `/readyz` `components.api` | the lifespan finished (above) |
| runtime platform | `components.runtime_platform` | built; `unavailable` otherwise, which is never ready |
| models | `components.models` | per timeframe: `pending` -> `bootstrapping` -> `queued` -> `training` -> `trained` -> `ready`, or `unavailable` / `failed` with the reason |
| trading loops | `components.trading_loops` | the timeframe loops started; per timeframe tick count, first tick, age of the last tick |

`GET /readyz` (API key; `src/api/readiness.py`) answers 200 only when every
layer is up, and 503 otherwise with `phase` (`starting`, `warming_up`,
`degraded`, `failed`, `stopping`) and `reasons`. A first tick is reported,
not required: a 4h timeframe's first tick can be hours away. The dashboard
shows the phase and reasons, or why it cannot ask (unreachable API, rejected
key, an API without `/readyz`).

* **History on restart.** A store whose newest bar is inside the training
  window (2000 bars) refreshes the window plus a day instead of the full
  90-day bootstrap -- every bar training reads is still re-downloaded, the
  gap since the last bar is closed, and an empty or stale store still takes
  the full bootstrap.
* **Tick stalls.** A timeframe loop registers its stall probe when it starts.
  Until the first tick the wait counts from then, and the limit is one bar
  plus 60s (never under 300s), so a healthy 15m or 4h loop is not a stall.
* **Memory.** After the warm-up the process collects garbage and returns
  freed heap pages to the OS (`malloc_trim` on glibc), logged as
  `orchestrator.warm_up_complete released_mb=...`. The optional LSTM ensemble
  member loads the native Torch runtime only when it is fitted; when the
  configured critical RSS limit cannot cover the conservative runtime/tensor
  reserve, it is skipped with `lstm_skipped_memory_budget` and the remaining
  ensemble members are reported as a degraded ensemble. Core direction/meta
  training and deterministic risk gates are not bypassed.
* **Preflight.** `python3 scripts/startup_preflight.py` starts the API in
  PAPER mode with exchange credentials blanked and a one-off API key, and
  reports listening, readiness phases, first tick, peak RSS against
  `MEMORY_CRITICAL_MB` and a graceful shutdown as one JSON line.

## Known limitations

* Change records, adaptive candidates and decision traces are in memory; after
  a restart the change *history* is in `audit_log`, and the loop resubmits
  gated steps from the durable desired state, so an approval is asked again
  rather than replayed.
* Engines, the ensemble, live models, tuning parameters, tasks, workers,
  providers and upgrade artifacts are observe-only; INTEGRATION_AUDIT.md says
  why for each.
* The tuning, retraining and strategy producers promote behind their own
  gates and are not routed through `AdaptiveLifecycle` (an owner decision:
  doing so puts every autonomous promotion behind a human approval).
* Component resource usage, latency and event rate are not reported; health
  comes from adapters and controller probes.
* A probe has no timeout of its own; a controller whose probe can hang must
  bound it.


### Controlled startup preflight evidence (2026-10-11)

On implementation commit `e8162a3377a6ed90b015c85e1a82eb1d754412b6`, the isolated PAPER-mode preflight passed with a clean tracked worktree: API bind 6.0 s, API and Runtime Platform ready 6.1 s, all three timeframe models ready 253.6 s, first 1m tick 256.0 s, peak process-tree RSS 543 MiB against the unchanged 1,024 MiB critical limit, and graceful shutdown in 3.7 s (exit 0, not forced). The XGBoost runtime resource profile was 300 estimators/depth 4/one worker; configured 500/depth 6 settings were preserved. All live credentials were blanked and the preflight used isolated SQLite and model-artifact copies with PAPER mode and live orders disabled.

The short preflight explicitly requires the 1m first tick; the 15m and 4h loops were active but had not yet emitted a first tick at the 260.4-second snapshot. One public OKX instruments request exhausted retries without preventing readiness; OKX health itself remains unverified by this run. See `INTEGRATION_AUDIT.md` for the complete machine-generated summary and limits.

### Repeat preflight against the final runtime implementation (2026-10-11)

A second controlled PAPER-mode run against runtime implementation commit `c307752a90837a614cbca725b2ff26b41650a13f` passed with a clean tracked worktree. It used isolated SQLite/model-artifact snapshots, blank exchange and optional-service credentials, loopback port `18181`, `live_orders_enabled=false`, and the unchanged 1,024 MiB critical limit. API bind/API readiness/Runtime Platform readiness were each 14.1 s; all 1m/15m/4h models were ready at 414.0 s; the first healthy 1m tick was 427.2 s; reconciliation passes were 84; peak process-tree RSS was 553.5 MiB; total elapsed was 431.5 s; shutdown exited 0 in 3.4 s without force. The effective XGBoost profile remained 300 estimators/depth 4/one worker and the configured 500/depth 6 values were unchanged.

The preflight passed without a readiness failure or memory-limit breach. Only the 1m first tick was required and observed; the 15m/4h loop tick counters were still zero at the snapshot, so their first ticks are not claimed as verified. One public OKX instruments request exhausted retries without blocking readiness; OKX endpoint health remains unverified. The machine-generated summary also confirmed PAPER mode, no inherited exchange credentials, isolated storage, and live orders disabled.


## Full-timeframe PAPER acceptance procedure

The default preflight requires the first `1m` tick because a `15m` or `4h` loop deliberately waits for the next UTC-aligned bar close. To make an acceptance run account for every configured timeframe, run this command on the exact commit under review:

```bash
python3 scripts/startup_preflight.py \
  --timeout 1800 \
  --tick-timeout 15000 \
  --require-tick 1m \
  --require-tick 15m \
  --require-tick 4h
```

This is an operational PAPER preflight, not a substitute for CI. It blanks exchange and optional-service credentials, disables live orders, snapshots configured storage/model artifacts into a disposable directory, and stops the API gracefully. The JSON report is SHA-bound and explicitly lists `required_timeframes`, `missing_required_timeframes`, each observed `first_tick_s`, API/runtime/model readiness, peak RSS, and shutdown status. A pass requires every requested tick and clean shutdown; a model-ready response alone does not count as a tick. The 4-hour first tick can require waiting for the next UTC-aligned 4-hour boundary after model warm-up, so the extended timeout is intentional. Repeat the command after the first process has fully exited to check clean-restart behavior; retain both reports with their source SHAs. Do not infer a pass from older measurements or a green frontend build.

The Chromium dashboard suite separately exercises visible readiness, populated operational panels, whole-page scrolling, drag/drop persistence, keyboard resize/move, hide/restore navigation across a remount, terminal-session creation/resizing, and process-center inspect/terminate interactions. Its API and terminal provider are deterministic fixtures; it validates browser interactions, not a live API-to-terminal deployment. Backend readiness, process lifecycle, and graceful API shutdown remain covered by their owning Python tests and the isolated PAPER preflight.
