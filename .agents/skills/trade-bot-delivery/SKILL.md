---
name: trade-bot-delivery
description: Use when preparing a Trade-Bot branch, commit, pull request, release, or completion report; preserves work and requires evidence-based delivery.
---

# Trade-Bot delivery and verification

- Start from a freshly verified origin/main and a dedicated task branch/worktree. Never reuse a dirty candidate branch without auditing every change.
- Inventory staged, unstaged, untracked, ignored, and unpushed work. Do not discard or silently omit user-owned changes.
- Keep a PR limited to one coherent requested objective. Update applicable docs, quality registry, traceability, and tests when repository policy requires it.
- Validate JSON/TOML/YAML syntax and schemas using installed authoritative tools when available. Never claim a configuration was accepted by Codex if only syntax was checked.
- Run focused tests and the repository's prescribed quality gates. Use the existing CI workflow when a required gate cannot be run locally; do not claim success until evidence is available.
- Before commit, review git diff --check, git diff --stat, and every staged path. Include only task-related files.
- Push only when authorized. Never force-push. Never merge or enable auto-merge unless separately authorized.
- Final report: branch and base SHA, commit SHA, push/PR URL, exact verification results, CI state, outstanding gaps, and manual account/workspace actions.
