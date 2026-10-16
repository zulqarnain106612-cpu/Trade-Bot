---
name: trade-bot-delivery
description: Use when preparing a Trade-Bot branch, commit, pull request, release, or completion report; preserves work and requires evidence-based delivery.
---

# Trade-Bot delivery and verification

- Start from a freshly verified origin/main and a dedicated task branch/worktree. Never reuse a dirty candidate branch without auditing every change.
- Inventory staged, unstaged, untracked, ignored, and unpushed work. Do not discard or silently omit user-owned changes.
- Keep a PR limited to one coherent requested objective. Update applicable docs, quality registry, traceability, and tests when repository policy requires it.
- Validate JSON/TOML/YAML syntax and schemas using installed authoritative tools when available. Never claim a configuration was accepted by Codex if only syntax was checked.
- Add focused regression tests and required quality coverage, but never execute tests, builds, lint/format/type/security checks, or other quality gates locally. GitHub Actions is the sole execution path for those checks; after pushing an authorized PR update, use only `./scripts/prfail --pr <PR_NUMBER>` for the finalized report bound to current HEAD SHA.
- Do not read workflow logs, job records, artifacts, check results, or arbitrary PR comments, and do not start a live CI watcher. If the allowed notice is insufficient, document the evidence gap instead of widening access.
- Before commit, review the final diff and every staged path using approved bounded inspection. Include only task-related files.
- Push only when authorized. Never force-push. Never merge or enable auto-merge unless separately authorized.
- Final report: branch and base SHA, commit SHA, push/PR URL, exact verification results, CI state, outstanding gaps, and manual account/workspace actions.

For a local-only fix, inspect the source at the report SHA and apply a reviewed unified diff with `python3 scripts/apply_ci_patch.py --pr <PR_NUMBER> --head-sha <REPORT_HEAD_SHA> --patch-stdin`; the command refuses a mismatched HEAD and changes only the working tree, without staging, committing, or pushing. For an active PR correction, use the approved GitHub branch-write route against the report SHA and exact file SHA so the correction reaches the PR branch and GitHub Actions validates it; a local-only patch is not a completed PR repair.
