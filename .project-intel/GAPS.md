# Architecture Gaps
> Auto-maintained by Project Intelligence Router
> Agents: read this file for known issues before implementing


## Gap-001 [2026-06-23] — RESOLVED [2026-06-23]

## Gap-002 [2026-06-23] — RESOLVED (verified 2026-06-23, session 2)

## Gap-007 [2026-06-23] — RESOLVED (session 3, same session it was introduced)

## Gap-003 [2026-06-23] — RESOLVED [2026-06-26]

## Gap-004 [2026-06-23] — RESOLVED [2026-06-24]

## Gap-005 [2026-06-23]
No portfolio correlation layer for multi-symbol operation.
Kelly sizing per-symbol ignores cross-asset correlation — correlated drawdowns
breach 2% daily halt faster than per-symbol calculations predict.
Severity: Medium. File: src/risk/ (new file needed)
Status: RESOLVED [2026-07-06] (re-verified). orchestrator.py imports
get_portfolio_correlation() (line 53), computes correlation_scalar per tick
(lines 411-438), passes it to signal_engine.py (line 451), which passes it
to kelly.py for position sizing (signal_engine.py lines 401, 417, 601).
The PortfolioCorrelationTracker IS wired in the live signal path.
GAP-011 was incorrectly marked OPEN in the 2026-06-29 audit — that audit
grepped for direct imports in gates.py, but correlation flows via orchestrator→
signal_engine→kelly, not via a direct gates.py import.
────────────────────────────────────────────────────────────


## Gap-006 [2026-06-23] — PARTIALLY RESOLVED [2026-06-26]

## Gap-008 [2026-06-24] — RESOLVED [2026-06-24]

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

## Gap-011 [2026-06-24] — RESOLVED [2026-06-24]

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

## Gap-001 / TASK-009 / TASK-010 (spread half) — RESOLVED (verified 2026-06-24)

## Gap-013 [2026-06-24] — RESOLVED [2026-06-26]

## Gap-014 [2026-06-24] — RESOLVED [2026-06-24]

## Gap-015 [2026-06-29] — NEW (independent audit session, Claude)
Multiple substantial, unit-tested modules across the risk and intelligence
layers are NOT reachable from any live or paper trade. Verified by grepping
every import site in src/ and tests/, and cross-checking against the fresh
pytest --cov run from this session (not the cached SESSION_STATE.json claims):

1. **src/risk/portfolio_correlation.py** — RESOLVED [2026-07-06]. Wired via
   orchestrator.py → signal_engine.py → kelly.py. The 2026-06-29 audit grep
   was too narrow (looked only at gates.py imports). See GAP-005 note above.
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

UPDATE [2026-07-01]: Re-verified all 5 files directly against source + a
fresh pytest --cov run (not cached claims):
  1. portfolio_correlation.py — RESOLVED. orchestrator.py imports
     get_portfolio_correlation(), computes correlation_scalar per tick,
     passes through signal_engine.py -> SignalContext. Committed (clean
     git diff). 99% coverage. tests/test_orchestrator.py (12 tests) added
     and passing this session.
  2. intelligence_gates.py (ExchangeStressGate/WhaleActivityGate) and
     probabilistic_gates.py (ProbabilisticGate7/8) — these implemented a
     SECOND, different version (Bayesian) of the same Gate 7/8 that
     gates.py already ships via check_exchange_stress()/check_whale_activity()
     (deterministic, fed by signal_engine.py, called in the live
     ordered_results gate sequence — confirmed committed, confirmed in the
     actual gate stack). Wiring both versions in would mean two competing
     implementations of the same risk check running simultaneously.
     DECISION: retired as dead code. Deleted src/risk/intelligence_gates.py,
     src/risk/probabilistic_gates.py, tests/test_probabilistic_gates_coverage.py;
     removed the corresponding test classes from
     tests/test_trade_auditor_and_intel_gates.py (renamed to
     tests/test_trade_auditor.py, trade_auditor coverage retained intact).
     Full suite re-run after deletion: 770 passed / 1 skipped / 1 pre-existing
     unrelated failure (test_signal_engine.py::TestTask010FundingRateWiring —
     hits live Binance API instead of mocking it, confirmed failing
     identically before this session's changes too; separate issue, not
     caused by this cleanup).
  3. intelligence_features.py, ensemble_predictor.py, causal_inference.py,
     risk_quantification.py — confirmed still genuinely disconnected (0%
     coverage on intelligence_features.py/ensemble_predictor.py; the model
     trains/predicts on 9 base features only, pipeline.py has zero
     references to intelligence_features.py). User decision: wire these in
     (real scope — extend feature pipeline 9->24 features, retrain model).
     User decided: provision Glassnode/CryptoQuant API keys first, then build full historical backfill + retrain. BLOCKED on API key provisioning (user action -- account/billing, outside agent scope). Full scoped 6-step plan recorded in DECISION_LOG.md ("Intelligence feature wiring -- blocked on API provisioning"). GLASSNODE_API_KEY/CRYPTOQUANT_API_KEY placeholders added to .env and .env.example this session.


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
Status: RESOLVED [2026-07-05] — .claude/CLAUDE.md replaced with a single-line
redirect ("See ../CLAUDE.md"). Content drift between the two files is now
structurally impossible.
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
Status: RESOLVED [2026-07-05] — Import graph verified: drift_integration.py/performance_drift.py
ARE wired (see Gap-015 correction note). Disconnected intelligence modules
(causal_inference.py, ensemble_predictor.py, risk_quantification.py, intelligence_features.py)
marked EXPERIMENTAL/UNUSED in MODULE_MAP.json and CONTEXT_PRIMER.md with explicit note that they
are NOT active in the live signal path. Wiring blocked on API key provisioning — see
DECISION_LOG.md "Intelligence feature wiring — blocked on API provisioning". GAP-015 captures
the backfill/wiring plan in full detail.
────────────────────────────────────────────────────────────


## Gap-018 [2026-07-07] — NEW (audit session, Amazon Q)
src/intelligence/onchain/ directory exists in the repo tree but is completely empty
(no __init__.py, no source files). It is referenced by the directory structure and
the intelligence layer architecture, but has no implementation. Any future code that
attempts `from src.intelligence.onchain import ...` will raise ImportError at runtime.
The empty directory is also a misleading signal to contributors that on-chain data
fetching is implemented when it is not.
Severity: Low (no runtime impact today — nothing imports from it; but a maintenance
and documentation clarity gap).
File: src/intelligence/onchain/ (empty)
Status: OPEN — Action: either add __init__.py + stub module with EXPERIMENTAL marker,
or remove the directory and add a note in CONTEXT_PRIMER.md that on-chain fetching
is planned but not yet implemented.
Reported by: Amazon Q [amazonq]
────────────────────────────────────────────────────────────


## Gap-019 [2026-07-07] — NEW (audit session, Amazon Q)
config.py IntelligenceSettings uses env_prefix="INTELLIGENCE_" so the Glassnode key
reads from INTELLIGENCE_GLASSNODE_API_KEY. The .env file (line 33) correctly sets
INTELLIGENCE_GLASSNODE_API_KEY. However, .env.example (lines 22-23) uses the bare
names GLASSNODE_API_KEY and CRYPTOQUANT_API_KEY without the INTELLIGENCE_ prefix —
a new operator copying .env.example will set the wrong env var names and the
IntelligenceSettings fields will silently remain empty strings (default="").
This is a silent misconfiguration: no startup error, no warning, intelligence client
just skips all Glassnode calls with "GLASSNODE_API_KEY not set" log lines.
Severity: Medium (operator onboarding trap — silent failure, not a crash).
File: .env.example (lines 22-23), src/config.py (IntelligenceSettings)
Status: OPEN — Action: update .env.example to use INTELLIGENCE_GLASSNODE_API_KEY
and INTELLIGENCE_CRYPTOQUANT_API_KEY to match the actual env_prefix.
Reported by: Amazon Q [amazonq]
────────────────────────────────────────────────────────────


## Gap-020 [2026-07-07] — NEW (audit session, Amazon Q)
No per-package coverage floors exist in pyproject.toml. The single global
fail_under=60 gate can be satisfied while src/execution/live.py (live order
placement) and src/engine/orchestrator.py (main event loop) remain severely
under-tested. Current coverage: runtime_monitor.py 27%, orchestrator.py ~10%
(per Debt-009 history), live.py 69% (improved from 27% but still below any
reasonable safety-critical floor). The 60% global gate provides false confidence
that the highest-blast-radius files are adequately covered.
Severity: Medium-High (safety-critical path protection gap — mirrors Risk-004).
File: pyproject.toml ([tool.coverage.report])
Status: OPEN — Action: add per-package minimums under [tool.coverage.report]
exclude_also or use pytest-cov --cov-fail-under per path. Recommend:
  src/execution/ → 75%
  src/engine/    → 60%
  src/diagnostics/runtime_monitor.py → 50%
Reported by: Amazon Q [amazonq]
────────────────────────────────────────────────────────────
