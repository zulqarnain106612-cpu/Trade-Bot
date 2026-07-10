# Self-Tuning Design — Bot-Controlled Parameter Adaptation

Goal: let the bot adjust *its own* tunable parameters at runtime, subject to a
hard guarantee — **every promoted change must be evidence-backed to be an
improvement, never a regression.** No parameter drifts "sideways" or
degrades behavior; a tuning attempt either measurably helps or is discarded.

This is a design for review before implementation — it touches the trading
core, so it should not land without sign-off on the safety mechanism below.

## 1. Non-negotiable safety invariants

1. **Self-tuning never touches hard risk limits.** `kelly_ceiling`,
   `daily_drawdown_halt_pct`, `consecutive_loss_halt`, `max_position_size_pct`,
   `notional_limit_usd`, and the paper→live gate thresholds stay user-only,
   `.env`-only, forever. Self-tuning operates only on a whitelisted set of
   **soft/adaptive** parameters (§3).
2. **No parameter is mutated in the live path directly.** Every candidate
   change is evaluated as a *challenger* against the current *champion* on
   held-out data before it can be promoted (§4). Nothing the bot "learns"
   affects real trades until it clears the gate.
3. **Promotion requires statistically significant, multi-metric improvement**
   — not just higher OOS Sharpe. A challenger must not be worse than the
   champion on any of: OOS Sharpe, max drawdown, win rate, Kelly-implied
   risk-adjusted return, calibration error. One metric improving while
   another degrades is a rejection, not a trade-off the bot gets to make
   unilaterally.
4. **Every promotion is reversible.** Config is versioned (append-only log,
   not overwrite). If live performance after promotion drifts below the
   pre-promotion baseline (reusing `PerformanceDriftDetector`, inverted as a
   regression check against the *new* baseline), auto-rollback to the prior
   version and freeze self-tuning for that parameter for a cooldown period.
5. **Full audit trail.** Every attempt — proposed, evaluated, promoted or
   rejected, and every auto-rollback — is logged immutably with the evidence
   that justified the decision. This is a compliance requirement for a
   trading system, not an optional nicety.
6. **Rate-limited and killable.** Minimum cooldown between tuning attempts
   per parameter (e.g. no more than once per N closed trades or per T hours,
   whichever is longer — avoids overfitting to short-run noise). A single
   `SELF_TUNING_ENABLED=false` kill switch (env-level, user-only) disables
   the entire subsystem; `POST /self-tuning/pause` gives the operator a
   live, no-restart kill switch too.

If any of these can't be satisfied for a given parameter (e.g. no reliable
held-out evaluation signal exists), that parameter is not eligible for
self-tuning, full stop — it stays user/Claude-only.

## 2. Architecture

```
                     ┌─────────────────────────┐
                     │   ParameterRegistry      │  whitelist + bounds +
                     │   (src/tuning/registry)  │  owner + eval strategy
                     └────────────┬─────────────┘
                                  │
   closed trades ──► ┌────────────▼─────────────┐
   drift signals ──► │      TuningProposer        │  proposes ONE candidate
   regime stats  ──► │  (src/tuning/proposer.py)  │  value within bounds,
                     └────────────┬─────────────┘  using existing stats
                                  │ (candidate, param, rationale)
                     ┌────────────▼─────────────┐
                     │   ChallengerEvaluator      │  backtest/CPCV replay
                     │  (src/tuning/evaluator.py) │  champion vs challenger
                     └────────────┬─────────────┘  on held-out window
                                  │ (verdict, metrics diff)
                     ┌────────────▼─────────────┐
                     │    PromotionGate            │  multi-metric,
                     │  (src/tuning/gate.py)       │  significance-tested
                     └────────────┬─────────────┘  accept/reject
                          accept  │  reject → log + discard
                     ┌────────────▼─────────────┐
                     │  VersionedConfigStore        │  append-only, RuntimeConfig
                     │  (src/tuning/store.py)       │  applied live, no restart
                     └────────────┬─────────────┘
                                  │
                     ┌────────────▼─────────────┐
                     │  PostPromotionWatchdog       │  reuses
                     │  (src/tuning/watchdog.py)    │  PerformanceDriftDetector
                     └───────────────────────────┘  auto-rollback on regression
```

All five modules are new, under `src/tuning/`. They read from existing
infra (`performance_drift.py`, `cognitive_engine.py`, `regime/detector.py`,
CPCV in `features`) rather than duplicating it.

## 3. Parameter whitelist (self-tunable, bounded)

Each entry gets a **hard floor/ceiling set by the user in `.env`**, which
self-tuning can narrow within but never exceed:

| Parameter | Current source | Self-tuning range example | Evaluation signal |
|---|---|---|---|
| `hmm.entropy_threshold` / `entropy_scalar_floor` | `HMMSettings` | ±20% of user-set value | Regime-scalar-adjusted OOS Sharpe |
| `risk.slippage_impact_coeff_bps` | `RiskSettings` | Recalibrate from realized fills (already flagged TODO in `config.py`) | Realized vs. estimated slippage error |
| `features.*_window` (VWAP/OFI/ATR/Sharpe/volume) | `FeatureSettings` | ± a few bars around default | Feature importance stability + OOS accuracy |
| `xgboost.learning_rate`, `n_estimators`, `max_depth` (next retrain only, not live model hot-swap) | `XGBoostSettings` | Bayesian-opt within existing `ge/le` bounds | CPCV OOS Sharpe/accuracy, retrain-time cost |
| Meta-label gate confidence threshold | `CognitiveEngine` validator | narrow range | Precision/recall trade-off on vetoed trades |

Explicitly **excluded forever**: `kelly_multiplier`, `kelly_ceiling`,
drawdown/loss halts, position size caps, live-gate thresholds,
`TRADING_MODE`, exchange credentials, API/network settings.

## 4. Evaluation method (how "improvement" is proven, not asserted)

- Reuse the existing **CPCV** (`FeatureSettings.cpcv_*`) purge/embargo
  scheme — the challenger is never evaluated on data adjacent to its own
  training/proposal window (avoids leakage-driven false positives, per
  AFML Ch.7, the same standard already used for the live gate).
- Reuse `PerformanceDriftDetector`'s statistical tests
  (`_proportion_drop_significant`, Sharpe-drop test) **inverted**: instead
  of "did live performance drop vs. baseline," ask "does challenger beat
  champion by more than noise" using the same significance machinery, so
  there's one trusted stats implementation, not two.
- Minimum sample size before a challenger is even considered: same
  `min_trades_live_gate` order of magnitude, scaled down for shadow
  evaluation but never below a floor that makes the test underpowered.

## 5. Rollback (the "never regress" guarantee in practice)

`PostPromotionWatchdog` runs the same drift check against the *newly
promoted* baseline for a probation window (e.g. next 50 closed trades or 72h,
whichever comes first). Any of:
- Sharpe/accuracy/win-rate drop flagged by `PerformanceDriftDetector`
- Drawdown exceeding the pre-promotion baseline

triggers immediate revert to the last-known-good version from
`VersionedConfigStore`, a cooldown lock on that parameter (no re-proposal
for N days), and an alert (existing `/debug/audit` style logging, extended).

## 6. API surface (operator visibility/control, live)

- `GET /self-tuning/status` — current champion values, pending challengers,
  probation state, full attempt history.
- `POST /self-tuning/pause` / `/resume` — operator kill switch, no restart.
- `POST /self-tuning/rollback/{param}` — manual forced revert.
- `SELF_TUNING_ENABLED` env flag — off by default; must be explicitly
  turned on, same pattern as `TRADING_MODE=live`.

## 7. Rollout plan

1. Ship `ParameterRegistry` + `VersionedConfigStore` + audit log only, with
   **zero live parameters registered** — pure plumbing, testable in
   isolation, no behavioral risk.
2. Add one low-blast-radius parameter (`hmm.entropy_threshold`) end-to-end
   through proposer → evaluator → gate → watchdog, running in **shadow mode
   only** (proposals logged, never promoted) for a full paper-trading cycle
   to validate the harness itself doesn't have false positives/negatives.
3. Enable live promotion for that one parameter, paper mode only, observe
   ≥1 full probation cycle.
4. Expand whitelist one parameter at a time, same shadow→live-paper→
   live-real progression. Never batch-enable multiple parameters at once —
   isolates which change caused which effect.

## 8. What this deliberately does NOT do

- No online/incremental model weight updates (no gradient step ever applied
  from live trade outcomes directly into the production XGBoost/HMM models
  without going through full CPCV retrain + promotion gate).
- No self-tuning of anything in the excluded list in §3, ever, regardless of
  how much "improvement" the bot thinks it sees — hard limits are a human
  policy decision, not an optimization target.
- No autonomous widening of its own bounds. The floor/ceiling in the
  registry is set by the user in `.env`; the bot can propose within it but
  cannot edit the registry's bounds themselves.
