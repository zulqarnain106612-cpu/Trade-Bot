# Agent Handoff State
> Updated: 2026-07-07 18:34:44 | Read this before starting any work.

## Current Status
**Agent**:   claude
**Status**:  🔴 INTERRUPTED — resume required
**Task**:    shell session started — GAP-015 follow-on: provision GLASSNODE_API_KEY/CRYPTOQUANT_API_KEY (see DECISION_LOG.md), then build
**Started**: 2026-07-07 18:34:30
**Last checkpoint**: 2026-07-07 18:34:44

## ⚠ INTERRUPTION — Resume from here
**Reason**: shell exited with 2 uncommitted file(s)

### What was completed before interruption:

### Exact resume point:
  GAP-015 follow-on: provision GLASSNODE_API_KEY/CRYPTOQUANT_API_KEY (see DECISION_LOG.md), then build

### Files modified (may have uncommitted changes):

### Action required:
  1. Run `git status` — check for uncommitted changes
  2. Run `git diff` — review what was partially done
  3. Read the files listed above — continue from next_step above
  4. Do NOT restart from scratch — work is partially done

## Next Step for Incoming Agent
  GAP-015 follow-on: provision GLASSNODE_API_KEY/CRYPTOQUANT_API_KEY (see DECISION_LOG.md), then build

## Files to Check
  (no specific files — start from OPEN_TASKS.md)

## Session History (last 5)
  [2026-07-07 18:34:44] claude — interrupted: shell exited with 2 uncommitted file(s)
  [2026-07-07 18:34:39] claude — interrupted: shell exited with 2 uncommitted file(s)
  [2026-07-07 18:34:30] claude — interrupted: shell session started — GAP-015 follow-on: provision GLASSNODE_API_KEY/CRYPTOQUA
  [2026-07-07 18:34:07] claude — interrupted: shell session started — GAP-015 follow-on: provision GLASSNODE_API_KEY/CRYPTOQUA
  [2026-07-07 17:55:29] claude — interrupted: shell session started — GAP-015 follow-on: provision GLASSNODE_API_KEY/CRYPTOQUA

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