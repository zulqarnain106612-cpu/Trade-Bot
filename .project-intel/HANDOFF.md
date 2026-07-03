# Agent Handoff State
> Updated: 2026-07-03 18:27:10 | Read this before starting any work.

## Current Status
**Agent**:   claude
**Status**:  🟢 ACTIVE
**Task**:    shell session started — GAP-015 follow-on: provision GLASSNODE_API_KEY/CRYPTOQUANT_API_KEY (see DECISION_LOG.md), then build
**Started**: 2026-07-03 18:27:10
**Last checkpoint**: 2026-07-03 18:27:10

## ⚠ ANOTHER AGENT IS ACTIVE
If claude is no longer running, status is stale.
Check: `git log --oneline -3` — if no recent commits, agent likely crashed.
Safe to take over: run `python3 .project-intel/scripts/handoff.py start --agent YOUR_AGENT --task 'resume'`

## Next Step for Incoming Agent
  shell session started — GAP-015 follow-on: provision GLASSNODE_API_KEY/CRYPTOQUANT_API_KEY (see DECISION_LOG.md), then build

## Files to Check
  (no specific files — start from OPEN_TASKS.md)

## Session History (last 5)
  [2026-07-03 18:27:10] claude — interrupted: shell session started — GAP-015 follow-on: provision GLASSNODE_API_KEY/CRYPTOQUA
  [2026-07-03 18:25:39] claude — interrupted: GAP-015 steps 2-5: historical fetch methods, intelligence_features_history table
  [2026-07-03 18:08:16] claude — interrupted: GAP-015 full implementation: torch install + ensemble test, probabilistic/intell
  [2026-07-01 02:35:45] claude — interrupted: shell session started — TASK-010: wire live spread_bps and funding_rate_8h into 
  [2026-06-29 18:16:34] claude — interrupted: shell exited with 8 uncommitted file(s)

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