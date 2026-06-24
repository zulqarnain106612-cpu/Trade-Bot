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
Status: OPEN — Action: `ruff check . --fix` for the ~230 auto-fixable
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
Status: OPEN — Recommend prioritizing signal_engine.py and live.py tests
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
Status: OPEN — Action: pick ONE canonical tool-version source (recommend
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
