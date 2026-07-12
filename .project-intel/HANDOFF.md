# Agent Handoff State
> Updated: 2026-07-12 16:34:47 | Read this before starting any work.

## Current Status
**Agent**:   claude
**Status**:  🔴 INTERRUPTED — resume required
**Task**:    shell session started — GAP-006 storage migration decision (TimescaleDB vs QuestDB) or Debt-003 Python 3.14/3.11 venv mismat
**Started**: 2026-07-12 12:32:42
**Last checkpoint**: 2026-07-12 16:34:47

## ⚠ INTERRUPTION — Resume from here
**Reason**: shell exited with 47 uncommitted file(s)

### What was completed before interruption:

### Exact resume point:
  Debt-003 (Claude-actionable): pin .venv to Python 3.11 (`uv python pin 3.11 && uv sync`), rerun full

### Files modified (may have uncommitted changes):

### Action required:
  1. Run `git status` — check for uncommitted changes
  2. Run `git diff` — review what was partially done
  3. Read the files listed above — continue from next_step above
  4. Do NOT restart from scratch — work is partially done

## Next Step for Incoming Agent
  Debt-003 (Claude-actionable): pin .venv to Python 3.11 (`uv python pin 3.11 && uv sync`), rerun full

## Files to Check
  (no specific files — start from OPEN_TASKS.md)

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
