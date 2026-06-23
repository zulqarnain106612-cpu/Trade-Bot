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
Status: OPEN
