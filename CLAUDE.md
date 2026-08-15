# Trade-Bot — Claude Code

# Trade-Bot — Claude Code

## EXECUTION BOUNDARY

The current user request is the sole task authority.

- Before a user request: do nothing.
- Never perform startup, background, monitoring, housekeeping, orientation, CI,
  review, or repository work without a current user request.
- During a task, execute only actions necessary to fulfill that request.
- Do not explore unrelated files, branches, services, or history.
- Reuse already obtained evidence; never reread unchanged content.
- Prefer the smallest tool operation that provides the required evidence.
- Stop tool execution immediately when the requested result is complete.
- Do not anticipate, continue, monitor, or start another task after completion.
- Do not call tools merely to "check", "verify", "clean up", or "stay current"
  unless the current request requires it.

Stack: Python 3.11 | uv | FastAPI | XGBoost+HMM | ccxt (Binance/OKX) | pytest | ruff+mypy

Entry: `src/api/main.py`
Engine: `src/engine/orchestrator.py`
Risk: `src/risk/kelly.py`
Regime: `src/regime/detector.py`

## DECISION AUTHORITY

**Proceed without asking:** implementation, refactoring, tests requested by the
user, non-breaking dependency changes, documentation, lint/type fixes, commits,
and pushes when required by the user's request.

**Do not perform unless explicitly requested:** live trading, destructive
operations, force-push, `.env*` access/modification, execution-mode changes,
deletion of `src/execution/*` or `src/risk/*`, modification of
`.claude/settings*.json`, `.claude/hooks/**`, or `.github/workflows/**`.

## DOMAIN PRIORS

- Execution: fees, slippage, partial fills, latency, reconnects.
- Risk: Kelly is a ceiling, not a target; enforce drawdown and position limits.
- Regime: HMM transitions are probabilistic; avoid hard-coded regime assumptions.
- Crypto: funding, liquidation, basis risk, exchange solvency, rate limits.
- Data: UTC timestamps; OHLCV gaps are real.
- Validate signals out-of-sample; in-sample metrics alone are insufficient.

## HARD RULES

- Use `uv run` for Python execution; never bare `python3` or `pip`.
- Never read: `.env`, `.venv/`, `data/`, `logs/`, `models/`,
  `requirements.lock`, `rag.db`.
- Never run tests, lint, type-check, or build locally. CI only.
- CI validation:
  `push → gh workflow run ci.yml --ref <branch> → gh run view`
- Coverage gate: 95% global (`--cov-fail-under=95`).
- Per-file coverage floors are enforced in CI by
  `scripts/check_coverage_floors.py` for `src/execution/`, `src/engine/`,
  `runtime_monitor`.
- Never use Agent/sub-agents.
- No destructive operation without explicit user authorization.
- Never enable live trading or alter live execution mode.

## QUALITY, EXECUTION & CONTEXT DISCIPLINE

### Final Quality Gate
- Before delivery, perform a final correctness review of the completed work.
- If a defect is found, fix it and revalidate; repeat until no known defect remains.
- Do not knowingly deliver an incomplete, broken, speculative, or unvalidated result.
- Final response must be correctness-focused, evidence-backed, and aligned with the
  user's requested outcome.
- Validate the implementation, not merely the absence of an obvious error.

### Forward-Only Engineering
Before every consequential action, determine its expected effect:
- Does it move the requested task toward completion?
- Could it damage, regress, weaken, or unnecessarily alter working behavior?
- Is there a safer forward path?

Proceed when the action is justified and forward-progressing.
Never downgrade working functionality to hide or bypass a failure.
Prefer enhancement, correction, upgrade, optimization, or compatibility-preserving
fixes over regression-inducing workarounds.

Never use a knowingly broken, fake, demo-only, placeholder, or incomplete approach
when a production-correct approach is required.

### Root-Cause Execution
- Diagnose the exact root cause before modifying code.
- Obtain only the minimum relevant evidence/log lines needed to establish it.
- Search for the exact affected symbol/code path.
- Edit the smallest relevant code region directly.
- Do not wander through unrelated files or repeatedly inspect the repository.
- After a fix, inspect only changed/relevant candidates required for validation.

### Context Preservation
- Treat already-loaded information as authoritative until its source changes.
- Never reload, re-inject, or repeat data already available in context.
- If a file/code block is already loaded and only specific lines changed, inspect
  only those changed/relevant lines; never reload the complete file unnecessarily.
- After any tool call, retain and reuse its relevant result.
- Never request duplicate information from another tool.
- Never re-read unchanged code merely for confirmation.
- For every tool call, obtain only the data required for the current decision/output.

### Tool Efficiency
When multiple tools can accomplish the same operation:
- Use the cheapest sufficient tool.
- Request the smallest sufficient input.
- Produce the smallest sufficient output.
- Prefer narrow searches, targeted reads, and bounded command output.
- Never use a broader tool or retrieve more data when a narrower operation is sufficient.
- Tool calls must directly serve the current user request.

### Decision Discipline
Before consequential actions, continuously evaluate:
- Current objective
- Evidence already obtained
- Exact next action
- Expected forward impact
- Possible regression/failure
- Whether the same information is already available

Do not take an action merely because it is available.
Take the smallest justified action that advances the task.

### Changed-Candidate Principle
For every development cycle:
- Prioritize changed candidates regardless of development stage or issue type.
- Do not repeatedly reload complete files after localized changes.
- Revalidate changed behavior and directly affected dependencies only.
- Expand scope only when evidence proves it is necessary.

### Failure Handling
When something fails:
1. Capture only the relevant failure output.
2. Identify the exact failing component/path.
3. Locate the exact responsible code/configuration.
4. Determine the root cause.
5. Apply the smallest forward-correct fix.
6. Revalidate the affected behavior.
7. Repeat only if a new failure is revealed.

Never compensate for uncertainty by indiscriminately reading, searching, rerunning,
or modifying unrelated code.

## CI ECONOMY

CI minutes are a finite budget.

- Never validate identical content twice.
- Validation identity is content/tree SHA, not branch name.
- Read existing CI results before dispatching another run.
- Never dispatch speculatively.
- Never re-dispatch merely for fresher logs.
- Never cancel a useful in-flight run to start an equivalent one.
- Shared dependency/model/fixture setup must be cached by relevant lockfile
  content, not branch name.
- Run only jobs relevant to changed paths.
- Shared base code is reviewed/validated once; review branch deltas separately.
- Batch branches sharing identical verification content.
- Mine every CI failure before spending another CI run.
- Fix all reported failures in one pass where possible.
- Before adding a workflow/job, identify:
  1. its cost per run;
  2. the unique failure it detects.
  If either is unknown, do not add it.

## REVIEW DEDUPLICATION

When reviewing multiple PRs/branches, treat them as one review set.

- Review identical candidates/common unchanged code once.
- Reuse verified evidence across branches containing the same unchanged code.
- Review only the relevant delta when the candidate changed.
- Skip previously reviewed candidates only when the underlying evidence is
  demonstrably unchanged.
- Reverify when behavior-affecting code, configuration, data flow, state,
  precision, error handling, execution, or risk logic changed.
- Different root causes are separate findings even when they affect the same file.
- Report one finding per root cause, not one finding per branch.
- Never assume similarity means equivalence.

## REVIEW METHOD

For review tasks:

1. Start from the requested PR/branch diff.
2. Inspect changed files/hunks only.
3. Identify concrete candidate defects.
4. Verify candidates with the minimum surrounding context required.
5. Cross-check applicable Hard Rules and Domain Priors.
6. Follow cross-file references only when necessary to prove the finding.
7. Never report speculative, stylistic, or unverified issues.
8. Report only findings with exact `file:line` evidence.
