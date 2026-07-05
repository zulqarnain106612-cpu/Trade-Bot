# Agent Handoff State
> Updated: 2026-07-05 18:23:25 | Read this before starting any work.

## Current Status
**Agent**:   claude
**Status**:  🟢 ACTIVE
**Task**:    shell session started — GAP-015 follow-on: provision GLASSNODE_API_KEY/CRYPTOQUANT_API_KEY (see DECISION_LOG.md), then build
**Started**: 2026-07-03 22:01:55
**Last checkpoint**: 2026-07-05 18:23:25

## ⚠ ANOTHER AGENT IS ACTIVE
If claude is no longer running, status is stale.
Check: `git log --oneline -3` — if no recent commits, agent likely crashed.
Safe to take over: run `python3 .project-intel/scripts/handoff.py start --agent YOUR_AGENT --task 'resume'`

## Next Step for Incoming Agent
  GAP-015 follow-on: provision GLASSNODE_API_KEY/CRYPTOQUANT_API_KEY (see DECISION_LOG.md), then build historical intellig

## Files to Check
  - .project-intel/ARCHITECTURE.md
  - .project-intel/HANDOFF.md
  - .project-intel/MODULE_MAP.json
  - .project-intel/SESSION_STATE.json
  - tests/test_signal_engine.py
  - src/diagnostics/signal_debugger.py
  - tests/test_performance_drift.py
  - .env
  - .project-intel/MODULE_MAP_SLIM.json
  - .project-intel/RAW_SCAN.json
  - src/api/main.py
  - src/api/metrics.py
  - requirements.txt
  - tests/test_metrics.py
  - src/engine/orchestrator.py
  - src/intelligence/risk_quantification.py
  - src/intelligence/probabilistic_adapter.py
  - src/engine/signal_engine.py
  - .project-intel/PROBABILISTIC_LAYER_CALIBRATION_AUDIT.md
  - src/data/storage.py
  - scripts/backfill_intelligence.py
  - src/features/pipeline.py
  - src/models/trainer.py
  - tests/test_gap015_backfill.py
  - src/intelligence/providers/base.py
  - src/intelligence/providers/okx_provider.py
  - src/intelligence/providers/blockchain_provider.py
  - src/intelligence/providers/coingecko_provider.py
  - src/intelligence/providers/aggregator.py
  - tests/test_intelligence_providers.py
  - src/intelligence/providers/binance_provider.py
  - .claude/CLAUDE.md
  - .project-intel/GAPS.md

## Session History (last 5)
  [2026-07-04 00:23:37] claude — interrupted: shell exited with 2 uncommitted file(s)
  [2026-07-04 00:23:37] claude — interrupted: shell exited with 2 uncommitted file(s)
  [2026-07-03 22:01:55] claude — interrupted: shell session started — GAP-015 follow-on: provision GLASSNODE_API_KEY/CRYPTOQUA
  [2026-07-03 21:58:54] claude — interrupted: shell exited with 3 uncommitted file(s)
  [2026-07-03 21:42:44] claude — interrupted: shell session started — GAP-015 follow-on: provision GLASSNODE_API_KEY/CRYPTOQUA

## AUTO-FIX DIRECTIVE (permanent, overrides all prior session decisions)

Any part of the project found to be **silent, broken, missing, open, less-coverage,
minimal, partial, experimental, blocked, or simple/incomplete** MUST be fixed to full
production quality without pausing or asking the user.

Autonomous execution rules:
- Fix all issues in priority order (P0 → P1 → P2 → P3).
- When one task completes, immediately proceed to the next without asking user to continue.
- The ONLY valid stopping point is: all open gaps/tasks are resolved AND the only remaining
  item is GLASSNODE_API_KEY provisioning (user action, outside agent scope).
- Never ship: demo logic, mock implementations, placeholder code, or fragile patches.
- Every "COMPLETE" claim requires: (1) unit tests pass, (2) `pytest --cov` shows nonzero
  coverage for the file, (3) grep of signal_engine.py/gates.py/orchestrator.py confirms
  the module is imported or explicitly marked EXPERIMENTAL with justification.
- After every fix: run affected tests, checkpoint, commit, move to next task.

## Quick Start for Any Agent
```
# 1. Read context (mandatory):
cat .project-intel/CONTEXT_PRIMER.md
cat .project-intel/HANDOFF.md          ← you are here
cat .project-intel/SESSION_STATE.json

# 2. Check uncommitted work:
git status --short
git diff --stat

# 3. Register yourself:
python3 .project-intel/scripts/handoff.py start --agent YOUR_AGENT --task 'describe task'

# 4. Checkpoint as you work (every meaningful step):
python3 .project-intel/scripts/handoff.py checkpoint --agent YOUR_AGENT \
  --completed 'what you just did' --next 'exact next action' --files 'src/x.py'
```