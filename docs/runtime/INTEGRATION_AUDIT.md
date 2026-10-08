# Universal Runtime Platform -- production integration audit (phase 19)

Baseline: `main` at `648ebe0` (PR #431). PR #431 built the framework; this
audit maps it against what the running process actually holds, and the
change that follows it wires the two together. Registry ids: RES-021 ..
RES-024.

## What the running process holds

`src/api/main.py` lifespan builds, in order: strategy registry (+ kill
switches, registered in `Orchestrator.startup` from the training baseline),
storage, `Orchestrator` (one `SignalEngine` per active timeframe, each with
its own `ModelRegistry` live slot and optional shadow `ShadowBundle`; the
`CryptoBoxSignalAdapter`, which holds the 18-engine `EngineOrchestrator` when
`CRYPTO_BOX=true`; two `ThreadPoolExecutor`s; on `run()`, one loop per
timeframe plus midnight reset, position monitor, allocation rebalance,
optional orderbook stream / price preview, and the Crypto-Box provider loops
that fill `ProviderCache`), `CryptoIntelligence` when `INTEL_ENABLED` (horizon
`WorkerOrchestrator`, MLflow `upgrade.ModelRegistry`), the API's own tasks
(websocket heartbeat and fan-out, decision-trace consumer) and, when
`SELF_TUNING_ENABLED`, the `AutoTuningScheduler`, which registers the tunable
parameters when it starts.

The ensemble (`EngineOrchestrator.run`) is called from the trading tick and
can only scale the Kelly fraction down or veto the tick (`min(new, old)`, the
E-16 / consensus circuit breaker). Its engines degrade individually: a failed
or timed-out engine is dropped from consensus.

## Gap matrix

Before = `648ebe0`; after = this change. "Real controller" means a controller
that calls the subsystem's own operation; every other component is
observe-only and declares no action.

| Component | Exists | Discovered in production | Dependencies declared | Real controller | Runtime controllable | Safe capabilities | Traceable | Durable state | Reconcile coverage |
|---|---|---|---|---|---|---|---|---|---|
| E-01..E-18 | yes | before: no / after: yes (`engine:E-NN`, positional) | after: provider-cache producer per engine (optional), from the engine sources | no | no (decision below) | none | before: signal stage only / after: per engine, per cycle (`engine` event) | n/a (stateless per cycle) | observed health only |
| consensus | yes | after: yes | after: E-01..E-18 (optional) | no | no | none | after: in the `engine` event | n/a | health |
| risk quantifier | yes | after: yes | after: consensus (req), E-11, E-17 | no | no | none | after: yes | n/a | health |
| signal gate | yes | after: yes | after: consensus, risk quantifier (req), E-16 | no | no | none | after: yes | n/a | health |
| ensemble (Crypto-Box) | yes | after: yes (STOPPED when off) | after: signal gate | no | no (enabled by `CRYPTO_BOX` at start) | none | after: yes | n/a | health |
| strategies | yes | yes | none (no code edge found beyond the kill switch) | before: none / after: kill switch | after: QUARANTINE | QUARANTINE = `disable` (LIVE_SAFE); re-enable only by the gauntlet endpoint | killswitch topic | after: desired state re-applied after restart | after: continuous |
| strategy kill switch | yes | as strategy state | -- | after: `disable` added (same path as drift) | via strategy | disable only | killswitch topic | in memory (re-applied from desired state) | via strategy |
| live models (per timeframe) | yes | before: no / after: `model:<tf>/live` | -- | no | no | none: only a passed shadow takes the slot, inside `SignalEngine` | structlog + decision log | n/a | observed version |
| shadow candidates | yes | before: no / after: `model:<tf>/candidate/<id>` | after: its live slot (req) | after: `discard_shadow_now` | after: STOP | STOP (LIVE_SAFE); no ACTIVATE (decision below) | -- | desired state cleared with the candidate | continuous; retired when dropped |
| tuning parameters | yes | yes, but only those registered before the platform (none in practice) / after: all, as they register | none | no | no | none: values move only through `TuningRunner` | selftuning topic | `VersionedConfigStore` (unchanged) | observed version (promotions recorded) |
| self-tuning (pause switch) | yes | before: no / after: `tuning:self_tuning` | -- | after: `set_paused_now` | after: PAUSE / RESUME | PAUSE LIVE_SAFE, RESUME LIVE_GATED | selftuning audit log | after: pause survives restart via desired state | continuous |
| online trainer / retrains | yes | after: retrain tasks as `task:manual_retrain_*` | -- | no | no | none | structlog | online-learner files (unchanged) | observed |
| upgrade registry (MLflow) | yes (INTEL_ENABLED) | before: no / after: yes, listed off the event loop | -- | no | no | none | -- | MLflow | observed |
| worker pools | yes | before: no / after: `worker:training`, `worker:ensemble`, `worker:intel_horizons` | -- | no | no | none (a poisoned ensemble pool is UNHEALTHY) | -- | n/a | health |
| asyncio tasks | yes | before: no / after: orchestrator loops, retrains, API tasks | after: `loop_<tf>` -> live model (req), ensemble (opt); `ws_fanout`, `decision_traces` -> event bus | no | no | none | -- | n/a | observed state |
| providers | yes | before: no / after: `provider:<cache field>` from their loops | engines depend on them | no | no | none | intel topic (OCI providers) | n/a | observed state |
| event bus | yes | yes | consumers depend on it | no | no | none | drop counters | n/a | health |
| order / execution path | yes | no | -- | no | no | none: never a runtime component (RES-019) | trace stages order / fill | executor storage | -- |
| adaptive producers | yes | via their components | -- | -- | -- | -- | -- | -- | see below |
| watchdog / promotion gates | yes | not components | -- | unchanged | -- | -- | -- | unchanged | -- |
| API / control surface | yes | API tasks | -- | -- | -- | -- | runtime topic | -- | -- |
| persistence | yes | -- | -- | -- | -- | -- | -- | desired state, change audit; after: late-component intents deferred, not dropped | -- |
| event / decision tracing | yes | -- | -- | -- | -- | -- | after: `engines` stage | in memory (bounded) | -- |

## Gaps by kind

**Implementation gaps (fixed here)**

* Production discovery covered three sources (strategies, tuning parameters
  registered before startup, the bus). It now covers everything above, and
  `RuntimeLoop` re-discovers every 5 s: late components register, owner-made
  changes (a tuning promotion, a model swap) are recorded as observations
  with the old version kept, and components their owner dropped are retired
  (only when STOPPED, unwanted and depended on by nothing).
* No production controller was wired. Three are, each over an operation the
  subsystem owns (`src/runtime/controllers.py`).
* No production dependency edge existed. Edges now come from the code: the
  provider-cache fields each engine reads (checked against the engine sources
  by RES-021's test), the stage inputs of `EngineOrchestrator.run`, shadow ->
  live, timeframe loop -> live model, bus consumers -> bus.
* Reconciliation ran only on request. `RuntimeLoop` runs one pass per
  interval through the change manager; the first pass runs before the first
  tick, so a quarantine or pause stored before a restart is in force when
  trading resumes.
* `restore()` dropped intents for components not yet registered at startup.
  They are now deferred and applied when the component appears.
* `ChangeManager` rolled a risk-reducing change back when the post-change
  probe was unhealthy -- an automatic RESUME / ACTIVATE that no approver
  saw. A risk reduction is now never undone on health; the probe is audited.
* Per-engine evidence: one `engine` event per ensemble cycle.

**Architectural decisions (deliberate, not gaps)**

* **Engines and the ensemble are observe-only.** Excluding an engine from
  consensus is not a risk reduction: E-16 is the manipulation veto, E-11 and
  E-17 feed the tail-risk score, and every engine moves the consensus price.
  The change classifier treats PAUSE / QUARANTINE as LIVE_SAFE (no approval),
  so declaring them on an engine would let a request remove a veto without
  anyone approving it. A gated engine exclusion would need a new rollout
  class for "pause that adds risk"; that is a product decision, not taken here.
* **No runtime promotion of a model.** `SignalEngine` promotes a shadow
  itself, under its model lock, the tick its own evaluation says it beats the
  incumbent. `ModelRegistry.promote_shadow` only moves ids -- wiring
  `ShadowModelController` to it would have desynchronised the registry from
  the model objects that actually predict. Discarding a candidate is the one
  real operation.
* **No runtime re-enable of a strategy.** Only the gauntlet endpoint; it now
  records the operator's act as desired state so the loop keeps it.
* **Tuning parameters are observe-only.** Values move only through
  `TuningRunner` (gate, shadow mode, probation, watchdog);
  `/self-tuning/rollback` stays the manual revert.
* **Open change records, adaptive candidates and decision traces stay in
  memory.** An approval that survived a restart could execute against a
  world that moved on; after a restart the loop resubmits gated steps from the
  durable desired state, so the approval is asked again. Change history is in
  `audit_log`; decision traces are diagnostics (orders and fills are in the
  executor's storage and the audit trail).

**Open: needs an owner decision**

* **Adaptive producers are not routed through `AdaptiveLifecycle`.** The
  tuning scheduler/runner (Bayesian proposer, factor search), the online
  trainer and the retrain -> shadow path promote autonomously behind their own
  gates. Routing them through `AdaptiveLifecycle` makes every promotion a
  change request that waits for a human approver -- a change to how the bot
  trades, not to how it is wired. The runtime now observes every product of
  those producers (parameter versions, shadow candidates, the self-tuning
  switch) and can pause tuning or discard a candidate; the approval question is
  left to the owner.
