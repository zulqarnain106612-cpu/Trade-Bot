# Self-Tuning — Complete Implementation Plan

Tracks docs/SELF_TUNING_DESIGN.md from its current state (Phase 2 shipped,
fully inert) through to a live, operator-supervised self-tuning parameter.
Each phase is independently shippable, independently testable, and leaves
the system safe to stop at if priorities change.

## Status snapshot

| Phase | Scope | State | Commit |
|---|---|---|---|
| 1 | Registry, versioned store, audit log, kill switch | ✅ done | `852b739` |
| 2 | Proposer, evaluator, gate, shadow-mode runner | ✅ done | `6bdce77` |
| 3 | CPCV backtest harness → real metric samples | not started | — |
| 4 | Register `hmm.entropy_threshold`, shadow-mode soak | not started | — |
| 5 | `PostPromotionWatchdog` (auto-rollback on live drift) | not started | — |
| 6 | Operator API surface (`/self-tuning/*`) | not started | — |
| 7 | Enable live promotion (paper mode) for one parameter | not started | — |
| 8 | Expand whitelist, one parameter at a time | not started | — |

Nothing past Phase 2 is implemented. Everything below is the plan for 3–8.

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

1. `hmm.entropy_scalar_floor` (paired with entropy_threshold, same eval cost)
2. `risk.slippage_impact_coeff_bps` (already flagged as a TODO recalibration target in `config.py`)
3. `features.*_window` params (one at a time — vwap, ofi, atr, sharpe, volume)
4. XGBoost hyperparameters (highest cost — requires retraining per candidate; do this last, and only after the harness's compute cost is understood from cheaper parameters)

Never added: anything in `EXCLUDED_PARAMS` (`src/tuning/registry.py`) —
this list is not revisited by this plan under any phase.

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
