# Agent Handoff State
> Updated: 2026-07-12 17:30:42 | Read this before starting any work.

## Current Status
**Agent**:   claude
**Status**:  🟢 ACTIVE
**Task**:    shell session started — GAP-006 storage migration decision (TimescaleDB vs QuestDB) or Debt-003 Python 3.14/3.11 venv mismat
**Started**: 2026-07-12 12:32:42
**Last checkpoint**: 2026-07-12 17:30:42

## ⚠ ANOTHER AGENT IS ACTIVE
If claude is no longer running, status is stale.
Check: `git log --oneline -3` — if no recent commits, agent likely crashed.
Safe to take over: run `python3 .project-intel/scripts/handoff.py start --agent YOUR_AGENT --task 'resume'`

## Next Step for Incoming Agent
  Commit the 17 in-flight Phase 8 self-tuning files (config.py, tuning/*, regime/detector.py, risk/slippage.py, scripts/ru

## Files to Check
  - .project-intel/DECISION_LOG.md
  - .project-intel/DIAGNOSTICS.md
  - .project-intel/HANDOFF.md
  - .project-intel/MODULE_MAP.json
  - .project-intel/MODULE_MAP_SLIM.json
  - .project-intel/OPEN_TASKS.md
  - .project-intel/SESSION_STATE.json
  - .project-intel/scripts/extract_intelligence.py
  - .project-intel/scripts/handoff.py
  - .project-intel/scripts/resume.py
  - CLAUDE.md
  - docs/ROADMAP_NEXT_STEPS_20260712.md
  - docs/reference/glassnode-features.md
  - glassnode-features.md
  - scripts/export_diagnostics.py

## Session History (last 5)
  [2026-07-12 16:34:47] claude — interrupted: shell exited with 47 uncommitted file(s)
  [2026-07-12 12:32:42] claude — interrupted: shell session started — GAP-006 storage migration decision (TimescaleDB vs Quest
  [2026-07-12 12:09:14] claude — interrupted: shell session started — GAP-006 storage migration decision (TimescaleDB vs Quest
  [2026-07-11 06:43:06] claude — interrupted: shell session started — GAP-006 storage migration decision (TimescaleDB vs Quest
  [2026-07-11 02:04:38] claude — interrupted: shell session started — GAP-006 storage migration decision (TimescaleDB vs Quest

## Quick Start for Any Agent
```
# 1. Get context (single command, do NOT cat files directly):
python3 .project-intel/scripts/resume.py .

# 2. Register yourself:
python3 .project-intel/scripts/handoff.py start --agent YOUR_AGENT --task 'describe task'

# 3. Checkpoint as you work (every meaningful step):
python3 .project-intel/scripts/handoff.py checkpoint --agent YOUR_AGENT \
  --completed 'what you just did' --next 'exact next action' --files 'src/x.py'
```
