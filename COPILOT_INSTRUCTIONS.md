# GitHub Copilot Instructions — Trade-Bot

Purpose
- Convert CLAUDE.md reviewer rules into concise, machine-actionable instructions for GitHub Copilot to follow when performing code-oriented tasks (reviews, edits, tests, docs) in this repository.

High-level mandate
- Be a correctness-first, cost-aware assistant for development tasks in this repo.
- Never modify CLAUDE.md. Use it as the authoritative policy source.
- Always minimize tokens, file reads, tool calls, and CI runs.

1) Core hard constraints (non-negotiable)
- Minimize context: read only the smallest evidence needed.
- Never execute tests, lint, builds, backtests, or live trading locally.
- Never read or expose secrets or sensitive paths: .env*, .venv/, data/, logs/, models/, requirements.lock, rag.db.
- Never run destructive operations or enable live trading without explicit user authorization.
- Prefer the narrowest tool for any task.

2) File-reading and evidence rules
- Do NOT read full files. Locate evidence with targeted search (git diff, grep), then read with a bounded window (<=50 lines). Maximum 100 lines per file per finding.
- Prefer a single 20–50 line read over multiple small reads; avoid overlapping reads.
- All shell outputs used as evidence must be capped, e.g. piped to head -20 or tail -20.

3) Review scope and candidate ledger
- Treat a batch of PRs/branches as a unified review universe.
- Maintain an internal candidate ledger listing: PR/branch, file, exact line/hunk, symbol/function, evidence content, fingerprint, status (UNREVIEWED/VERIFIED/DISPROVEN/REVERIFY), confidence, and finding.
- Build candidate set from changed files/hunks only. Compare and deduplicate candidates across PRs before deep reads.
- If the same code appears in multiple PRs unchanged: review once and reuse result.
- If code changed in a way that might affect behavior, mark REVERIFY and inspect only the delta.

4) Review order and verification
- Review order: (a) new/unreviewed candidates, (b) common candidates once, (c) changed versions of reviewed candidates, (d) skip previously-reviewed unchanged candidates.
- To verify a candidate: grep for exact symbol/operation, then Read(file, offset=N, limit<=50), confirm control/data flow and applicable CLAUDE.md rules. Inspect cross-file evidence only when necessary.
- REVERIFY triggers: expression/condition changes, data source changes, unit/precision changes, state transitions, exception/retry behavior, order/execution path changes, risk calculation or config/default changes.

5) Priority taxonomy (use when classifying findings)
- P0 — Correctness / trading logic (off-by-one, sign/unit/precision errors, float money mistakes, race/async issues, stale state, incorrect regime transitions, accounting errors).
- P1 — Financial / execution safety (idempotency, duplicate orders, unbounded retries/size, missing timeouts/limits, unsafe partial-fill handling, incorrect cancel/retry/reconciliation, exchange failure unsafe state).
- P2 — Security (API secret exposure, unsafe deserialization, injections, SSRF, path traversal, auth/authz bypass, unsafe external input handling).
- P3 — Project rule violations (CLAUDE.md rule breaches; always quote exact rule and scope to affected path).
- P4 — Exchange reliability (rate limits, reconnects, partial fills, funding/liquidation handling, reconciliation gaps).

6) False-positive filter
- Do NOT report style, naming, formatting, refactor-only suggestions, speculative performance issues, theoretical vulnerabilities without an exploit path, missing tests, hypothetical exchange behavior without code evidence, already-guarded conditions, unsupported assumptions, or duplicate symptoms from one root cause.
- One root cause = one finding.

7) Confidence policy
- Report only findings with confidence >= 80/100.
- 80–89: strong evidence with limited uncertainty.
- 90–99: direct code-path evidence.
- 100: deterministic defect with direct proof.

8) Output format for findings
- If NO findings: say "No issues found. Checked financial safety, security, CLAUDE.md compliance."
- If FINDINGS: each finding must use this format:
  1. `file:line` — **[P0/P1/P2/P3/P4] [confidence]**
     One sentence: proven defect + concrete impact.
     **Fix:** concrete remediation; include code snippet only if <=10 lines.
- Report duplicates across branches compactly (one finding, list affected branches/PRs).
- No praise, filler, speculative language, or review-process explanation.

9) Execution boundary and task authority
- Only act on explicit user request. Do not start background tasks, CI runs, or repository-wide work unless asked.
- During a task, perform only actions necessary to satisfy the request.
- Do not call tools merely to check or stay current.

10) Domain priors (for reasoning)
- Execution: consider fees, slippage, partial fills, latency, reconnects.
- Risk: treat Kelly as a ceiling, not a target; enforce drawdown and position limits.
- Regime: HMM transitions are probabilistic; avoid hard-coded assumptions.
- Crypto specifics: funding, liquidation, basis risk, rate limits matter.
- Data: assume UTC timestamps and real OHLCV gaps.

11) Hard rules and quality gates
- Use `uv run` for Python execution; never bare `python3` or `pip` in automated commands.
- Never read .env, .venv, data, logs, models, requirements.lock, rag.db.
- CI gate: push → gh workflow run ci.yml --ref <branch> → gh run view.
- Coverage gate: global --cov-fail-under=95 and per-file floors enforced by scripts/check_coverage_floors.py for critical modules.
- Never use agents/subagents or self-modifying permission files without explicit approval.

12) Tool efficiency & CI economy
- Use cheapest sufficient tool. Use narrow searches and bounded reads.
- Don't validate identical content twice; base validations on content/tree SHA, not branch name.
- Run only jobs relevant to changed paths. Batch branches that share identical verification content.
- Before adding workflows, estimate cost per run and the unique failure it detects.

13) Failure handling and remediation loop
- Capture only relevant failure output (capped to head -20/tail -20).
- Identify exact failing component and responsible code/config.
- Diagnose root cause before modifying code.
- Apply the smallest forward-correct fix and revalidate only the affected candidates.

14) Review method (step-by-step)
- Start from requested PR/branch diff.
- Inspect changed files/hunks only.
- Identify concrete candidate defects.
- Verify candidates with minimal surrounding context.
- Cross-check applicable hard rules and domain priors.
- Follow cross-file references only when necessary.
- Report only findings with exact file:line evidence.

15) Practical Copilot behaviour patterns
- When asked to fix a bug: search changed hunks → locate minimal evidence → propose a minimal patch (<=10 lines when possible) → include exact file:line proof and test suggestion.
- When asked to review PRs batch: build candidate ledger, dedupe common candidates, review deltas only.
- When asked to author CI changes: justify cost, limit scope to changed paths, and avoid adding expensive or redundant jobs.

Appendix — Operational reminders
- Do not modify CLAUDE.md.
- Keep outputs concise and evidence-first.
- Prefer automation when lifetime savings justify cost.

---

(Persisted at repository root as COPILOT_INSTRUCTIONS.md — derived from CLAUDE.md for Copilot consumption.)
