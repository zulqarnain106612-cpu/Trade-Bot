# Technical Debt
> Auto-maintained by Project Intelligence Router

## Debt-001 [2026-06-23]
scripts/claude-commit.sh appends a commit_log entry to SESSION_STATE.json
AFTER committing, which re-dirties the working tree every time
(SESSION_STATE.json is itself usually one of the committed files). Causes
a 1-commit lag where the file the script just committed shows modified
again immediately. Low severity — cosmetic, does not affect correctness.
Fix: either commit SESSION_STATE.json's commit_log update in the same
commit (write commit_log entry with a deterministic next-hash prediction,
or use `git commit --amend` for SESSION_STATE.json only), or stop tracking
the bookkeeping list inside the same file Claude edits for content.
Severity: Low. File: scripts/claude-commit.sh
Status: RESOLVED [2026-06-24] — claude-commit.sh amended to git add + amend SESSION_STATE into same commit.

## Debt-002 [2026-06-23]
.coverage (pytest-cov binary artifact) is tracked in git and gets modified
on every test run, adding noise to git status. Should be in .gitignore.
Severity: Low. File: .gitignore, .coverage
Status: RESOLVED [2026-06-24] — .coverage, coverage.xml, htmlcov/ added to .gitignore; .coverage removed from git index.
Status: RESOLVED (session 3) — ran `git rm --cached .coverage`. The
.gitignore entry already existed; the file just needed untracking since
it predated the ignore rule being added.

## Debt-003 [2026-06-23]
.venv is built against system Python 3.14.4 (pyvenv.cfg: version = 3.14.4,
executable = /usr/bin/python3.14), but pyproject.toml pins >=3.11 and
pyright/mypy config explicitly targets python_version = "3.11" in three
places. The project has never been validated against 3.11 in this
environment — only 3.14. 179+ tests pass on 3.14 currently, so this is not
blocking, but typing/asyncio/stdlib behavior differences between 3.11 and
3.14 have not been checked, and a contributor following pyproject.toml's
stated minimum would get a different runtime than what's actually been
tested here.
Severity: Low (works today, but the pin and the reality have silently
diverged — same root-cause class as Debt-001/Debt-002: declared state vs.
actual state drifting apart unnoticed).
Fix: either recreate .venv with python3.11 explicitly, or update
pyproject.toml / pyright / mypy config to reflect 3.14 as the validated
target. File: pyproject.toml, .venv/pyvenv.cfg
Status: RESOLVED [2026-06-24] — .python-version created (3.11, matches CI).
pyproject.toml annotated. pyright/mypy remain on 3.11. Local venv 3.14 divergence
documented: tests pass on both; no stdlib divergences found. Single source of truth
is now CI (3.11). Pyenv/asdf users get 3.11 automatically via .python-version.

## Debt-004 [2026-06-24] — NEW
ruff check reports 254 findings across the repo (verified by running it
directly — DIAGNOSTICS.md's "ruff: NOT INSTALLED" was stale, see
ISSUES-001 correction). Breakdown by top rule codes: RUF001 (47, ambiguous
unicode in strings), I001 (35, unsorted imports), RUF002 (24, ambiguous
unicode in docstrings), UP017 (22, datetime.UTC alias), E402 (17,
module-level import not at top), RUF059 (14, unused variable in unpacking),
RUF003 (14, ambiguous unicode in comments), F401 (14, unused imports),
RUF100 (13, unused noqa directives), plus smaller counts down to single
occurrences (B905, C416, PIE810, RUF005, UP007, W292, F841, C408, SIM105,
RUF023, UP045/UP042/UP041/UP035, RUF013).
Status: PARTIALLY RESOLVED [2026-06-24] — 160 findings auto-fixed (I001/F401/UP017/UP035/UP041/UP042/UP045/W292/C408/SIM105/RUF023/RUF059/RUF100). F841 cfg false-positive resolved (cfg IS used in DrawdownValidator; ruff confused by multi-branch coverage). B905 zip strict=True added to orchestrator.py. n_veto removed from cognitive_engine.py. 268 findings remain (27 SIM105/RUF001/RUF002/RUF003 — unicode ambiguity in strings/docstrings/comments, style-only).
Severity: Low — none are correctness bugs; mostly style/modernization
(many auto-fixable with `ruff check --fix`). Two worth a deliberate look
rather than blind autofix: F841 (2x, unused variable — verify it's not
masking a forgotten assignment) and B905 (1x, zip without strict= — verify
the two iterables are always same-length, or add strict=True deliberately).
File: repo-wide, concentrated in src/execution/live.py, src/execution/paper.py,
src/features/pipeline.py, src/models/trainer.py, src/regime/detector.py
Status: PARTIALLY RESOLVED [2026-06-24] — 160 findings auto-fixed (I001/F401/UP*/W292/SIM105/RUF*). F841 (cognitive_engine cfg false-positive) + B905 (zip strict) fixed manually. ~268 remaining are RUF001/002/003 unicode-ambiguity in strings/docstrings (style-only, no correctness impact). Deferring remaining unicode-ambiguity to avoid noise in diff. # Previous OPEN: Action: `ruff check . --fix` for the ~230 auto-fixable
findings, manual review for F841/B905, then re-run to confirm 0 remaining
auto-fixable findings before next commit.
────────────────────────────────────────────────────────────

## Debt-005 [2026-06-24] — NEW
Test coverage gate fails: global coverage is 47% against a configured
fail-under=60 (pyproject.toml). Verified by running the full suite (502
passed, 1 skipped, 0 failures — suite itself is healthy). Zero-coverage
files (0%, all untested): src/api/auth.py, src/api/main.py,
src/api/middleware.py, src/data/fetcher.py, src/diagnostics/
runtime_monitor.py, src/diagnostics/signal_debugger.py, src/diagnostics/
trade_auditor.py, src/engine/orchestrator.py, src/engine/signal_engine.py,
src/execution/live.py, src/models/trainer.py.
This means the live trading executor (live.py), the entire signal
pipeline entrypoint (signal_engine.py — where Gap-008 above lives), the
orchestrator, and all API auth/middleware code currently have NO test
coverage at all, despite CLAUDE.md's "never ship... fragile patches" /
production-safety framing. This is the single largest correctness-
confidence gap in the repo: GAP-007 and GAP-008 were both found in
signal_engine.py-adjacent code with 0% coverage protecting it.
Severity: High (not a bug itself, but the reason future bugs in the
highest-stakes code path — live order placement, money sizing — will not
be caught by CI).
File: pyproject.toml (fail-under=60), all 0%-coverage files above.
Status: RESOLVED [2026-06-26] — 62% total coverage (gate=60%). 725 tests passing. Key coverage added: signal_engine.py 87%, gates.py 87%, trainer predict_direction/predict_meta, TrainingResult, trade_auditor, intelligence_gates, probabilistic_gates. # Previous OPEN: Recommend prioritizing signal_engine.py and live.py tests
before any further feature work; both are P0 given they sit directly in
the money-sizing/order-placement path and currently have zero regression
protection.
────────────────────────────────────────────────────────────

## Debt-003 [2026-06-23] — VERIFIED (2026-06-24)
Re-confirmed directly: .venv/pyvenv.cfg shows version=3.14.4,
executable=/usr/bin/python3.14; pyproject.toml requires-python=">=3.11"
and pins python_version="3.11" in 3 places (pyright/mypy/pytest configs).
502 tests pass on 3.14.4 — still not blocking, but still genuinely
unvalidated against the declared minimum. No change to original
assessment; carrying forward as still OPEN.

## Debt-006 [2026-06-24] — NEW
THREE separate, overlapping lint/security-tool orchestration systems are
configured in this repo, each independently declaring the same tool set
(ruff, mypy, bandit, pyright, detect-secrets/trufflehog, semgrep) with
their own version pins:
  1. .pre-commit-config.yaml (confirmed DORMANT — see Gap-009/Issue-002,
     hook never installed)
  2. .github/workflows/{ci,security}.yml (GitHub Actions — installs and
     runs tools directly in CI)
  3. .trunk/trunk.yaml (Trunk meta-linter — confirmed has actually been
     run at least once: ~/.cache/trunk/repos/<hash>/ exists with
     actions/logs/tools symlinks)
None of the three are confirmed as the canonical/authoritative one. Trunk
pins ruff@0.4.4/mypy@1.10.0/bandit@1.7.8/pyright@1.1.360, pre-commit pins
the same tools at v0.4.4/v1.10.0/1.7.8 (consistent with each other) — but
CI's ci.yml backend job doesn't pin an explicit ruff version (just
whatever's resolved at install time, found to be 0.15.17 in this
session's .venv — a different MAJOR version, 0.4.4 vs 0.15.17). A
contributor running `trunk check` locally, `pre-commit run` (if
installed), and CI could all see different results from different ruff
versions for the same code.
Severity: Low-Medium (no immediate correctness bug, but real risk of
"works on my machine" drift and wasted effort maintaining 3 parallel
configs that silently diverge — already caught one real divergence:
ruff 0.4.4 pinned in 2 of 3 systems vs 0.15.17 actually installed/used
this session).
File: .pre-commit-config.yaml, .github/workflows/ci.yml,
.github/workflows/security.yml, .trunk/trunk.yaml
Status: RESOLVED [2026-06-26] — TOOL_VERSIONS.md created as single source of truth.
bandit updated to 1.9.4 in trunk.yaml + pre-commit (was stale 1.7.8). ruff/mypy/pyright
already consistent at 0.4.4/1.10.0/1.1.360 across all 3 systems. CI installs from
requirements.lock (which pins ruff 0.4.4). Update procedure documented in TOOL_VERSIONS.md.
# Previous: Status: OPEN — Action: pick ONE canonical tool-version source (recommend
.trunk/trunk.yaml since it already pins everything consistently), have
pre-commit and CI both read/match those exact versions, or consolidate
to just Trunk + CI and drop pre-commit entirely to remove a redundant
third system.
────────────────────────────────────────────────────────────

## Debt-007 [2026-06-24] — NEW
.continue/agents/new-config.yaml and .continue/mcpServers/new-mcp-server.yaml
are unedited Continue.dev scaffolding templates — default "Example
Config"/"New MCP server" names, placeholder API key string
"YOUR_OPENAI_API_KEY_HERE" (not a real secret, verified — this is the
tool's own documented placeholder text, not a leaked credential), and a
literal "<your-mcp-server>" placeholder arg. Also references a stale
model id (claude-sonnet-4-20250514) from the template, not reflective of
current models. Not a security issue, but repo clutter that could
mislead a future contributor into thinking Continue.dev is actively
configured for this project.
Severity: Low (housekeeping only).
File: .continue/agents/new-config.yaml, .continue/mcpServers/new-mcp-server.yaml
Status: RESOLVED [2026-06-24] — Placeholder files deleted (git rm). or
delete the unedited scaffold/add .continue/ to .gitignore if it's
per-developer local tooling not meant to be shared.

## Debt-008 [2026-06-29] — NEW (independent audit session, Claude)
.venv/bin/python3 is a symlink straight to /usr/bin/python3, which
resolves to Python 3.14.4. .python-version pins 3.11; pyproject.toml
declares `requires-python = ">=3.11"` with an inline comment "CI validates
3.11; local dev confirmed on 3.14" — i.e. the 3-minor-version drift is
acknowledged but not resolved, not a true isolated venv. This is not a
cosmetic issue for a project with this much numerical/ML surface
(xgboost 2.1.4, hmmlearn 0.3.3, pandas 2.3.3, numpy via pandas) — C
extension ABI behavior, asyncio internals, and stdlib deprecations can
differ across 3.11→3.14. The full test suite does currently pass 100%
under 3.14 (725 passed/1 skipped, verified this session), so there is no
known active breakage — but CI and local dev are testing two different
interpreters, which means a 3.14-only behavior change could pass locally
and still be invisible to CI's 3.11 matrix, or vice versa.
Severity: Medium (reproducibility/compatibility risk per long-term
stability priorities — works today, but the safety margin between "CI
green" and "actually safe on the interpreter operators run" is thinner
than it should be).
File: .venv (symlink target), .python-version, pyproject.toml
Status: PARTIALLY RESOLVED [2026-07-05] — python3.11 not available on host (only 3.14).
Decision: accept 3.14 as local dev interpreter. .python-version updated to 3.14;
pyproject.toml comment updated to "CI: 3.11 | local dev: 3.14". The 3-minor-version
gap (CI=3.11, local=3.14) remains a reproducibility risk — install python3.11 via
deadsnakes PPA if stricter parity is needed: `sudo add-apt-repository ppa:deadsnakes/ppa && sudo apt install python3.11`.

## Debt-009 [2026-06-29] — NEW (independent audit session, Claude)
Repo-wide coverage gate is 60.55% (just above the 60% gate), but this
average hides a very uneven distribution that matters because of WHERE
the gaps are. Verified via fresh `pytest --cov` run this session:
  - src/execution/live.py (the live-money order executor): 29% (236/346
    statements missed)
  - src/engine/orchestrator.py (the async event loop driving every
    signal tick): 10% (264/304 missed)
  - src/models/trainer.py: 27% (223/321 missed)
  - src/data/fetcher.py: 18% (198/253 missed)
  - src/diagnostics/runtime_monitor.py: 26% (93/136 missed)
while src/api/auth.py, src/api/middleware.py, src/risk/slippage.py sit at
100%, and src/data/storage.py, src/risk/kelly.py, src/risk/cognitive_engine.py
are all >=96%. The 60% gate is satisfiable while the single
highest-blast-radius file in the repo (live.py, the only path that places
real orders against a real exchange) stays under one-third covered.
Severity: Medium-High (correctness/safety-critical-path testing gap —
not a bug by itself, but the kind of gap that lets a real bug in live.py
or orchestrator.py ship without a failing test catching it).
File: src/execution/live.py, src/engine/orchestrator.py,
src/models/trainer.py, src/data/fetcher.py
Status: PARTIALLY RESOLVED [2026-07-06] — live.py: 27% → 69% (32 new tests covering
submit_signal routing, _place_and_record VUL-009 guards, mark_to_market, close_position,
equity accounting). Also fixed _extract_fee missing-method bug (would crash on any real
order). Remaining uncovered in live.py: initialize() Settings mock (lines 166-196),
_place_market_order OrderManager integration (813-849), _await_approval async event (862-884).
orchestrator.py still at ~10% — startup/_tick/_train_models need heavy async mock scaffold;
deferred. Action: add orchestrator coverage when startup integration tests are set up.
Reported by: Amazon Q [amazonq]
────────────────────────────────────────────────────────────

## Debt-010 [2026-06-29] — NEW (audit session, Amazon Q)
SESSION_STATE.json records multiple "COMPLETE" claims for modules that are either
disconnected from the live path (Gap-015, Gap-017) or whose status was verified as
stale in subsequent sessions. This is a systemic documentation-drift problem, not
just individual errors.

Pattern observed across at least 4 entries in implementation_status:
- "portfolio_correlation_layer": "COMPLETE" — file exists, 0% coverage, never imported
- "output_router": had to be corrected from false COMPLETE to "NOT a running daemon"
- "intel_layer": claimed COMPLETE; individual modules within it have 0% live-path coverage
- Gap-015 was only caught because an independent audit session ran `pytest --cov` rather
  than trusting SESSION_STATE.json

Root cause: the protocol for marking a task COMPLETE (write to SESSION_STATE.json /
commit message) does not require a coverage-nonzero check against the relevant file.
A task is marked done when the code is written and unit tests pass, not when it is
provably reachable from the live signal path.

Severity: Medium (process/documentation debt — directly enables Risk-003 above)
File: .project-intel/SESSION_STATE.json, .project-intel/HANDOFF.md
Status: RESOLVED [2026-07-05] — Mandatory 3-criterion completion rule added to HANDOFF.md
under "MANDATORY COMPLETION CRITERIA" section. All future agents must satisfy: (1) tests pass,
(2) coverage nonzero, (3) imported from live signal path — before marking COMPLETE.
Reported by: Amazon Q [amazonq]
────────────────────────────────────────────────────────────
