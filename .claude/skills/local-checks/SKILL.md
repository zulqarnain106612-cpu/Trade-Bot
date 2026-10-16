---
name: local-checks
<<<<<<< HEAD
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
=======
description: Enforce GitHub-only validation and retrieve the finalized filtered CI diagnostics report.
---

# GitHub-only check and diagnostics contract

GitHub Actions is the sole execution path for tests, builds, lint, security scans,
workflow validation, registry/schema checks, generated-document checks, and quality
gates. Never execute an individual test, test suite, build, or check locally.

1. Use the exact command `./scripts/prfail --pr <PR_NUMBER>` to retrieve the
   finalized diagnostics report for the requested open PR's current head SHA.
2. The collector runs autonomously on GitHub-hosted runners after source workflows
   complete. It waits for all applicable workflows/jobs and never cancels siblings.
3. A missing, stale, incomplete, malformed, or inaccessible report is an operational
   error, never evidence that CI passed. Do not read raw workflow logs, jobs,
   annotations, artifacts, check records, or arbitrary PR comments.
4. Inspect the report's exact path/line/column when present and the surrounding
   source at the same SHA. Do not invent locations when the diagnostic does not
   provide them.
5. Apply a reviewed unified diff only through
   `python3 scripts/apply_ci_patch.py --pr <PR_NUMBER> --head-sha <REPORT_HEAD_SHA> --patch-stdin`
   with a quoted `TB_CI_PATCH` heredoc. The patch applier revalidates the finalized
   report, PR number, head SHA, and local HEAD before applying an in-place text edit.
   It never stages, commits, pushes, changes branches, or runs checks. For an active PR repair, submit the reviewed correction through the approved GitHub branch-write route against the report SHA and exact file SHA; a local-only patch is not a completed PR repair.
6. GitHub Actions validates every correction. If the PR head changes, retrieve the
   new report before preparing another patch.

The old `scripts/local_checks.py prepare` comment-notice route is retired. The
`local_checks.py run` route remains disabled. Runtime state under
`.claude/.hook-state/` must never be committed.
>>>>>>> origin/main
