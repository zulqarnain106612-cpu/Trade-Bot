---
name: trade-bot-evidence-first
description: Use for Trade-Bot engineering, audits, debugging, tests, and delivery. Verify repository truth, preserve existing work, and follow the project's enforced quality and safety gates.
---

# Trade-Bot evidence-first workflow

- Read repository root AGENTS.md and CLAUDE.md plus applicable .claude/ and config/ policies before changing code.
- Inspect live branch/HEAD, upstream, worktree including untracked files, relevant history, dependency lockfiles, tests, quality registry, and CI evidence before deciding.
- Reproduce the behavior and trace the real execution path. Distinguish facts, inferences, and unknowns. Do not trust prior agent claims without evidence.
- Preserve all existing work. Use a dedicated branch/worktree; never destructive-reset, delete, force-push, merge, or bypass policies.
- Add a regression test at the actual failure layer. Use deterministic PAPER/offline fixtures for trading/runtime tests; do not send real orders or expose credentials.
- Run focused tests and repository-required gates; never weaken coverage, architecture, security, risk, authentication, or readiness rules. Report unrun checks honestly.
- Keep tool output compact and targeted. Follow config/command_policy.json and the observation boundary; do not stream CI logs.
- Commit and push only scoped reviewed changes when authorized; never merge or enable auto-merge without separate authorization.
