# Agent Handoff State
> Updated: 2026-07-10 10:56:43 | Read this before starting any work.

## Current Status
**Agent**:   claude
**Status**:  🟢 ACTIVE
**Task**:    shell session started — GAP-006 storage migration decision (TimescaleDB vs QuestDB) or Debt-003 Python 3.14/3.11 venv mismat
**Started**: 2026-07-10 04:35:21
**Last checkpoint**: 2026-07-10 10:56:43

## ⚠ ANOTHER AGENT IS ACTIVE
If claude is no longer running, status is stale.
Check: `git log --oneline -3` — if no recent commits, agent likely crashed.
Safe to take over: run `python3 .project-intel/scripts/handoff.py start --agent YOUR_AGENT --task 'resume'`

## Next Step for Incoming Agent
  GAP-006 storage migration decision (TimescaleDB vs QuestDB) or Debt-003 Python 3.14/3.11 venv mismatch. Both are archite

## Files to Check
  - docker-compose.yml
  - requirements.in
  - scripts/migrate_sqlite_to_timescale.py
  - scripts/timescaledb.sh
  - src/api/main.py
  - src/config.py
  - src/data/fetcher.py
  - src/data/storage.py
  - src/data/timescale_storage.py
  - src/engine/orchestrator.py
  - src/engine/signal_engine.py
  - src/execution/live.py
  - src/execution/paper.py
  - tests/test_api_main_coverage.py
  - tests/test_timescale_storage.py

## Session History (last 5)
  [2026-07-10 04:35:21] claude — interrupted: shell session started — GAP-006 storage migration decision (TimescaleDB vs Quest
  [2026-07-10 00:30:53] claude — interrupted: shell session started — GAP-006 storage migration decision (TimescaleDB vs Quest
  [2026-07-09 23:51:29] claude — interrupted: shell session started — GAP-006 storage migration decision (TimescaleDB vs Quest
  [2026-07-09 23:50:27] claude — interrupted: shell exited with 25 uncommitted file(s)
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
