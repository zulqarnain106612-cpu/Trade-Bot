# Adaptive horizon implementation: confirmed gaps

Initial source review started at `d151f319`; the feature branch was then
fast-forwarded to current `origin/main` at `994ad9f` before implementation.
Confirmed gaps were:

- `src/workers/orchestrator.py::_run_horizon_inference` derives direction, confidence, magnitude, and timing from a SHA-256 seeded random generator. It does not call any model.
- `src/models/architectures.py` resolves configured architecture names, and `src/intel.py::CryptoIntelligence` validates that mapping, but `WorkerTask` carries no architecture/model state and the inference worker does not read the mapping.
- `config/horizons.yaml` declares ten intervals; `config/intelligence.yaml` independently restricts the active IDs to six and requests twelve workers. The registries can disagree.
- The worker has a bounded 1,000 item multiprocessing queue, but `CryptoIntelligence.on_bar` submits and collects results serially. Worker errors are converted into a neutral-shaped result, which can be mistaken for a prediction.
- `src/models/online_trainer.py` documents itself as unwired and models bar labels, not horizon-specific matured outcomes.
- `src/intelligence/intelligence_adapter.py` maps V6 values into `intelligence_*` names; its persisted schema and production invocation need separate verification before claiming an integrated learning path.
- ECC defaults in `_ECC_NEUTRAL_FEATURES` are numeric zeros without validity or freshness metadata, so a failed provider is indistinguishable from a valid zero observation.

## Implemented in this change

- The canonical ten horizon registry now selects an actual lightweight `MLPHead`
  for each independent expert. Workers load the horizon and instrument scoped
  artifacts, validate the feature schema, and emit `UNTRAINED`/`FAILED` instead
  of synthetic values when an artifact is unavailable.
- Timestamped, deduplicated market events create per-horizon pending samples.
  Labels use the first source event at or after expiry, and update the matching
  horizon/instrument candidate learner once. Candidate checkpoints are stored
  separately from the active pointer and cannot affect inference before an
  explicit promotion. Forward predictions are attached to their source samples
  so directional out-of-sample accuracy is measurable.
- Fusion aligns by horizon ID and accepts only promoted/ready outputs. No
  promotion path is configured, so the current system abstains rather than
  treating an unvalidated model as trade-ready.
- Missing or invalid portfolio risk inputs suppress sizing. ECC outputs carry
  provider validity and observation time; missing/stale cluster measurements
  are reported as unavailable.
- V6 decision fields round-trip through dedicated nullable SQLite columns and
  `fetch_intelligence_features()`. The market-data adapter serializes per-horizon
  contributions and model/freshness maps.
- The persisted-bar observer serializes concurrent calls and catches up in
  bounded ascending pages instead of skipping directly to the newest bar.
- Production horizon ingestion now reads the verified persisted 1m stream even
  when the primary execution schedule is 15m. Event, sample, cadence, status,
  and training queries retain source-timeframe identity so mixed schedules and
  legacy rows without a known source interval cannot silently calibrate or
  train the active feed's experts.
- Source-event cadence is measured from distinct persisted event timestamps.
  The 30-second expert reports `UNAVAILABLE_CADENCE` until enough source
  events demonstrate a cadence faster than its target; the verified one-minute
  source therefore cannot claim a valid 30-second forecast.
- Matured labels persist before fitting. Production fitting runs on one
  background thread with batches capped at 100; durable `trained=0` rows allow
  interrupted updates to be retried, and shutdown waits for the active batch.

## Still not established

- Paired challenger/incumbent shadow evaluation and a reversible promotion
  lifecycle are now implemented in the learning store and connected to the
  existing human-approved runtime change flow. The code records paired
  forward metrics, requires a minimum sample count and fresh compatible
  evidence, activates only into probation, retains the incumbent pointer, and
  recovers interrupted probation by rolling back. Regression coverage has been
  added, but repository-authorized tests have not run; this remains
  **implemented, unverified**. No model is claimed tradeable from this work.
- The production adapter still has no validated provider for portfolio CVaR,
  drawdown, daily loss, ATR, and odds as one timestamped risk contract.
  `CryptoIntelligence.on_bar()` now abstains when that contract is missing,
  stale, malformed, non-finite, or outside its valid bounds. Therefore this
  intelligence path remains analytics-only and cannot authorize execution.
- The exchange adapter currently supplies persisted one-minute OHLCV bars. The
  30-second horizon is now explicitly unavailable on that feed. A supported
  timestamped sub-minute provider is still required before that expert can
  produce eligible forecasts or labels.
- The feature event store keeps normalized bar snapshots and matured labels;
  it does not yet persist raw exchange, order-book, or external provider events
  for complete replay. ECC features are observable metadata and are not model
  inputs in the current lightweight expert.
- The V6 feature columns are available to storage consumers, but existing
  model training does not yet consume these decision columns as inputs.

These limits are deliberate fail-closed boundaries, not evidence of a completed
promotion or live-trading path.

## Current local database evidence (read-only inspection)

Inspected `data/trade_bot.db` through SQLite read-only mode. It contains
140,997 bars and 84 `intelligence_features_history` rows; the horizon event and
sample tables do not exist in this database. The available BTC/USDT one-minute
series contains 131,672 bars from 2026-06-26 12:32 UTC through 2026-09-25
23:03 UTC, with a 60-second median, minimum, and maximum observed interval.
The following counts are source snapshots that have a later one-minute
observation at or after the configured target expiry. They describe potential
causal labels from OHLCV only; no warm-start was run and none of these counts
are trained samples.

| Horizon | Potential OHLCV label pairs |
| --- | ---: |
| 30s | 131,671 (unavailable: source cadence is 60s) |
| 2m | 131,670 |
| 5m | 131,667 |
| 15m | 131,657 |
| 1h | 131,612 |
| 4h | 131,432 |
| 1D | 130,232 |
| 3D | 127,352 |
| 1W | 121,592 |
| 1M | 88,472 |

An isolated, bounded warm-start command now reads SQLite bars in ascending
pages and writes only to an artifact root outside the repository. It is
restartable and does not activate its output. It has not yet been executed or
validated by the repository checks; no training evidence is claimed. Current
machine inspection found 11 GiB RAM, 941 MiB free / 3.5 GiB available, and
4 GiB swap fully consumed (about 5.6 MiB free), with a `code` process using
about 1.16 GiB RSS and high CPU. The historical replay is not safe to launch
until this pressure is explained and reduced; the production database remains
untouched.

## Remaining implementation plan

1. **Isolated historical warm-start.** The read-only paginated replay command
   is implemented, uses a separate artifact root, persists `matured_at`, skips
   targets shorter than source cadence, and resumes by event identity. Run its
   focused regression through the approved CI/local-check flow, then measure it
   on a disposable database/artifact copy. Keep production storage unchanged
   until those checks pass. Report coverage and trained counts separately.
2. **Challenger evaluation and promotion verification.** Run the new focused
   lifecycle regressions through the authorized PR/local-check route. Confirm
   paired metrics, sample/freshness/schema gates, human approval, atomic
   probation, rollback, concurrency refusal, and restart recovery before
   changing this item to verified.
3. **Critical-path isolation.** Event persistence and fitting are now
   separated, with one trainer thread and batches capped at 100; pending
   samples live in SQLite instead of an in-memory job queue. Add backlog-age
   and saturation alarms, verify storage-error and worker-death recovery, and
   measure queue age, fitting latency, startup latency and process-tree RSS
   against the 1 GiB memory ceiling before PAPER replay.
4. **Decision and risk contract.** Keep one fused decision and the current
   central authorization/execution path. Identify and wire a supported source
   for timestamped portfolio CVaR, drawdown, daily loss, ATR and odds; until
   then the intelligence path abstains. Regression coverage and an architecture
   check must confirm missing/malformed/stale context abstains and workers
   cannot submit orders.
5. **Data and model contract.** Add source/provider provenance and replay
   fidelity for external/order-book inputs only where a supported source is
   available. Keep ECC validity and freshness separate from zero values.
   Decide explicitly whether persisted V6 decision columns remain outputs;
   do not feed them into training without a causal feature definition.
6. **Verification and delivery.** After PR CI reports its compact failures,
   use only the repository-authorized local-check wrapper. Add isolated
   regressions for schema migration, warm-start/restart, lifecycle races,
   risk abstention and status contracts. Then run the bounded PAPER replay,
   record latency/resource evidence, review the full staged and unstaged diff,
   and deliver the feature branch PR without merging it.
