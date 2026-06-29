# Probabilistic Intelligence Layer — Calibration Audit

**Status**: ✅ ALL ISSUES FOUND DURING THIS AUDIT HAVE BEEN FIXED AND VERIFIED  
**Method**: Adversarial functional testing (deliberately fed extreme/edge-case
inputs to every new probabilistic module before trusting it), not just
syntax/import checks.  
**Why this matters**: a probabilistic risk system that LOOKS sophisticated but
is miscalibrated is more dangerous than a simple deterministic one, because it
invites false confidence. Every issue below was caught by deliberately trying
to break the model, not assumed away.

---

## BUG 1: Confidence formula contradicted the credible interval (probabilistic.py)

**Symptom**: A *healthy* exchange (all indicators normal) produced
P(failure)=1.0% but only 4.4% confidence — i.e. "very likely safe, but I'm
barely sure." That's backwards: a clear-cut case should be reported with high
confidence.

**Root cause**: confidence was computed as a separate, disconnected formula
(`sum of |inputs| / something`) with no mathematical relationship to the
credible interval the model also reports. The two numbers could — and did —
disagree with each other.

**Fix**: confidence is now *derived from* the credible interval width
(`confidence = 1 - normalized_CI_width`), so the two numbers are tied together
by construction and can never contradict.

**Verified**: healthy case confidence rose from 4.4% → 86.1%; monotonicity
(healthy < borderline < crisis probability) preserved.

---

## BUG 2: Model could claim literal 100% probability (Cromwell's Rule violation)

**Symptom**: under an extreme but plausible "FTX-like" crisis input, the model
reported P(failure) and CI-upper that printed as exactly 100.0%. Under an
absurd stress-test input (10x more extreme), the underlying float was
confirmed to literally equal 1.0 in places.

**Why this matters**: a Bayesian model that assigns probability exactly 1
(or 0) to anything can never be updated by future evidence — it has
asserted certainty, which is never epistemically justified from a handful of
historical crisis examples. This is a textbook violation of Cromwell's Rule.

**Fix**: the logit (not the probability) is clipped to ±8 before transforming
through the logistic function, bounding every probability strictly within
(0.0003, 0.9997) — the boundary itself was redesigned, not just patched with a
post-hoc `np.clip(p, 0, 1)`.

**Verified**: stress-tested with 10x-more-extreme-than-realistic inputs;
point estimate, CI lower, and CI upper all confirmed strictly inside (0, 1)
at full float precision.

---

## BUG 3: Credible interval used a Wald (normal) approximation — wrong tool for a bounded probability

**Symptom**: consequence of Bug 2 — the symmetric `prob ± 1.96*se` interval
is a known-bad approximation near 0/1 and required a post-hoc clip to stay in
[0,1], which is exactly the kind of papered-over assumption being removed.

**Fix**: replaced with a **Beta-distribution** credible interval — the
conjugate distribution for a probability, continuous on the open interval
(0,1) by construction, no clipping needed. Interval width is driven by an
explicit **effective sample size** that *shrinks* (more honestly) for input
combinations far outside the historical crisis data the model was calibrated
on, rather than naively growing confidence with extremity.

**Verified**: re-ran all three original scenarios; CI never touches exact 0/1
bound; width behaves sensibly with extremity.

---

## BUG 4: Regime detector used hard if/elif thresholds → literal 0%/100% outputs

**Symptom**: `BayesianRegimeDetection.detect_regime()` returned
`bear: 1.0, neutral: 0.0, bull: 0.0` with **100% confidence** from only four
noisy indicators — a far more serious version of Bug 2, in the multi-class
setting.

**Root cause**: each indicator used `if x > threshold: regime_scores[r] += w`
— discrete step functions that can saturate a regime's score to exactly zero
contribution from any given indicator, and the normalization had no floor.

**Fix**: replaced with an **ordered logistic (proportional-odds) model** — the
standard statistical tool for an ordinal outcome (bear < neutral < bull) —
combined with a small **Dirichlet smoothing** term so no regime can ever
receive exactly zero probability mass.

**Verified**: same bull/bear scenarios still classified correctly and
smoothly (88.7% / 96.2% confidence, not 100%); every regime retains nonzero
probability even under absurd extreme inputs.

---

## BUG 5: Naive return annualization amplified sampling noise 252x

**Symptom**: caught by my OWN test of Bug 4's fix — a deliberately
*neutral* 60-day synthetic return series (mean ≈ 0, by construction) was
classified as 88.8% "bear" with high confidence. The regime model was working
as designed, but being fed a garbage signal.

**Root cause**: `mean_return = returns_series.mean() * 252` annualizes the
signal — but it annualizes the *sampling noise* by the same 252x factor. For
n=60 days, the standard error of the mean is amplified to ~0.65 (larger than
the model's entire decision scale), so a truly neutral process can swing
wildly by pure chance.

**Fix**: replaced the raw annualized mean with a proper **one-sample
t-statistic** (`mean / standard_error`), the standard tool for "is this signal
reliably different from zero given how much data and noise we actually have."
Short/noisy windows are now naturally downweighted; long, consistent trends
still come through clearly.

**Verified**: across 20 random seeds of a genuinely neutral 60-day process,
max single-regime dominance dropped from one outlier hitting 88.8% confidence
to a 35-44% confidence range, roughly balanced 9/11 bear/bull split (no
artificial bias). A 90-day *strong, sustained* bull trend is still correctly
and confidently classified as bull (65.3%) — sensitivity was not sacrificed.

---

## BUG 6: Causal effect estimate was 50% contaminated by the confound it claimed to remove

**Symptom** (the most serious bug found): on synthetic data with a **known
ground-truth direct effect of exactly 0.000** and a confounded naive
correlation of 0.849, the reported "adjusted causal effect" was **0.422** —
and the reported 95% CI `[0.835, 0.862]` didn't even contain that point
estimate.

**Root cause** (two compounding issues):
1. `estimate_treatment_effect()` unconditionally averaged a correct
   confounder-adjusted backdoor regression (which alone recovered -0.005, very
   close to truth) with a fake "instrumental variable" method that, absent a
   real instrument, was implemented as literally `corrcoef(treatment, outcome)`
   — exactly the confounded quantity adjustment exists to remove. Averaging
   them silently reintroduced ~50% of the bias while still labeling the result
   "adjusted."
2. The bootstrap confidence interval resampled and recomputed the *raw
   correlation*, not the backdoor-adjusted estimator actually being reported —
   so the CI and point estimate were answering two different questions.

**Fix**:
- Backdoor adjustment (the textbook-correct method given a valid confounder
  set) is now the sole primary estimate.
- True instrumental-variable estimation (proper two-stage least squares) is
  only performed when the caller supplies a **genuine instrument** array — it
  is never silently substituted with a non-instrument, and never auto-blended
  into the primary estimate.
- Bootstrap CI now resamples and recomputes the *same* backdoor estimator used
  for the point estimate, so the interval is internally consistent.
- `is_robust` was also redefined: it previously compared bias-removed against
  the (possibly near-zero) point estimate, which absurdly flagged the
  *best-case* outcome ("large confound, correctly and fully removed") as
  "not robust." It now compares CI precision against the scale of the original
  unadjusted signal instead.

**Verified**:
- Confounded synthetic test: adjusted effect now -0.005 (true=0.000), CI
  `[-0.063, 0.050]` genuinely contains the point estimate, `is_robust=True`.
- Genuine-instrument synthetic test (true effect=0.700): backdoor recovers
  0.700 exactly, independent 2SLS cross-check recovers 0.697.
- Tiny/noisy-sample synthetic test: correctly flagged `is_robust=False`.

---

## BUG 7: EnsemblePredictor crashed on construction (ZeroDivisionError)

**Symptom**: `EnsemblePredictor()` raised `ZeroDivisionError` inside its own
`__init__` — the class could not be instantiated at all before being fitted.

**Root cause**: at cold start every model reports `rmse=inf`. The weighting
formula `1/(rmse+0.01)` correctly evaluates to `0.0` for each model, but then
`total = sum([0.0, 0.0, 0.0]) = 0.0`, and the normalization step `w/total`
computes `0.0/0.0`, which Python raises as `ZeroDivisionError` for native
floats (not `NaN`, as one might assume).

**Fix**: added an explicit cold-start fallback — when no model has reported
finite performance yet, weights default to equal weighting rather than
crashing or producing `NaN`.

**Verified**: construction and `.predict()` both run cleanly at cold start;
weights sum to exactly 1.0.

---

## BUG 8: EnsemblePredictor.fit() silently failed for ARIMA and LSTM

**Symptom**: after calling `.fit(X, y)`, weights remained at uniform
33.3%/33.3%/33.3% — implying nothing had actually been learned, even though
no exception surfaced.

**Root cause** (two issues):
1. `ARIMAPredictor.fit()` takes a single univariate `timeseries` argument (it
   models the target's own autocorrelation), but was being called as
   `model.fit(X, y)` — a `TypeError` on every call, silently swallowed by a
   broad `except Exception`.
2. `LSTMPredictor.fit()` expects pre-windowed 3D sequences
   `(n_samples, lookback, 1)`, but was being handed the raw tabular
   `DataFrame` — shape mismatch, also silently swallowed.
3. (Compounding) `EnsemblePredictor.fit()` never called `_update_weights()`
   at the end — weights only refreshed on the *next* `.predict()` call, which
   could mislead a caller who checks `.weights` immediately after fitting.

**Fix**: `fit()` now dispatches each model with the input shape it actually
requires (ARIMA gets `y` alone; LSTM gets properly windowed sequences built
by a new `_build_lstm_sequences()` helper; XGBoost keeps the tabular `(X, y)`
call), and refreshes `.weights` immediately after fitting completes.

**Verified**: ARIMA now fits successfully (rmse=2.215, previously always
failed); XGBoost fits and dominates the weighting appropriately given its
lower error (96.9% weight vs ARIMA's 3.1%); LSTM correctly reports its real
environment limitation (TensorFlow not installed) instead of a shape-mismatch
crash; weights are correct immediately after `.fit()`, no longer stale.

---

## CROSS-CUTTING PRINCIPLE APPLIED THROUGHOUT

Every fix above followed the same discipline, directly per your instruction
to reduce assumptions to zero wherever doing so doesn't cost capability:

1. **Find the hidden assumption.** (a hard threshold, a fake placeholder, a
   mismatched interface, an unbounded approximation)
2. **Check whether removing it loses anything.** In every case here, removing
   it *only* removed a source of miscalibration — no case required keeping an
   assumption to preserve real functionality. (Per your instruction: "if
   arresting will make it more perfect, do it immediately.")
3. **Replace with the statistically correct tool**, not a quick patch: Beta
   intervals instead of clipped-normal, ordered logit instead of if/elif,
   t-statistics instead of raw means, genuine 2SLS instead of a fake IV
   placeholder, equal-weight fallback instead of a silent crash.
4. **Verify numerically against a constructed scenario with a KNOWN ground
   truth** (synthetic confounded data with a known true effect, a
   deliberately neutral return series, absurd-extreme stress inputs) — not
   just "it runs without an exception."

---

## FILES MODIFIED IN THIS AUDIT

| File | Bugs Fixed | Status |
|------|-----------|--------|
| `src/intelligence/probabilistic.py` | 1, 2, 3, 4, 5 | ✅ Verified |
| `src/intelligence/causal_inference.py` | 6 | ✅ Verified |
| `src/intelligence/ensemble_predictor.py` | 7, 8 | ✅ Verified |
| `src/intelligence/risk_quantification.py` | (audited, no bugs found) | ✅ Verified |
| `src/risk/probabilistic_gates.py` | (consumer of above; re-verified end-to-end) | ✅ Verified, 21/21 existing tests pass |

## TEST EVIDENCE

- 8/8 adversarial regression checks pass (constructed in this session)
- 21/21 pre-existing `tests/test_probabilistic_gates_coverage.py` tests pass
  unmodified against the fixed implementation
- `src/risk/probabilistic_gates.py`: 85% line coverage from that test file alone
