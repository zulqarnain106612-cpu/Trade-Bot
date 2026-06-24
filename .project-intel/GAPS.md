# Architecture Gaps
> Auto-maintained by Project Intelligence Router
> Agents: read this file for known issues before implementing

## Gap-001 [2026-06-23] — RESOLVED [2026-06-23]
No slippage + market impact model in live.py.
Almgren-Chriss model needed: slippage_bps = spread + impact * sqrt(qty/adv_20d).
Severity: High. File: src/execution/live.py, src/risk/
Status: RESOLVED — src/risk/slippage.py (SlippageModel.estimate / veto_if_negative_ev),
wired into src/risk/gates.py as gate 0 (check_slippage_veto). Settings added to
RiskSettings (slippage_default_spread_bps, slippage_impact_coeff_bps,
slippage_veto_margin_bps). 18 tests, 100% line coverage on the new module.
NOTE: live.py execution itself is not yet calling SlippageModel directly —
the gate exists and fails open (passes) until a call site supplies an
expected_edge_bps + SlippageEstimate. Wiring the signal engine / live
executor to actually populate these fields is tracked as a new follow-up
task (see OPEN_TASKS.md).
────────────────────────────────────────────────────────────

## Gap-002 [2026-06-23] — RESOLVED (verified 2026-06-23, session 2)
GaussianHMM regime has no posterior entropy gate.
hmm.predict_proba() entropy not computed — confidence not quantified.
Risk: regime misclassification during transitions blocks or opens positions incorrectly.
Severity: High. File: src/regime/detector.py
Status: RESOLVED — implemented in commit 99ad2a5 (entropy field on
RegimePrediction, normalized Shannon entropy, position_scalar() helper) and
wired into src/risk/kelly.py compute_position_size(regime_scalar=...).
CORRECTION: SESSION_STATE.json from the previous session still listed this
as "NOT STARTED" and as the next recommended task — that was stale. Verified
directly against source (src/regime/detector.py lines ~104-541) before
relying on it. SESSION_STATE.json corrected in this session.
Test backfill (session 3): tests/test_detector.py added (32 tests, 31
passed/1 skipped) — detector.py coverage 0% → 92%. Covers entropy math,
position_scalar() continuity/monotonicity, fit/predict/save/load, and the
non-convergence fail-safe path.

## Gap-007 [2026-06-23] — RESOLVED (session 3, same session it was introduced)
CognitiveEngine (src/risk/cognitive_engine.py, added session 2 alongside
the RAG/intel layer, commit 88a5e73 area) computed position size via an
INDEPENDENT continuous-Kelly formula (_base_size(): mu/sigma^2) completely
decoupled from kelly_result.adjusted_fraction — the real, already
entropy-gated half-Kelly fraction from src/risk/kelly.py. signal_engine.py
then rescaled kelly_result by adjusted_size_fraction / kelly_result.adjusted_fraction,
a ratio between two unrelated formulas with no risk-coherent meaning.
Verified numerically: a realistic scenario (p_long=0.62, edge=24bps,
vol=45%, entropy-gated kelly_fraction=0.0784) produced a rescale ratio of
2.23x — the "mandatory risk governor" layer could INFLATE position size
beyond what the entropy gate intended, not just shrink it. This is the
opposite of "never bypass / never weaken a risk gate."
Severity: Critical (money-sizing correctness on every trade in the live
signal path). File: src/risk/cognitive_engine.py, src/engine/signal_engine.py
Status: RESOLVED — CognitiveEngine.evaluate() now starts size_fraction
from a new SignalContext.kelly_adjusted_fraction field (populated from
kelly_result.adjusted_fraction in signal_engine.py) instead of recomputing.
The old continuous-Kelly formula is kept as _continuous_kelly_estimate(),
used ONLY by a new _log_size_divergence() diagnostic (logs WARN if the two
formulas disagree by >50% — useful as a model-calibration signal, never
mutates size). adjusted_size_fraction is now provably bounded to
[0, kelly_adjusted_fraction] — pure multiplicative shrink via WARN (0.70x
each) or zero via VETO, matching the explicit intent that the five
cognitive domains be a mandatory governor on the real trade, not a second
competing estimate.
Also fixed in the same pass: a stale `regime_conf` variable reference
(NameError on the ProbabilityValidator VETO branch) left over from an
interrupted prior edit that renamed the Bayesian-score variables to
dominant_prob/direction_conf.
Tests: tests/test_cognitive_engine.py added (42 tests, all passing,
cognitive_engine.py coverage 0% → 97%), including two targeted regression
tests (test_size_fraction_starts_from_kelly_adjusted_fraction_not_recomputed,
test_size_fraction_never_exceeds_kelly_adjusted_fraction) that will fail
loudly if this regresses.
────────────────────────────────────────────────────────────
────────────────────────────────────────────────────────────

## Gap-003 [2026-06-23]
KS-test drift detection misses label shift.
SignalDebugger detects covariate shift but not feature→return relationship change.
Rolling performance-based trigger needed alongside KS test.
Severity: Medium. File: src/diagnostics/signal_debugger.py
Status: OPEN
────────────────────────────────────────────────────────────

## Gap-004 [2026-06-23]
No order state machine in live executor.
No FSM tracking PENDING→SUBMITTED→PARTIAL_FILL→FILLED→CLOSED|REJECTED|TIMEOUT.
Partial fills corrupt Kelly denominator (position size vs intended size diverge).
Severity: High. File: src/execution/live.py
Status: OPEN
────────────────────────────────────────────────────────────

## Gap-005 [2026-06-23]
No portfolio correlation layer for multi-symbol operation.
Kelly sizing per-symbol ignores cross-asset correlation — correlated drawdowns
breach 2% daily halt faster than per-symbol calculations predict.
Severity: Medium. File: src/risk/ (new file needed)
Status: OPEN
────────────────────────────────────────────────────────────

## Gap-006 [2026-06-23]
SQLite WAL write contention risk at scale.
Under 3 timeframes + audit log + equity updates, SQLite serializes writes.
Migration path to TimescaleDB/QuestDB needed before multi-symbol live.
Severity: Low (paper/single-symbol fine). File: src/data/storage.py
Status: OPEN
────────────────────────────────────────────────────────────

## Gap-008 [2026-06-24] — RESOLVED [2026-06-24]
Double-counted regime confidence scalar — second instance of the GAP-007
risk pattern, found live in src/engine/signal_engine.py.
Verified directly against source:
  1. Line ~302: regime_scalar = regime.position_scalar() (Shannon-entropy-
     based, src/regime/detector.py) is passed into compute_position_size(),
     which already shrinks kelly_result.notional_usd by this factor.
  2. Line ~448 (via src/strategies/filters.py
     regime_position_scalar(), AFML Ch.17 probability-based formula) computes
     a SECOND, independently-derived regime confidence scalar, returned as
     _filter_result["scalar"].
  3. Line ~432-439: this second scalar is applied AGAIN, multiplicatively,
     directly onto the already-scaled kelly_result.notional_usd/quantity.
Unlike GAP-007, both factors are bounded to (0, 1], so this cannot inflate
size beyond the entropy-gated Kelly fraction — but it silently
double-discounts regime uncertainty through two unrelated formulas with
no documented relationship, meaning actual position size in any
low-confidence regime is smaller than either model alone intends, by an
uncalibrated, undocumented compounding factor.
Severity: High (money-sizing correctness, same class as GAP-007, found
live and unresolved — TASK-011 asked the question, this answers it).
File: src/engine/signal_engine.py (lines ~302, ~432-439), 
src/strategies/filters.py (regime_position_scalar, line ~167)
Status: RESOLVED — Option (a) implemented. filters.py _filter_result['scalar'] is now
logged for observability (regime_scalar_filter_logged_only) but NOT applied as a
notional multiplier. Regime sizing is the sole domain of detector.py entropy gate
→ compute_position_size(regime_scalar=...). The redundant second application block
(lines ~472-478) removed from signal_engine.py. 502/502 tests pass.
────────────────────────────────────────────────────────────

## Gap-009 [2026-06-24] — NEW (root cause of Issue-002)
No Linux/macOS dev setup script exists — only setup_dev.ps1 (PowerShell).
This repo is being developed on Ubuntu (verified: whoami host
fujitsu-s752, .venv built against /usr/bin/python3.14 on Linux), yet the
only setup script that runs `pip install ruff pyright pre-commit` AND
`pre-commit install` is Windows-only. This is the direct, confirmed root
cause of Issue-002 (pre-commit hooks never installed) — there has never
been a one-command setup path for this OS that would have installed
them. scripts/ contains autocommit.sh, autofix.sh, claude-commit.sh (all
Claude/automation-session helpers) but no general dev-environment
bootstrap script for bash/zsh.
Severity: Medium (onboarding + defense-in-depth gap — every Linux/Mac
contributor, including the actual development environment this project
is running in, has had no local pre-commit enforcement and no
guaranteed pyright/mypy install path).
File: setup_dev.ps1 (Windows-only), missing: setup_dev.sh
Status: OPEN — Action: port setup_dev.ps1 to a setup_dev.sh
(pip install ruff pyright mypy bandit semgrep pre-commit; pre-commit
install; etc.), or convert to a cross-platform `make setup` /
`python scripts/setup_dev.py` so the install path isn't OS-forked.

## Gap-010 [2026-06-24] — NEW
.gitignore does not cover models/ or logs/ directories at all — only
/data/ is ignored (verified: `git check-ignore` on a test file placed in
each directory exits 1/no-match for both; `git status --short` shows
both as untracked `??` directories, meaning a `git add -A` or `git add .`
would stage everything inside them). .claudeignore DOES list `models/`,
`*.pkl`, `*.joblib`, `logs/`, `*.log` — but .claudeignore only controls
what Claude reads into context, it has zero effect on what git tracks.
This is a real gap: models/artifacts/ is where ModelTrainer presumably
persists XGBoost/HMM model binaries (confirmed dir exists, created
2026-06-24 06:05, same day as this session's training runs), and logs/
is where structlog output could land if file-based logging is ever
enabled — both are exactly the kind of large-binary / potentially
sensitive-runtime-data directories that should never be committed.
Severity: Medium (no secrets confirmed leaked yet — directories are
currently empty of tracked content — but the protection that the
project clearly intends to have, given .claudeignore's explicit list,
does not actually exist at the git level).
File: .gitignore
Status: OPEN — Action: add `/models/` and `/logs/` (or `*.pkl`,
`*.joblib`, `*.log` patterns, matching .claudeignore's existing list) to
.gitignore. Also run `git status` before any future `git add -A` to
confirm nothing in these directories is accidentally staged.

## Gap-011 [2026-06-24] — RESOLVED [2026-06-24]
Paper executor (src/execution/paper.py) simulates fees (_PAPER_FEE_PCT =
0.001, 0.1% taker — matches Binance) but has NO slippage/spread/impact
simulation. Verified: _open_position_internal uses entry_price=
current_price directly (the raw mark/ticker price) with only
entry_fee = current_price * quantity * _PAPER_FEE_PCT subtracted — no
SlippageModel call, no spread widening, no price-impact term anywhere in
paper.py (grep for slippage/spread in the file returns only the fee
constant, confirmed against live.py which at least has the gate-0
machinery even if unwired per GAP-001/TASK-009).
Why this matters: CLAUDE.md's own live-gate criteria require "Paper 30d
minimum" trading before live unlock, and the OOS Sharpe>1.5 / DD<15% /
500+ trades thresholds (trainer.py _check_live_gate) are computed from
backtest data, but the PAPER track record that operators will look at
before manually approving live trading is generated by this executor —
and it fills at a strictly better price than live ever will. This
compounds with Risk-001's already-estimated 5-25 bps/month slippage drag:
paper results will look that much better than live will actually perform,
for the entire 30-day minimum evaluation window.
Severity: High (directly undermines the credibility of the human go/no-go
decision for live trading — same root cause class as GAP-001, applied to
the wrong executor).
File: src/execution/paper.py
Status: RESOLVED — SlippageModel.estimate() wired into paper.py _open_position_internal.
adv_20d and spread_bps params added; simulated_fill_price replaces current_price
as entry_price for both PaperPosition and TradeRecord. Adverse fill direction
correct (long fills higher, short fills lower). 502/502 tests pass.

## Gap-012 [2026-06-24] — NEW
No schema migration system exists for the SQLite database. All 6 tables
(bars, trades, regime_snapshots, model_metrics, equity_curve, audit_log)
are created via `CREATE TABLE IF NOT EXISTS` in src/data/storage.py —
verified no Alembic dependency (not in pyproject.toml or requirements.in),
no migrations/ directory anywhere in the repo, no schema_version tracking
table, no ALTER TABLE statements anywhere in storage.py.
Why this matters now, not just theoretically: data/trade_bot.db already
exists with ~27MB of real data (bars/trades/regime snapshots from actual
runs). The next time a column needs to be added or a constraint changed
(e.g. to support GAP-004's order FSM states, or GAP-005's portfolio
correlation columns), `CREATE TABLE IF NOT EXISTS` will silently no-op
against the existing on-disk schema — the new column simply won't exist,
and the first INSERT/SELECT referencing it will fail with a generic
sqlite3 OperationalError at runtime, with no clear "you need to migrate"
signal pointing back to a missing migration step.
Severity: Medium (not an active bug — schema hasn't changed yet — but a
predictable, sharp-edged failure mode the moment GAP-004/005/006 land,
each of which implies schema changes).
File: src/data/storage.py
Status: OPEN — Action: either adopt a lightweight migration approach
(e.g. a simple `PRAGMA user_version` check + an ordered list of
ALTER TABLE statements run at startup if user_version is behind, which
fits this project's existing aiosqlite/asyncio style without adding
Alembic's sync ORM assumptions) or explicitly document a manual
backup-and-recreate procedure as the supported upgrade path before
GAP-004/005/006 are implemented.
