# Agent Handoff State
> Updated: 2026-07-11 00:18:56 | Read this before starting any work.

## Current Status
**Agent**:   claude
**Status**:  🟢 ACTIVE
**Task**:    shell session started — GAP-006 storage migration decision (TimescaleDB vs QuestDB) or Debt-003 Python 3.14/3.11 venv mismat
**Started**: 2026-07-10 23:38:20
**Last checkpoint**: 2026-07-11 00:18:56

## ⚠ ANOTHER AGENT IS ACTIVE
If claude is no longer running, status is stale.
Check: `git log --oneline -3` — if no recent commits, agent likely crashed.
Safe to take over: run `python3 .project-intel/scripts/handoff.py start --agent YOUR_AGENT --task 'resume'`

## Next Step for Incoming Agent
  GAP-006 storage migration decision (TimescaleDB vs QuestDB) or Debt-003 Python 3.14/3.11 venv mismatch. Both are archite

## Files to Check
  - tests/test_kelly.py
  - tests/test_model_trainer_coverage.py
  - tests/test_orchestrator.py
  - tests/test_performance_drift.py
  - tests/test_portfolio_correlation.py
  - tests/test_probabilistic_engine.py
  - docs/SELF_TUNING_DESIGN.md
  - src/config.py
  - src/tuning/__init__.py
  - src/tuning/audit.py
  - src/tuning/registry.py
  - src/tuning/store.py
  - tests/test_tuning_audit.py
  - tests/test_tuning_registry.py
  - tests/test_tuning_store.py

## Session History (last 5)
  [2026-07-10 23:38:20] claude — interrupted: shell session started — GAP-006 storage migration decision (TimescaleDB vs Quest
  [2026-07-10 04:35:21] claude — interrupted: shell session started — GAP-006 storage migration decision (TimescaleDB vs Quest
  [2026-07-10 00:30:53] claude — interrupted: shell session started — GAP-006 storage migration decision (TimescaleDB vs Quest
  [2026-07-09 23:51:29] claude — interrupted: shell session started — GAP-006 storage migration decision (TimescaleDB vs Quest
  [2026-07-09 23:50:27] claude — interrupted: shell exited with 25 uncommitted file(s)

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
