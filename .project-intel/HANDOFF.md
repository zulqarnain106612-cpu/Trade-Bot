# Agent Handoff State
> Updated: 2026-06-29 17:19:03 | Read this before starting any work.

## Current Status
**Agent**:   claude
**Status**:  🟢 ACTIVE
**Task**:    not set
**Started**: unknown
**Last checkpoint**: 2026-06-29 17:19:03

## ⚠ ANOTHER AGENT IS ACTIVE
If claude is no longer running, status is stale.
Check: `git log --oneline -3` — if no recent commits, agent likely crashed.
Safe to take over: run `python3 .project-intel/scripts/handoff.py start --agent YOUR_AGENT --task 'resume'`

## Next Step for Incoming Agent
  TASK-010: wire live spread_bps and funding_rate_8h into SignalContext in signal_engine.py

## Files to Check
  - .project-intel/ARCHITECTURE.md
  - .project-intel/MODULE_MAP.json
  - .project-intel/RAW_SCAN.json
  - .project-intel/SESSION_STATE.json
  - .project-intel/scripts/agent_detect.py
  - requirements.in
  - src/intelligence/ensemble_predictor.py
  - .project-intel/HANDOFF.md

## Session History (last 5)
  [2026-06-29 14:43:07] claude — completed: Handoff system built — 3-agent protocol (claude/copilot/amazonq) with stale sess

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