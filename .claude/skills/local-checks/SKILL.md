---
name: local-checks
description: Read the exact current PR CI notice; never execute local checks.
---

# GitHub-only check contract

GitHub Actions is the sole execution path for tests, builds, lint, security scans,
workflow validation, and quality gates. Do not run an individual test file, a
test suite, a build, or any check locally.

1. Push the current PR commit and let GitHub Actions publish its CI notice.
2. Run python3 scripts/local_checks.py prepare only when the agent needs the
   notice for the current HEAD. This command may read only the PR's single
   SHA-bound CI notice comment whose hidden SHA marker exactly matches HEAD.
3. The command emits only the visible body of that exact notice and stores that same notice for reliability evidence; it must not fetch
   workflow runs, check results, job logs, annotations, artifacts, or arbitrary
   PR comments into agent context.
4. Fix only the check named in the new notice, then push the fix. GitHub Actions
   will terminate other in-flight watched workflows when a non-green check is
   detected and update the same PR comment with only the current actionable
   failure.
5. When the complete watched workflow set is green, the comment body is exactly
   workflow Succeeded. When the PR is merged, it becomes exactly merged.
6. scripts/local_checks.py run is intentionally disabled. The PreToolUse hook
   also blocks direct test/build/check commands and wrapper-based execution.

The plan is runtime state under .claude/.hook-state/ and must never be committed.
