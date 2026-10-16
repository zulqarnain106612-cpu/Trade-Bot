---
name: local-checks
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
