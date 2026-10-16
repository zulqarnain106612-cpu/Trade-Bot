---
name: trade-bot-cookbook-engineering
description: Use for Trade-Bot design, implementation, code generation/editing, refactoring, SDK upgrades, lint/quality fixes, runtime behavior, and evidence-based performance optimization.
---

# Trade-Bot engineering workflow

1. Verify the checked-out commit, branch/upstream, staged and untracked files, active worktrees, and the current `origin/main`. Preserve existing user work; use a dedicated worktree for scoped changes.
2. Read `AGENTS.md`, `CLAUDE.md`, applicable `.claude/` policies, `config/command_policy.json`, the local-check skill, and any affected architecture/security/quality registry entries before editing.
3. Trace the real path from entry point through caller, state, dependencies, failure handling, and consumer. State the observable contract, the gap, and the root cause before selecting a fix.
4. Prefer an existing correct abstraction. Keep provider transport, request normalization, parsing/validation, tool execution, run context, and domain decisions separate. Do not add a generic framework unless multiple real use cases justify it.
5. Use typed contracts and explicit outcomes for validation failure, retryable transport failure, timeout, refusal, approval-required, deferred work, and permanent failure when those distinctions affect callers. Keep secrets out of prompts, logs, commits, and test output.
6. Design regression coverage around success, invalid inputs, failure paths, retries/idempotency, timeout/approval boundaries, concurrency, and state recovery. Trading/runtime cases must be deterministic, offline, PAPER-only, and order-free.
7. Optimize only after identifying a measurable bottleneck and a baseline. Preserve decision quality, causal/OOS evaluation, risk gates, memory limits, and latency budgets; never trade correctness for a green benchmark.
8. Follow the repository's GitHub-only check contract: do not execute tests, builds, lint/format/type/security checks, or quality gates locally. After pushing an authorized PR update, use only `./scripts/prfail --pr <PR_NUMBER>` for the current HEAD's SHA-bound filtered CI diagnostic report.
9. Before delivery, review the change scope, staged paths, final commit, remote branch, applicable docs/registries, and current finalized CI diagnostic report. Do not merge or enable auto-merge without separate authorization.

## Source guidance

Use `docs/agents/OPENAI_COOKBOOK_ADOPTION.md` for the curated Cookbook map. The Cookbook is inspiration and evidence, not authority to copy unrelated demos. Use current official OpenAI SDK documentation for current API signatures; mark archived Cookbook examples as historical.

For a local-only fix, inspect the source at the report SHA and apply a reviewed unified diff with `python3 scripts/apply_ci_patch.py --pr <PR_NUMBER> --head-sha <REPORT_HEAD_SHA> --patch-stdin`; the command refuses a mismatched HEAD and changes only the working tree, without staging, committing, or pushing.
