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

## Gap-003 [2026-06-23] — RESOLVED [2026-06-26]
KS-test drift detection misses label shift.
SignalDebugger detects covariate shift but not feature→return relationship change.
Rolling performance-based trigger needed alongside KS test.
Severity: Medium. File: src/diagnostics/signal_debugger.py
Status: RESOLVED [2026-06-26] — LabelShiftDetector added to signal_debugger.py. Tracks rolling win-rate vs training-time baseline (window=100 trades, threshold=0.15 drop). Module-level singleton via get_label_shift_detector(). Fires log warning on drift. Plugged in alongside FeatureDriftMonitor (KS covariate shift). Covers the label-shift gap independently of covariate detection.
────────────────────────────────────────────────────────────

## Gap-004 [2026-06-23] — RESOLVED [2026-06-24]
No order state machine in live executor.
No FSM tracking PENDING→SUBMITTED→PARTIAL_FILL→FILLED→CLOSED|REJECTED|TIMEOUT.
Partial fills corrupt Kelly denominator (position size vs intended size diverge).
Severity: High. File: src/execution/live.py
Status: RESOLVED [2026-06-24] — OrderManager instantiated in LiveExecutor.__init__; FSM fully wired.
────────────────────────────────────────────────────────────

## Gap-005 [2026-06-23]
No portfolio correlation layer for multi-symbol operation.
Kelly sizing per-symbol ignores cross-asset correlation — correlated drawdowns
breach 2% daily halt faster than per-symbol calculations predict.
Severity: Medium. File: src/risk/ (new file needed)
Status: OPEN — REOPENED 2026-06-29 (independent audit session). A prior session's commit (5c38b05, "feat(correlation): add PortfolioCorrelationTracker") and SESSION_STATE.json both claimed this resolved 2026-06-26, but src/risk/portfolio_correlation.py is never imported by gates.py, signal_engine.py, or orchestrator.py, and has 0% test coverage (verified via fresh pytest --cov run + repo-wide grep). The file exists but is fully disconnected — see Gap-015 for full detail. Treat this gap as still genuinely open: per-symbol Kelly sizing still ignores cross-asset correlation in the live signal path today.
────────────────────────────────────────────────────────────

## Gap-006 [2026-06-23] — PARTIALLY RESOLVED [2026-06-26]
SQLite WAL write contention risk at scale.
Under 3 timeframes + audit log + equity updates, SQLite serializes writes.
Migration path to TimescaleDB/QuestDB needed before multi-symbol live.
Severity: Low (paper/single-symbol fine). File: src/data/storage.py
Status: PARTIALLY RESOLVED [2026-06-26] — WAL already enabled. synchronous=FULL→NORMAL (2-4x write throughput, still crash-safe with WAL). cache_size=64MB + mmap_size=256MB + temp_store=MEMORY added. TimescaleDB migration still required before multi-symbol live — deferred until that milestone.
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
Status: RESOLVED [2026-06-24] — setup_dev.sh created (bash, Python 3.11+ check, venv, --require-hashes install).
(pip install ruff pyright mypy bandit semgrep pre-commit; pre-commit
install; etc.), or convert to a cross-platform `make setup` /
`python scripts/setup_dev.py` so the install path isn't OS-forked.

## Gap-010 [2026-06-24] — RESOLVED (verified 2026-06-29, independent audit session)
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
VERIFIED 2026-06-29: .gitignore lines 76-78 now contain `/models/` and `/logs/` with an explicit GAP-010 comment; `git check-ignore -v` confirms both paths are ignored. Closing.

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
Status: RESOLVED [2026-06-24] — PRAGMA user_version migration system implemented. _MIGRATIONS list in storage.py; _run_migrations() auto-applies at startup. v1 (initial schema) + v2 (spread_bps column) registered. 4 migration tests added.

## Gap-008 — RESOLVED (verified 2026-06-24, same session as discovery)
Re-read src/engine/signal_engine.py lines 469-478 directly: fix is committed
and present in working tree (git status clean on this file). filters.py's
regime_position_scalar() is now explicitly logged only
(regime_scalar_filter_logged_only) and the code comment documents the
resolution as "ADR option (a)" — exactly the fix this report recommended.
compute_position_size(regime_scalar=...) (detector.py's entropy scalar)
remains the sole sizing authority. No further action needed.

## Gap-001 / TASK-009 / TASK-010 (spread half) — RESOLVED (verified 2026-06-24)
Re-read src/engine/signal_engine.py lines 248-352 directly:
  - Live order-book spread IS now fetched (_live_ob_spread_bps, line 256)
    and fed into a real SlippageModel().estimate() call (line 341).
  - gate_ctx.slippage_estimate now receives this real estimate (line 363) —
    the slippage veto gate (check_slippage_veto in gates.py) is genuinely
    active, not perpetually fail-open as originally found.
  - expected_edge_bps is computed from real model outputs (p_long,
    avg_win_usd, avg_loss_usd), not hardcoded.
TASK-010 funding_rate_8h=0.0 / basis_pct=0.0 (signal_engine.py line
517-518) — VERIFIED NOT A BUG. Checked src/data/fetcher.py lines
124-155: both Binance and OKX ccxt connectors are hardcoded to
"defaultType": "spot". This bot does not trade perpetual futures, so
funding rate and basis genuinely do not apply — 0.0 is the correct value
for a spot-only system, not a stub. The inline comment ("spot only; wire
basis here for perp trading") accurately documents this. No fix needed
unless/until this project adds perpetual futures support, at which point
these two fields would need real wiring alongside whatever code adds
perp order types.
Status: CLOSED.

## Gap-013 [2026-06-24] — RESOLVED [2026-06-26]
No automated position-exit logic exists anywhere in the running system.
Discovered while investigating GAP-011's exit-side slippage gap and
finding there is no production call site for close_position() at all.

Verified exhaustively:
  - grep for "close_position" across src/ + tests/: the ONLY call sites
    are inside docstring usage examples (paper.py:171, live.py:145) and
    test files (tests/test_paper_executor.py). No orchestrator code, no
    background task, no scheduled check ever calls it in production.
  - grep for "stop_loss" / "take_profit" across the entire src/ tree:
    ZERO matches. No such config field exists in config.py or anywhere
    in risk/. There is no stop-loss or take-profit concept implemented
    at all, despite trade_record fields like exit_reason supporting the
    values 'profit_target' | 'stop_loss' | 'time_exit' | 'manual' (per
    paper.py's own close_position docstring) — the STRINGS exist as
    documented possible reasons, but nothing in the codebase ever
    produces them.
  - mark_to_market() (which updates unrealized_pnl per tick) is called
    from live.py/paper.py/base.py's own internals, but orchestrator.py
    — the top-level event loop — only references "mark_to_market" in a
    single code COMMENT (line 647), never actually calls it. The
    orchestrator's own docstring lists its responsibilities explicitly
    (bootstrap, schedule ticks, route signals, snapshot regime, retrain,
    reset daily equity, stop) and conspicuously does not include
    "monitor/close open positions" anywhere.
  - submit_signal() in both executors only ever OPENS positions; there
    is no check for an existing open position on the same symbol, no
    opposite-direction auto-close, no time-based exit check.

Practical effect: a position opened by this system today (paper or
live) will remain open indefinitely, accruing unrealized PnL that is
never reconciled into realized PnL, UNLESS a human manually calls the
close_position API/method directly. The bot can enter trades but cannot
exit them on its own. This makes every other risk gate downstream of
entry (daily drawdown halt, consecutive-loss halt, position-size cap)
far less protective than they appear, since a single losing position
left open with no stop-loss can drift arbitrarily far against the
account with nothing in the automated system stepping in.

This is almost certainly the single most important unresolved gap found
across all 4 audit rounds — more fundamental than GAP-001/008/011, since
those all assume positions get closed in a reasonably bounded time and
optimize the cost/sizing of that lifecycle, but the lifecycle's closing
half doesn't exist yet.

Severity: CRITICAL.
File: src/engine/orchestrator.py (missing exit-check step entirely),
src/execution/paper.py / live.py (close_position exists and works
correctly when called, it is just never called)
Status: RESOLVED [2026-06-26] — _position_monitor_loop() fully implemented and started in orchestrator.run() (line 271). Runs every position_monitor_interval_s (default 5s). Calls check_position_exit() (gates.py:722) for stop_loss/take_profit/time_exit. Calls executor.close_position() on trigger. RuntimeConfig.get_risk_controls() provides toggleable thresholds. mark_to_market() called before exit check. Race-safe: KeyError on already-closed position is caught. Gap-013 was already resolved before this session; GAPS.md status was stale.
# Previous: Status: OPEN — Action: implement an exit-check step in the orchestrator's
tick loop (or a dedicated lightweight loop running on a faster cadence
than signal generation, e.g. every few seconds against live mark price)
that, for each open position, evaluates:
  1. Stop-loss: unrealized_pnl_pct <= -stop_loss_pct → close_position(reason="stop_loss")
  2. Take-profit: unrealized_pnl_pct >= take_profit_pct → close_position(reason="profit_target")
  3. Time-based exit: now - entry_ts >= max_holding_period → close_position(reason="time_exit")
  4. (Optional) Opposite-direction signal → close_position(reason="signal_reversal") before opening the new position
This requires adding stop_loss_pct/take_profit_pct/max_holding_period_s
to RiskSettings (none currently exist) and wiring mark_to_market() into
the orchestrator's tick loop so unrealized PnL is actually kept current
for the exit checks to evaluate against.

## Gap-014 [2026-06-24] — RESOLVED [2026-06-24]
src/api/main.py could not be imported at all under the currently-installed
fastapi==0.136.3 + pydantic==2.13.4. Discovered while building a TestClient
harness for the new /risk-controls endpoint. Two independent, unrelated
fatal errors, both confirmed pre-existing via `git stash`:

1. Four query-parameter declarations used the old-style
   `Annotated[T, Query(default=X, ...)] = X` pattern (default specified
   BOTH inside Query() and via the trailing `=`). This FastAPI version
   raises a hard AssertionError on app construction for any such
   parameter -- not a deprecation warning, a crash. Affected: /trades
   (symbol, limit, offset params), /equity (limit param), /debug/audit
   (limit param). FIXED: moved each default to be specified in exactly
   one place (the trailing `=`), constraints (ge/le) remain inside Query().

2. Two endpoints (/orders/{order_id}/status, /performance-drift) were
   decorated with `@router.get(...)` where `router` was never defined or
   imported anywhere in the file -- a bare NameError at module load time.
   These appear to be incompletely-integrated additions from whatever
   process added the (still-uncommitted) order_fsm.py/order_manager.py
   files. FIXED: changed to `@app.get(...)`, matching every other endpoint
   in the file.

ADDITIONAL FINDING surfaced while fixing #2: both of these endpoints had
NO api_key_header auth dependency at all -- they would have been the only
two unauthenticated endpoints in the entire API once the NameError was
fixed by someone else without checking. Added
`dependencies=[Depends(api_key_header)]` to both, matching the rest of
the API's auth convention.

NOT fixed (out of scope, flagged only): both endpoints internally
reference `runtime_config.executor` and `runtime_config.drift_adapter`,
neither of which exist on the RuntimeConfig class (verified by reading
it in full). Both references are defensively wrapped in `hasattr()`
checks so they fail soft (return a "not found"/error dict) rather than
crashing -- but the endpoints are effectively non-functional stubs until
whoever is building the order-FSM/drift-adapter integration wires those
attributes onto RuntimeConfig. Left as-is since redesigning someone else's
in-progress integration wasn't requested and the safe fallback behavior
means nothing breaks by leaving it.

Severity: CRITICAL (was) -- this meant `uvicorn src.api.main:app` would
never have started, on the current dependency versions, in any
environment running `pip install -r requirements.in`-equivalent ranges.
Combined with Debt-005 (0% test coverage on src/api/main.py) and SEC-004
(no requirements.lock, so the exact previously-working fastapi/pydantic
version pair was never pinned), this explains how the bug went
undetected: nothing in CI or the test suite has ever actually imported
this module as a whole.
Status: RESOLVED (the 2 import-blocking bug classes). Recommend adding
requirements.lock (SEC-004) now more urgently than before, specifically
to pin fastapi/pydantic to versions confirmed compatible with this
codebase, since the next `pip install` without a lockfile could
reintroduce a similar breaking change from either library.

## Gap-015 [2026-06-29] — NEW (independent audit session, Claude)
Multiple substantial, unit-tested modules across the risk and intelligence
layers are NOT reachable from any live or paper trade. Verified by grepping
every import site in src/ and tests/, and cross-checking against the fresh
pytest --cov run from this session (not the cached SESSION_STATE.json claims):

1. **src/risk/portfolio_correlation.py** (PortfolioCorrelationTracker, 313
   lines) — 0% coverage, 138/138 statements missed. Zero imports anywhere
   in src/ or tests/. The ONLY references in the whole repo are inside
   .project-intel/scripts/ (the automation that generated the false
   "GAP-005 resolved 2026-06-26" claim in SESSION_STATE.json — that claim
   is incorrect; the class is never instantiated, never called from
   gates.py or signal_engine.py, and has no test file). GAP-005 should be
   reopened, not treated as closed.
2. **src/intelligence/ensemble_predictor.py** (163 lines) — 0% coverage,
   163/163 missed, zero imports anywhere outside itself.
3. **src/features/intelligence_features.py** (59 lines) — 0% coverage,
   59/59 missed as a pipeline input (it IS imported by intelligence_gates.py,
   but see #4 — that consumer is itself unreachable).
4. **src/risk/probabilistic_gates.py** and **src/risk/intelligence_gates.py**
   — these are the only consumers of src/intelligence/{causal_inference,
   probabilistic,risk_quantification,metrics}.py. Both gate modules have
   real unit tests (tests/test_probabilistic_gates_coverage.py,
   tests/test_trade_auditor_and_intel_gates.py — 46 tests, commit fb57d03)
   and respectable coverage in isolation. But NEITHER is imported by
   src/risk/gates.py (the actual sequential gate stack — confirmed via
   `grep -n "^def check_" src/risk/gates.py`: only slippage_veto,
   daily_drawdown, consecutive_losses, regime, position_size, live_gate,
   paper_minimum_days, performance_drift, position_exit — no
   probabilistic/intelligence gate among them) or by signal_engine.py or
   orchestrator.py. A signal can pass through the entire live pipeline
   today without these gates ever executing.

Root cause pattern (not just these 5 files): work gets built and given
real unit-test coverage in isolation, then SESSION_STATE.json / commit
messages describe it as "COMPLETE" without the integration step that
would make it reachable from src/engine/signal_engine.py — the one
import path that actually matters for a live trade. This is the same
shape of error SESSION_STATE.json itself already caught once (the
"output_router" daemon that was claimed COMPLETE but never existed as a
process) — it has recurred at the module-wiring level, undetected until
this session's independent coverage-report cross-check.

Severity: High. Not because the disconnected code is dangerous (it's
inert), but because:
  (a) GAP-005 (portfolio correlation risk) is still genuinely unmitigated
      despite being logged as resolved — correlated-drawdown risk across
      symbols is real and undocumented as open;
  (b) the probabilistic/intelligence gate layer represents real risk
      logic (causal inference, risk quantification) that operators may
      believe is protecting live trades because it exists in the repo
      with passing tests, when it provides zero runtime protection;
  (c) ~1000 lines of untested-in-integration code is dead weight on
      every future refactor and a maintenance/audit burden.

File: src/risk/portfolio_correlation.py, src/intelligence/ensemble_predictor.py,
src/features/intelligence_features.py, src/risk/probabilistic_gates.py,
src/risk/intelligence_gates.py
Status: OPEN — Action: for each module, either (1) wire it into
signal_engine.py / gates.py with an integration test proving it affects a
real signal decision, or (2) if not yet ready for production, explicitly
mark it experimental/unused in CONTEXT_PRIMER.md and MODULE_MAP.json so
future sessions and operators don't assume it's active. Reopen GAP-005 in
this file (see correction note added to the original Gap-005 entry).
Recommend: re-run `pytest --cov` after any future "RESOLVED" claim and
confirm the relevant file's coverage is nonzero before accepting the
claim — this is what surfaced the discrepancy this session.
────────────────────────────────────────────────────────────

## Gap-016 [2026-06-29] — NEW (independent audit session, Claude)
.claude/CLAUDE.md claims to be a pointer stub ("PRIMARY INSTRUCTIONS FILE
HAS MOVED ... kept here only for tools that look in .claude/ ... root
file is the source of truth") but `diff` against the root CLAUDE.md shows
it is NOT a clean pointer — it still duplicates the full operational
content (output routing protocol, NEVER/ALWAYS rules, project identity)
with two small textual differences (a missing-vs-present trailing space
after the CHAT routing line; .claude/ version is missing the line "Always
follow project-bound blocks with a brief <chat> summary."). Any tool that
reads .claude/CLAUDE.md instead of root CLAUDE.md gets instructions that
are 95% identical but not byte-identical to the stated source of truth —
the kind of near-duplicate that's easy to miss when one file is edited
and the other isn't.
Also noted in passing: SESSION_STATE.json's `project_health` field
("daemon running (venv python fixed)") and its own `implementation_status.
output_router` field ("NOT a running daemon — no process found... Corrected
from prior false COMPLETE claim") refer to two DIFFERENT daemons (the
project-intel extraction daemon at .project-intel/daemon.pid, confirmed
alive this session as PID 100832, vs. the never-built "tag auto-router"
daemon) but use similar enough language that a future session could
conflate them. Not a functional bug — both statements are individually
accurate about their respective daemons — just a documentation-clarity
risk worth a one-line disambiguation.
Severity: Low (documentation integrity, not a code defect).
File: .claude/CLAUDE.md, CLAUDE.md, .project-intel/SESSION_STATE.json
Status: OPEN — Action: make .claude/CLAUDE.md a literal one-line redirect
(e.g. "See ../CLAUDE.md — this file intentionally left minimal") instead
of a near-duplicate, so drift becomes impossible. Optionally rename
SESSION_STATE.json's two daemon-related fields to disambiguate
(e.g. `intel_extraction_daemon_running` vs `output_router_daemon_exists`).
────────────────────────────────────────────────────────────

## Gap-017 [2026-06-29] — NEW (audit session, Amazon Q)
Multiple risk/intelligence modules exist and pass unit tests in isolation but are
completely unreachable from the live signal path. This was partially documented in
Gap-015, but the scope is broader than that entry captures. Verified by cross-referencing
directory listing against import graph:

- src/intelligence/ directory (6 files: client.py, causal_inference.py, ensemble_predictor.py,
  metrics.py, probabilistic.py, risk_quantification.py + providers/ subdir) — exists as a
  full intelligence layer but src/intelligence/client.py and providers/ have no confirmed
  import consumers in the live path.
- src/risk/drift_integration.py and src/risk/performance_drift.py — present in src/risk/
  CORRECTION 2026-06-29 (re-verified, Claude): This sub-claim is FACTUALLY
  INCORRECT. Both src/risk/drift_integration.py and src/risk/performance_drift.py
  ARE actively imported and used: orchestrator.py imports DriftIntegrationAdapter
  (line 48) and PerformanceDriftDetector (line 52), instantiates the detector at
  line 213, and gates.py's check_performance_drift() (line 660) consumes it. Both
  are live, not orphaned. The src/intelligence/ portion of this entry remains
  accurate and duplicates Gap-015. Recommend closing the drift sub-claim here.
  but not confirmed wired into gates.py sequential gate stack or signal_engine.py.

Root cause: the same pattern identified in Gap-015 (build → unit test → claim COMPLETE
without an integration step). The intelligence layer in particular appears to have been
designed as a major architectural addition but the wiring from signal_engine.py into these
modules is incomplete or absent.

Severity: Medium — not dangerous, but operators reading MODULE_MAP.json or the README
signal architecture diagram may assume these are active runtime components when they are not.
File: src/intelligence/, src/risk/drift_integration.py, src/risk/performance_drift.py
Status: OPEN — Action: run `grep -rn "from src.intelligence" src/` and `grep -rn
"from src.risk.drift_integration" src/` to confirm actual import graph. For any module
not imported from signal_engine.py/orchestrator.py, either wire it in with an integration
test or clearly mark it as EXPERIMENTAL/UNUSED in MODULE_MAP.json.
Reported by: Amazon Q [amazonq]
────────────────────────────────────────────────────────────
