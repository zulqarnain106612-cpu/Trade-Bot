# Agent Handoff State
> Updated: 2026-06-29 18:16:34 | Read this before starting any work.

## Current Status
**Agent**:   claude
**Status**:  🔴 INTERRUPTED — resume required
**Task**:    shell session started — TASK-010: wire live spread_bps and funding_rate_8h into SignalContext in signal_engine.py
**Started**: 2026-06-29 17:31:43
**Last checkpoint**: 2026-06-29 18:16:34

## ⚠ INTERRUPTION — Resume from here
**Reason**: shell exited with 8 uncommitted file(s)

### What was completed before interruption:

### Exact resume point:
  TASK-010: wire live spread_bps and funding_rate_8h into SignalContext in signal_engine.py

### Files modified (may have uncommitted changes):

### Action required:
  1. Run `git status` — check for uncommitted changes
  2. Run `git diff` — review what was partially done
  3. Read the files listed above — continue from next_step above
  4. Do NOT restart from scratch — work is partially done

## Next Step for Incoming Agent
  TASK-010: wire live spread_bps and funding_rate_8h into SignalContext in signal_engine.py

## Files to Check
  (no specific files — start from OPEN_TASKS.md)

## Session History (last 5)
  [2026-06-29 18:16:34] claude — interrupted: shell exited with 8 uncommitted file(s)
  [2026-06-29 17:31:43] claude — interrupted: 
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