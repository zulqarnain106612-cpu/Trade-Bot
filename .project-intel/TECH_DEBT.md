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
Status: OPEN

## Debt-002 [2026-06-23]
.coverage (pytest-cov binary artifact) is tracked in git and gets modified
on every test run, adding noise to git status. Should be in .gitignore.
Severity: Low. File: .gitignore, .coverage
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
Status: OPEN
