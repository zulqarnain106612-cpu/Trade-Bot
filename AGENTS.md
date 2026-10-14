# Trade-Bot — Codex repository instructions

## Mandatory repository policy

Before implementation, review CLAUDE.md and relevant repository-owned policies under .claude/, config/, and docs/. CLAUDE.md contains the project-wide engineering contract; these instructions supplement it and never weaken it. If a policy, hook, schema, quality registry, or CI gate applies, follow it.

## Evidence-first workflow

1. Inspect the live checkout before acting: branch, HEAD, upstream, staged/unstaged/untracked files, active Git operations, relevant history, dependencies, tests, policies, and CI evidence available through approved summary channels.
2. Establish current behavior, expected behavior, the evidence-backed gap, and root cause before implementation. Label facts, inferences, and unknowns distinctly. Do not treat prior-agent claims as proof.
3. Preserve all pre-existing user changes and untracked files. Never reset, clean, stash, overwrite, delete, force-push, rebase, cherry-pick, merge, or change branch state without explicit authorization. Do not bundle unrelated work.
4. Make the complete scoped fix and add regression tests for confirmed defects. Repository policy prohibits local tests, builds, lint/format/type/security checks, and quality gates; GitHub Actions is the sole execution path for those checks. The only permitted local-check operation is the exact read-only `python3 scripts/local_checks.py prepare` command, and only when its documented preconditions are met.
5. Follow `config/command_policy.json`, observation-boundary rules, required hooks, architecture/security/math constraints, `config/quality_registry.json`, and coverage/per-file floors. Never bypass or weaken gates to obtain a green result.
6. Before completion, inspect the scoped diff and generated-file changes without running prohibited checks, use only the sanctioned SHA-bound CI notice route, and verify actual HEAD/worktree/remote state. Report blocked/unrun checks honestly; do not claim tests, CI, runtime readiness, or a push succeeded without authoritative evidence.

## Task-specific knowledge routing

Use `codebase-analysis-docs/CODEBASE_KNOWLEDGE.md` as an architectural entry point when present; it is a snapshot-bound index, not authority over current source or repository policy. Load only relevant sections and verify claims against current implementation and tests. Use `FILE_INDEX.md` to locate files, `CHUNK_INDEX.md` only when its whole-file hash matches current source, `API_REFERENCE.md` for route discovery (then trace effective dependencies and authorization), and `STATE_BLOCKS.md` for historical handoff context. Treat indexed paths, line ranges, hashes, route counts, and diagrams as potentially stale after source changes. CI enforces `python3 scripts/codebase_knowledge_index.py --check`; the narrowly scoped `--refresh-file-index` operation updates only file-index metadata. Never auto-relabel stale semantic chunk ranges: re-inspect the source and update those anchors deliberately. Preserve pre-existing untracked analysis artifacts; do not overwrite or regenerate them without explicit scope authorization. For multi-file or high-risk work, maintain a bounded execution checklist with acceptance criteria and evidence; trivial changes do not need a separate plan.

## Trade-Bot safety invariants

- Trading and exchange tests use deterministic fixtures, PAPER mode, and no live orders or real credentials unless the user explicitly authorizes a separate, safe operation.
- Never weaken risk gates, authentication, approval, operator authorization, memory limits, or readiness requirements to make a test pass.
- Preserve the existing FastAPI/API entry point and frontend architecture; do not create a duplicate runtime server without evidence that it is required.
- Treat readiness as measured operational state, not a hard-coded healthy constant. Keep liveness, API readiness, runtime initialization/reconciliation, model readiness, and first-tick evidence distinct.
- Keep secrets out of source, logs, prompts, commits, and test output. Use ignored local environment files and documented secret stores.

## Efficient context and output

Prefer targeted searches and narrow file ranges. For command/test failures, return the shortest actionable summary with the failing assertion/error and relevant file/line; do not dump full logs or whole source files into agent context. Never run live CI watchers or retrieve bulk CI logs when repository policy prohibits it. Use the PR's designated compact CI failure-notice channel where required.

## Delivery

Use a dedicated branch based on the verified current origin/main; preserve unrelated worktrees. Commit and push only the task-scoped reviewed files when explicitly authorized. Do not merge or enable auto-merge unless separately instructed. Final reports must include exact commit/branch/PR, files changed, commands and actual results, CI state, remaining gaps, and any actions requiring account/workspace UI access.
