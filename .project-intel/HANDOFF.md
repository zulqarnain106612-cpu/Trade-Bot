# Agent Handoff State
> Updated: 2026-06-30 03:47:26 | Read this before starting any work.

## Current Status
**Agent**:   claude
**Status**:  🟢 ACTIVE
**Task**:    shell session started — TASK-010: wire live spread_bps and funding_rate_8h into SignalContext in signal_engine.py
**Started**: 2026-06-29 17:31:43
**Last checkpoint**: 2026-06-30 03:47:26

## ⚠ ANOTHER AGENT IS ACTIVE
If claude is no longer running, status is stale.
Check: `git log --oneline -3` — if no recent commits, agent likely crashed.
Safe to take over: run `python3 .project-intel/scripts/handoff.py start --agent YOUR_AGENT --task 'resume'`

## Next Step for Incoming Agent
  TASK-011 or coverage gap — check OPEN_TASKS.md

## Files to Check
  - .claude/CLAUDE.md
  - .github/workflows/auto-fix.yml
  - .project-intel/GAPS.md
  - .project-intel/HANDOFF.md
  - .project-intel/ISSUES.md
  - .project-intel/RISK_LOG.md
  - .project-intel/SECURITY_ISSUES.md
  - .project-intel/SESSION_STATE.json
  - .project-intel/TECH_DEBT.md
  - .project-intel/scripts/resume.py
  - CLAUDE.md
  - .project-intel/MODULE_MAP_SLIM.json
  - .project-intel/scripts/extract_intelligence.py
  - .claudeignore
  - .project-intel/ARCHITECTURE.md
  - .project-intel/MODULE_MAP.json
  - .project-intel/RAW_SCAN.json
  - src/data/fetcher.py
  - src/engine/signal_engine.py
  - tests/test_signal_engine.py
  - src/data/fetcher.py src/engine/signal_engine.py tests/test_signal_engine.py

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