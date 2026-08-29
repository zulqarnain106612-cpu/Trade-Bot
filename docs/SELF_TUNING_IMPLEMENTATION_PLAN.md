# Self-Tuning — Complete Implementation Plan

Tracks docs/SELF_TUNING_DESIGN.md from its current state (Phase 2 shipped,
fully inert) through to a live, operator-supervised self-tuning parameter.
Each phase is independently shippable, independently testable, and leaves
the system safe to stop at if priorities change.

## Status snapshot

| Phase | Scope | State | Commit |
|---|---|---|---|
| 1 | Registry, versioned store, audit log, kill switch | done | `852b739` |
| 2 | Proposer, evaluator, gate, shadow-mode runner | done | `6bdce77` |
| 3 | CPCV backtest harness → real metric samples | done | `095d81f` |
| 4 | Register `hmm.entropy_threshold`, shadow-mode soak | done | `095d81f` |
| 5 | `PostPromotionWatchdog` (auto-rollback on live drift) | done | `095d81f` |
| 6 | Operator API surface (`/self-tuning/*`) | done | `095d81f` |
| 7 | Enable live promotion (paper mode) for one parameter | **blocked on operator decision** | — |
| 8 | Expand whitelist, one parameter at a time: (1) `hmm.entropy_scalar_floor` done, (2) `risk.slippage_impact_coeff_bps` done, (3) feature-window params done, (4) XGBoost hyperparams done | ✅ done | `72b6282` (scheduler + item 1), `566d02b` (item 2), `1afdc8a` (item 3), this session (item 4) |

An `AutoTuningScheduler` (`src/tuning/scheduler.py`) runs the daily/hourly
propose→evaluate→gate loop automatically for every parameter with a working
backtest harness, satisfying Phase 4's "wire a scheduled call" requirement
beyond the originally-planned manual-only script. Phase 7 is the one item
that cannot be "implemented" further by code alone: it requires an operator
to actually set `SELF_TUNING_ENABLED=true` and `SELF_TUNING_SHADOW_MODE=false`
against real paper-trading data and watch a probation cycle complete — a
production decision, not a coding task. See "Phase 7 preconditions" below
for what's already true and what still needs a human call.

---

## Phase 3 — CPCV backtest harness (produces real `MetricComparison` inputs)

**Problem it solves:** Phase 2's `ChallengerEvaluator` takes pre-computed
metric samples as input, but nothing yet produces those samples from a
real backtest. Today there's no code path that runs the existing HMM/
XGBoost pipeline twice (once at champion params, once at challenger
params) over the same held-out CPCV folds.

**Files:**
- `src/tuning/backtest_harness.py` — `run_challenger_backtest(param, champion_value, challenger_value) -> tuple[list[MetricComparison], dict]`. Wraps the existing CPCV split logic (`FeatureSettings.cpcv_*`, already used for the live gate) to run the regime detector / model pipeline twice per fold, once per arm, and collect per-fold Sharpe, win-rate, drawdown, calibration-error samples.
- Reuses `src/regime/detector.py`, `src/models/trainer.py`, existing feature pipeline — **no new modeling code**, just re-invocation with a parameter override injected.

**Key design constraint:** the harness must NOT retrain a model per fold
if the parameter under test doesn't require it (e.g. `hmm.entropy_threshold`
only changes position sizing post-hoc, not model weights — cheap to
re-evaluate). Parameters that *do* require retraining (XGBoost hyperparams)
are explicitly the most expensive and lowest-priority in the whitelist —
confirmed by the design doc's parameter table ordering.

**Tests:** `tests/test_tuning_backtest_harness.py` — use a small synthetic
OHLCV fixture (already exists somewhere in `tests/fixtures` per the model
trainer tests — reuse, don't duplicate) to verify the harness produces
correctly-shaped `MetricComparison` lists and respects CPCV purge/embargo.

**Exit criteria:** harness runs standalone, produces comparisons consistent
with hand-computed values on a synthetic fixture with a known injected
effect (e.g. challenger param that should mechanically improve one metric).

---

## Phase 4 — Register `hmm.entropy_threshold`, shadow-mode soak

**Problem it solves:** connects Phase 2/3 to a real parameter and runs the
full pipeline end-to-end, still with zero live effect (`shadow_mode=True`
is hard-coded at this stage, not just default).

**Files:**
- `src/tuning/bootstrap.py` — `register_phase4_parameters(registry)`: registers exactly one `TunableParameter("hmm.entropy_threshold", floor=<0.8x default>, ceiling=<1.2x default>, current=<HMMSettings default>, eval_strategy="cpcv_oos_sharpe")`. Explicit function, not import-time side effect — matches the Phase 1 invariant of never silently registering parameters.
- Wire a scheduled call (e.g. daily, via existing cron/scheduler infra if present, or a manual CLI entrypoint `scripts/run_tuning_attempt.py`) that calls `TuningRunner.attempt("hmm.entropy_threshold", ...)` using Phase 3's harness output.
- `primary_metric="oos_sharpe"`, tracked regression metrics = `oos_sharpe`, `max_drawdown_inverted`, `win_rate` (matches design doc §1.3).

**Operating procedure (manual, not automated yet):** run the attempt script
against the current paper-trading data weekly; read `TuningAuditLog` by
hand; confirm proposals are sane (small perturbations, plausible deltas)
and that the harness isn't producing spurious "significant" results on
what should be noise (sanity check against a null-effect control: propose
challenger == champion, verify gate always rejects/no-ops).

**Exit criteria:** minimum 4 consecutive weekly attempts logged with no
harness bugs, no false-positive "significant" results on a same-value
control run, full audit trail reviewed by a human. This is the step that
validates the *harness itself* is trustworthy before it's allowed to
influence anything real — matches design doc §7 step 2 exactly.

---

## Phase 5 — PostPromotionWatchdog (auto-rollback)

**Problem it solves:** nothing today watches a promoted parameter for
live regression. Required *before* Phase 7 (live promotion) can be safely
enabled — do not skip this to get to live faster.

**Files:**
- `src/tuning/watchdog.py` — `PostPromotionWatchdog`, wraps `PerformanceDriftDetector` (reused, not reimplemented): after a promotion, tracks a probation window (`SelfTuningSettings.probation_trades` / `probation_hours`, already defined in Phase 1 config) against the *new* baseline. On drift detection within probation, calls `VersionedConfigStore.rollback()` and writes a `ROLLED_BACK` audit entry, then locks that parameter (no new proposals) for a cooldown period.
- Hook point: wherever live trade outcomes are already recorded for `DriftIntegrationAdapter` (`src/risk/drift_integration.py`) — the watchdog subscribes to the same trade-close event, scoped to params currently in probation.

**Tests:** `tests/test_tuning_watchdog.py` — simulate a promoted parameter, feed synthetic post-promotion trade outcomes that drift below baseline, assert rollback fires and the parameter is locked; assert a healthy post-promotion sequence clears probation without any store mutation.

**Exit criteria:** watchdog unit-tested in isolation; still not wired to
anything live yet (Phase 4's shadow mode means there's nothing to watch
in production regardless).

---

## Phase 6 — Operator API surface

**Files:**
- Extend `src/api/main.py` with:
  - `GET /self-tuning/status` — registry state + `VersionedConfigStore` current values + probation status + last N audit entries per param.
  - `POST /self-tuning/pause` / `/self-tuning/resume` — flips an in-memory `RuntimeConfig`-style flag (same asyncio.Lock pattern as `execution_mode`), independent of the `.env` kill switch so an operator doesn't need a restart to pause.
  - `POST /self-tuning/rollback/{param}` — manual forced revert, calls `VersionedConfigStore.rollback()` directly, operator-secret gated (same auth pattern as `/risk-controls`).
- Auth: reuse existing `api_key_header` dependency; this is not a new auth mechanism.

**Tests:** `tests/test_api_self_tuning.py`, following the existing pattern in whatever test file covers `/risk-controls` today (mirror its structure).

**Exit criteria:** endpoints pass the same security review bar as
`/risk-controls` (auth required, no wildcard CORS exposure, rate-limited
consistent with the rest of `/api/main.py`).

---

## Phase 7 — Enable live promotion, paper mode, one parameter

**This is the first phase with real behavioral effect.** Everything before
it is silent or manual-review-only.

Preconditions (all must hold, checked by a human, not automated):
- Phase 4 shadow soak exit criteria met.
- Phase 5 watchdog is wired and unit-tested.
- Phase 6 API surface gives the operator live visibility + a kill switch
  that doesn't require a restart.
- `SELF_TUNING_ENABLED=true` set explicitly (still requires `.env` edit +
  restart, same ceremony as `TRADING_MODE=live` — intentionally not made
  easier).

Change: flip `TuningRunner(shadow_mode=False)` for `hmm.entropy_threshold`
only, in `TRADING_MODE=paper`. Run for **at least one full probation
cycle** (per `SelfTuningSettings.probation_trades`/`probation_hours`)
before evaluating whether to proceed to Phase 8.

**Exit criteria:** at least one promotion cycle completes with no
watchdog rollback, and the promoted value's effect is explainable (not
just "the gate said yes") when reviewed against the audit trail.

---

## Phase 8 — Expand whitelist

One parameter at a time, repeating Phases 4→7's sequence per parameter,
never batch-enabling multiple parameters simultaneously (design doc §7:
"isolates which change caused which effect"). Suggested order, cheapest/
lowest-risk first:

1. Done — `hmm.entropy_scalar_floor` (paired with entropy_threshold, same eval cost):
   registered by `src/tuning/bootstrap.py::register_hmm_entropy_scalar_floor`,
   scheduled by `AutoTuningScheduler`.
2. Done — `risk.slippage_impact_coeff_bps` (was flagged as a TODO recalibration
   target in `config.py`): `src/tuning/backtest_harness.py::run_slippage_coeff_backtest`
   recalibrates the Almgren-Chriss impact coefficient against realized fill
   cost. For each historical trade, `SlippageFillSample.fill_price` (the
   trade's recorded entry price) is compared against `reference_price` (the
   close of the most recent bar at/before entry, via the new `bars_before()`
   storage method — no separate arrival-price capture exists on the
   live/paper path today, so this is a proxy, not a live tick). Two
   CPCV-folded metrics gate promotion: `slippage_prediction_accuracy`
   (negative mean absolute error, primary) and `slippage_prediction_bias`
   (negative absolute mean signed error — systematic over/under-estimation,
   distinct from raw magnitude). Wired into `AutoTuningScheduler` alongside
   the entropy parameters.
3. Done — `features.*_window` params (vwap, ofi, atr, sharpe, volume,
   one at a time): `src/tuning/backtest_harness.py::run_feature_window_backtest`.
   Scored against the currently deployed FROZEN direction model (loaded
   via `ModelTrainer.load_direction`, resolved the same way
   `orchestrator.py` does via `StorageSettings.model_dir`) — this is
   sensitivity-to-input testing, not "would a retrained model do
   better" (see risk 1 below, unresolved by design, not by this
   implementation). Skips cleanly when no model has been trained yet
   (`FileNotFoundError` is an expected state on a fresh deployment).
   See "Phase 8 item 3 — detailed scope" below for the full design.
4. Done — the eight `xgboost.*` hyperparameters (`n_estimators`, `max_depth`,
   `learning_rate`, `subsample`, `colsample_bytree`, `min_child_weight`,
   `reg_alpha`, `reg_lambda`): `src/tuning/backtest_harness.py::run_xgboost_hyperparam_backtest`
   reuses `ModelTrainer.train_direction`'s existing CPCV harness directly
   — champion and challenger `XGBoostSettings` variants are each fully
   retrained via real CPCV (not a frozen-model sensitivity test like
   items 1-3), so this is a faithful "would a retrained model generalize
   better" comparison. This is the most expensive parameter group (real
   `XGBClassifier.fit()` calls, not vectorised replay), so:
     - It only runs every `xgboost_cycle_interval`-th scheduler cycle
       (default 24), not every interval tick.
     - It runs via `loop.run_in_executor` so a multi-second-to-minutes
       retrain doesn't block the asyncio event loop the live API/trading
       loop shares.
     - `n_estimators` / `max_depth` / `min_child_weight` are int fields;
       the scheduler rounds proposal values before `XGBoostSettings.model_copy()`,
       which does not re-validate. Bounds use each field's own Pydantic
       constraint where one exists (e.g. `max_depth` clamped to [1, 20]),
       or a finite safety ceiling for fields with no upper bound — never
       `math.inf`, which would let the proposer's step computation
       produce inf/NaN.

Never added: anything in `EXCLUDED_PARAMS` (`src/tuning/registry.py`) —
this list is not revisited by this plan under any phase. Phase 8 is now
complete; the only remaining item in this plan is Phase 7 (see below).

**Bug found and fixed while wiring item 3 into the scheduler:**
`AutoTuningScheduler._attempt_all()` previously `return`ed immediately
whenever entropy's trade-sample count was insufficient, which silently
skipped the slippage AND feature-window attempts too on every cycle a
deployment had too few closed trades — even though feature-window tuning
only needs bar history, not trade history. Fixed by giving each of the
three parameter groups its own independent guard (see
`src/tuning/scheduler.py::_attempt_all`); regression test:
`tests/test_tuning_scheduler.py::TestAutoTuningSchedulerAttempts::test_insufficient_entropy_samples_does_not_skip_slippage_attempt`.

### Phase 8 item 3 — detailed scope (features.*_window params)

**Goal:** recalibrate one of the five rolling-window parameters feeding
`BASE_FEATURE_COLUMNS` — `vwap_window`, `ofi_window`, `atr_window`,
`sharpe_window`, `volume_zscore_window` — against the currently deployed,
already-trained direction model's out-of-sample predictive quality,
without retraining (matches the design doc's assumed cost ordering: window
params are supposed to be cheaper than XGBoost hyperparameters, which
require a retrain per candidate — see risk 1 below on what that
assumption actually buys you).

**Reusable, no new logic needed:**
- The five per-column functions in `src/features/pipeline.py`
  (`vwap_deviation_zscore`, `order_flow_imbalance`, `atr_momentum`,
  `rolling_sharpe`, `volume_zscore`) each take `window` as an explicit
  argument and operate independently on raw OHLCV series — confirmed no
  shared state across columns, so swapping one column's window doesn't
  require recomputing the other six.
- `build_feature_matrix()` already produces the full baseline feature
  dataframe plus `log_returns` (single-bar log return, `COL_RETURN`) from
  raw OHLCV. Call it once with production `FeatureSettings` for the
  baseline; only the one target column gets recomputed with the
  challenger window and swapped in.
- `ModelTrainer.load_direction(model_dir, symbol, timeframe)` loads the
  currently deployed frozen model exactly the way
  `src/engine/orchestrator.py` (around `model_dir =
  self._cfg.storage.model_dir` / `ModelTrainer.load_direction(model_dir,
  self._symbol, tf.value)`) already does — the harness must resolve the
  active model the same way, not invent a second "which model is live"
  mechanism. `StorageSettings.model_dir` (default `models/artifacts`) is
  the config field to reuse.
- `src/models/trainer.py::_oos_sharpe_and_drawdown(y_pred, log_returns)`
  is the exact Sharpe formula CPCV training already uses to judge a
  fold's predictive quality (`direction = ±1 from y_pred`, `strat_ret =
  direction * log_returns`, re-entered every bar) — import and reuse
  directly instead of reimplementing a parallel Sharpe calculation.
- `backtest_harness._make_folds` (CPCV purge-gap folding) and
  `ChallengerEvaluator.compare_metric` — same fold/evaluate shape as
  `run_entropy_threshold_backtest` / `run_slippage_coeff_backtest`.
- `ModelTrainer.predict_direction`'s `n_features_in_`-based column
  slicing (GAP-015 backward compatibility for models trained before the
  intelligence-feature expansion) — the harness must mirror this exact
  slicing, not assume a fixed 7-column schema.

**New code required:**
- `src/tuning/backtest_harness.py::run_feature_window_backtest(bars_df, column, champion_window, challenger_window, recompute_fn, direction_model, features_cfg) -> list[MetricComparison]`:
  build baseline once → recompute `column` for champion/challenger via
  `recompute_fn` → align (drop rows where either variant's warmup NaN
  prefix differs; a larger window has a longer NaN prefix) → score both
  variants with the frozen model, mirroring `predict_direction`'s
  slicing → fold via `_make_folds` → per-fold Sharpe
  (`_oos_sharpe_and_drawdown`) + win-rate → `ChallengerEvaluator`.
- `src/tuning/bootstrap.py`: one parametrized
  `register_feature_window_param(registry, field_name, settings)` helper
  covering all 5 field names (not 5 near-duplicate functions), same
  ±20% symmetric-bound convention as the existing `register_*`
  functions, floor clamped to the field's own `ge=2` Pydantic
  constraint.
- `src/tuning/scheduler.py`: fetch raw bars via `storage.fetch_bars` (not
  `fetch_trades` — this harness scores every bar, not realized trades,
  so it isn't limited by however few trades have closed the way the
  entropy/slippage harnesses are). Load the frozen model once per
  attempt cycle; on `FileNotFoundError` (no model trained yet — a
  normal, expected state on a fresh deployment, not an error), record a
  `SKIPPED` audit entry and move on, mirroring
  `orchestrator.py`'s existing `except FileNotFoundError:
  log.warning(...)` handling. Attempt one window parameter at a time per
  interval, same rotation pattern as the two existing parameter groups.
- Tests: `tests/test_tuning_backtest_harness.py` additions (synthetic
  OHLCV + a small fitted classifier fixture — check whether
  `tests/test_model_trainer_coverage.py` already builds a reusable
  synthetic-model fixture before writing a new one), `tests/test_tuning_scheduler.py`
  additions mirroring the slippage-attempt test shape, plus a
  `FileNotFoundError`/no-model-yet skip-path test.

**Key risks/decisions this scope surfaces but does not resolve —
flag before implementing:**

1. **No-retrain caveat.** This measures how sensitive the CURRENTLY
   DEPLOYED model is to a perturbed input distribution — not "would a
   model retrained with this window generalize better." That is a
   materially weaker claim than what Phases 3/4 make for
   `hmm.entropy_threshold` (a pure, deterministic post-hoc function, not
   a learned one being fed a shifted input). The existing ±20%
   symmetric-bound convention keeps challengers close enough to champion
   that this is unlikely to matter in practice, but this is a documented
   simplification inherited from the design doc's own Phase 8 ordering
   assumption, not something this scope resolves — worth a second
   opinion before shipping.
2. **Forward-return proxy has no meta-label gate.** Reusing
   `_oos_sharpe_and_drawdown` means "trade" = a single-bar-ahead
   directional bet, re-entered every bar — the same simplification
   `ModelTrainer._run_cpcv` already uses for its own OOS Sharpe, not a
   full triple-barrier P&L simulation, and it does NOT apply the
   meta-label gate (`p_bet`) the live signal path uses before sizing a
   real trade. Recommend matching `_run_cpcv`'s existing precedent
   (direction-only) for a first implementation; meta-label-gated
   evaluation is a possible later refinement, not a blocker.
3. **Requires a trained model to exist.** Unlike entropy/slippage (which
   only need trade/bar history that accumulates from normal operation),
   this harness is inert until at least one model has been trained and
   saved via `ModelTrainer.save()`. Must skip cleanly, not raise, when
   absent (see FileNotFoundError handling above).
4. **Still one parameter at a time.** Registering all 5 window params
   does not mean running them concurrently — design doc §7 ("isolates
   which change caused which effect") still requires one attempt cycle
   per parameter per interval, same as the two existing parameter
   groups today, just with 5 more names in rotation.

**Exit criteria (once implemented):** harness runs standalone against a
synthetic OHLCV + frozen-model fixture with a known injected effect
(e.g. a challenger window that should mechanically align the feature
closer to what the model was trained on); full validation gate
(`uv run ruff check --fix src/ && uv run mypy src/ && uv run pytest
tests/ -x -q`) at ≥95% coverage, per CLAUDE.md and this plan's
cross-cutting validation section.

## Phase 7 preconditions (status)

- Phase 4 shadow soak: harness code is in place and scheduler-driven; the
  exit criterion itself ("4 consecutive weekly attempts... reviewed by a
  human") requires real paper-trading history and a human review pass —
  not satisfiable by a code change.
- Phase 5 watchdog: wired and unit-tested (`src/tuning/watchdog.py`,
  `tests/test_tuning_watchdog.py`).
- Phase 6 API surface: `/self-tuning/status`, `/pause`, `/resume`,
  `/rollback/{param}` all live in `src/api/main.py`.
- `SELF_TUNING_ENABLED=true` + `SELF_TUNING_SHADOW_MODE=false`: still unset
  by design — this is the explicit operator ceremony the design doc
  requires, deliberately not made easier by this plan.

---

## Cross-cutting validation (every phase)

- `uv run ruff check --fix src/ && uv run mypy src/ && uv run pytest tests/ -x -q` must pass with coverage ≥95% before any commit, per CLAUDE.md.
- Every phase's diff must be reviewable independently — no phase should bundle unrelated changes.
- Every phase that touches `src/tuning/` gets its own `tests/test_tuning_<module>.py`, following the Phase 1/2 pattern already established (dataclasses + pure functions, no live I/O in unit tests, `tmp_path` fixtures for anything file-backed).

## Explicit non-goals (unchanged from design doc §8)

- No online/incremental model-weight updates outside the full CPCV
  retrain + promotion gate.
- No self-tuning of `EXCLUDED_PARAMS`, ever.
- No autonomous widening of a parameter's own registered bounds.
