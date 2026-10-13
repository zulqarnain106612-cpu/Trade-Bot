# Trade-Bot — Codex repository instructions

## Mandatory repository policy

Before implementation, review CLAUDE.md and relevant repository-owned policies under .claude/, config/, and docs/. CLAUDE.md contains the project-wide engineering contract; these instructions supplement it and never weaken it. If a policy, hook, schema, quality registry, or CI gate applies, follow it.

## Evidence-first workflow

1. Inspect the live checkout before acting: branch, HEAD, upstream, staged/unstaged/untracked files, active Git operations, relevant history, dependencies, tests, policies, and CI evidence available through approved summary channels.
2. Establish current behavior, expected behavior, the evidence-backed gap, and root cause before implementation. Label facts, inferences, and unknowns distinctly. Do not treat prior-agent claims as proof.
3. Preserve all pre-existing user changes and untracked files. Never reset, clean, stash, overwrite, delete, force-push, rebase, cherry-pick, merge, or change branch state without explicit authorization. Do not bundle unrelated work.
4. Make the complete scoped fix and add focused regression tests that express the intended behavior. Trade-Bot's policy prohibits executing tests, builds, lint/format/type/security checks, and quality gates locally; GitHub Actions is the sole execution path for them.
5. Follow config/command_policy.json, observation-boundary rules, required hooks, architecture/security/math constraints, config/quality_registry.json, and coverage/per-file floors. Never bypass or weaken gates to obtain a green result.
6. Inspect the final diff and staged paths through bounded read-only review. After an authorized push, the only permitted CI diagnostic command is `python3 scripts/local_checks.py prepare`, which returns only the current HEAD's SHA-bound PR notice. Do not fetch workflow logs, job records, artifacts, annotations, check results, or run a watcher. Verify actual HEAD/worktree/remote state and report missing evidence honestly; never claim tests or CI passed without a permitted result.

## Knowledge-pack routing

Use `codebase-analysis-docs/CODEBASE_KNOWLEDGE.md` as the architecture entry point, not as a substitute for source inspection. Load only the relevant reference for the task:

- File discovery: `FILE_INDEX.md`; bounded declaration lookup: `CHUNK_INDEX.md`. Verify the live path, symbol, hash, and line range before relying on an anchor.
- FastAPI route changes: `API_REFERENCE.md`, then inspect the actual router, dependencies, authorization, and tests.
- Runtime, trading flow, persistence, schema, terminal, or frontend changes: the matching section of `CODEBASE_KNOWLEDGE.md`, the relevant diagram under `assets/`, and the owning source/tests.
- Historical findings and handoffs: `STATE_BLOCKS.md` and `PHASE_2_SOURCE_AUDIT.md`; treat dated findings and explicitly unverified claims as historical until rechecked against current source.
- Before delivery, follow the documented knowledge-index refresh procedure and CI gate; never silently rewrite manually maintained semantic anchors or overwrite user-authored notes.

The knowledge pack is navigation and investigation evidence, not proof of runtime correctness, security, CI success, or production readiness. If it conflicts with current source or policy, current source and authoritative policy win; record material drift and update the owning document in the same scoped change. Do not require reading the full pack for unrelated or trivial edits.

## Trade-Bot safety invariants

- Trading and exchange tests use deterministic fixtures, PAPER mode, and no live orders or real credentials unless the user explicitly authorizes a separate, safe operation.
- Never weaken risk gates, authentication, approval, operator authorization, memory limits, or readiness requirements to make a test pass.
- Preserve the existing FastAPI/API entry point and frontend architecture; do not create a duplicate runtime server without evidence that it is required.
- Treat readiness as measured operational state, not a hard-coded healthy constant. Keep liveness, API readiness, runtime initialization/reconciliation, model readiness, and first-tick evidence distinct.
- Keep secrets out of source, logs, prompts, commits, and test output. Use ignored local environment files and documented secret stores.

## Agent skills and parallel work

Use the relevant repository-local skill in `.agents/skills/` for implementation, root-cause debugging, code review/PR handling, OpenAI SDK integration, evaluation/optimization, and parallel agent/sub-agent work. Parallelize only independent tasks with explicit path ownership and evidence requirements. Keep concurrent writes in isolated worktrees; the coordinating agent owns integration, final review, and one coherent PR. Skills never override the repository safety, permission, or validation policies.

## Efficient context and output

Prefer targeted searches and narrow file ranges. For command/test failures, return the shortest actionable summary with the failing assertion/error and relevant file/line; do not dump full logs or whole source files into agent context. Never run live CI watchers or retrieve workflow logs, job records, artifacts, annotations, or check results. Use only the PR's designated compact CI failure-notice channel.

## Delivery

Use a dedicated branch based on the verified current origin/main; preserve unrelated worktrees. Commit and push only the task-scoped reviewed files when explicitly authorized. Do not merge or enable auto-merge unless separately instructed. Final reports must include exact commit/branch/PR, files changed, commands and actual results, CI state, remaining gaps, and any actions requiring account/workspace UI access.
