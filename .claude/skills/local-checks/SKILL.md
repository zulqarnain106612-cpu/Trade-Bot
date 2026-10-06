---
name: local-checks
description: Run only PR checks that were non-green on the last completed GitHub run.
---

# Local check contract

Local validation is a CI-round-trip optimization, not a second test policy.

1. Push the PR and let the PR checks finish.
2. Run: python3 scripts/local_checks.py prepare
3. If it reports GREEN, do not run a local check.
4. If it records failures, fix the reported cause first.
5. Run only the recorded failed check with:
   TB_LOCAL_CHECKS=1 python3 scripts/local_checks.py run <failed-check>
6. For the Python test check, the wrapper permits only test paths extracted from the CI failure notice.
7. Never run the full test suite locally to validate a PR fix.
8. Never substitute a different or broader check for an unsupported failed check.
9. Push the fix only after the allowed local check passes.

The repository PreToolUse hook enforces the execution boundary. Direct pytest,
ruff, mypy, frontend test/build, architecture, security, workflow-lint, and
quality-gate commands are refused; only the validated wrapper is allowed for
local check execution.

The plan is runtime state under .claude/.hook-state/ and must never be
committed.
