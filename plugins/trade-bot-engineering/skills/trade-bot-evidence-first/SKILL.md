---
name: trade-bot-evidence-first
description: Use before modifying Trade-Bot code, configuration, tests, CI, runtime, trading, or agent policy; verifies live repository state and repository gates before changes.
---

# Trade-Bot evidence-first engineering

1. Read root AGENTS.md, then read CLAUDE.md and relevant .claude/ hooks/policies before implementation.
2. Establish branch, HEAD, origin/main, dirty state (including untracked files), active operations, and relevant existing work. Preserve unrelated work; use a separate worktree when the primary checkout is dirty.
3. Trace actual callers, configuration, dependencies/lockfiles, state/persistence, error paths, tests, architecture, quality registry, and CI notice evidence. Separate verified facts from inference and unknowns.
4. Reproduce the defect before fixing it where feasible. Do not assume the stated root cause. Prefer the smallest architecturally correct fix, not merely the fewest lines.
5. Add a regression test at the layer where the defect occurs. Do not label JSDOM as native browser E2E or imports as a complete startup preflight.
6. Keep trading tests offline, deterministic, PAPER-only, and order-free. Never bypass authentication, risk checks, policy hooks, coverage, architecture gates, or memory/resource limits.
7. Add focused regression tests, but do not execute tests, builds, lint/format/type/security checks, or quality gates locally: the repository mandates GitHub Actions as the sole execution path. After pushing an authorized PR update, use only `python3 scripts/local_checks.py prepare` to read the single CI notice whose hidden SHA marker matches current HEAD. Report exact CI evidence and any checks not yet run.
8. Keep context bounded: use targeted reads and the approved compact CI failure-notice route. Do not read workflow logs, job records, artifacts, arbitrary PR comments, or run a live CI watcher.
9. Do not perform destructive Git operations, push, merge, or enable auto-merge without explicit user authorization for that operation.
