---
name: trade-bot-code-review-pr
description: Use when reviewing Trade-Bot changes, analyzing code issues, designing scoped fixes, reviewing pull requests, resolving review feedback, or preparing PR delivery.
---

# Code review and pull-request workflow

1. Verify the live base/head SHAs, PR state, branch/worktree, and changed-file list. Inspect the actual diff, not just the PR title or previous summaries.
2. Review in risk order: behavior/correctness, safety and authorization, state transitions/idempotency, error handling/recovery, concurrency, data/privacy/secrets, compatibility/migrations, performance/resource limits, test quality, then style/readability.
3. Trace each suspected issue to a concrete path and observable failure. Distinguish a confirmed defect from a design concern or an unverified hypothesis. Prefer minimal fixes and explicit regression tests over broad rewrites.
4. Review tests for meaningful assertions, negative/error cases, deterministic fixtures, and isolation from network, billing, credentials, and live trading. A test that merely exercises code without asserting the contract is insufficient.
5. Check formatting, lint, type, architecture, dependency, security, and coverage policy by reading repository configuration and CI evidence; do not execute those checks locally. For Trade-Bot, only `./scripts/prfail --pr <PR_NUMBER>` may retrieve the current HEAD's SHA-bound filtered CI diagnostic report after a push.
6. Treat the finalized ./scripts/prfail --pr <PR_NUMBER> report as scoped evidence. Do not fetch workflow logs, job records, artifacts, annotations, check results, or arbitrary PR comments. A missing report is an operational error, not proof that checks passed.
7. Address only comments that apply to the current diff and have enough context to act safely. If a review request conflicts with risk controls or repository policy, explain the conflict rather than implementing it blindly.
8. Keep each commit cohesive, explain the reason in the commit message, and review staged paths before commit. Preserve unrelated work and use isolated worktrees for concurrent tasks. Push only to the explicitly authorized branch; do not merge or enable auto-merge unless separately instructed.
9. Report findings first by severity with file/path evidence, then the changes made, exact commit and PR, validation actually observed, and unresolved gaps. Never call a PR ready to merge when required checks are failing, missing, cancelled, or still running.

## Sources

- [OpenAI Cookbook: iterative Codex development workflows](https://github.com/openai/openai-cookbook/blob/5bd6cb40b6b84b69b2032d810610aa85064102df/examples/codex/iterating-development-workflows-with-codex.md)
- [OpenAI Skills repository](https://github.com/openai/skills) for official skill workflow conventions. The repository's stricter CI-observation and local-check policies take precedence over generic upstream instructions.

For a local-only fix, inspect the source at the report SHA and apply a reviewed unified diff with `python3 scripts/apply_ci_patch.py --pr <PR_NUMBER> --head-sha <REPORT_HEAD_SHA> --patch-stdin`; the command refuses a mismatched HEAD and changes only the working tree, without staging, committing, or pushing.
