# Agent Handoff State
> Updated: 2026-07-09 18:35:37 | Read this before starting any work.

## Current Status
**Agent**:   claude
**Status**:  🟢 ACTIVE
**Task**:    shell session started — GAP-015 follow-on: provision GLASSNODE_API_KEY/CRYPTOQUANT_API_KEY (see DECISION_LOG.md), then build
**Started**: 2026-07-09 01:31:05
**Last checkpoint**: 2026-07-09 18:35:37

## ⚠ ANOTHER AGENT IS ACTIVE
If claude is no longer running, status is stale.
Check: `git log --oneline -3` — if no recent commits, agent likely crashed.
Safe to take over: run `python3 .project-intel/scripts/handoff.py start --agent YOUR_AGENT --task 'resume'`

## Next Step for Incoming Agent
  GAP-015 follow-on: provision GLASSNODE_API_KEY/CRYPTOQUANT_API_KEY (see DECISION_LOG.md), then build historical intellig

## Files to Check
  - tests/test_orchestrator_coverage.py
  - src/engine/signal_engine.py
  - src/intelligence/client.py
  - tests/test_intelligence_providers.py
  - src/intelligence/providers/aggregator.py
  - src/intelligence/onchain/schema.py
  - scripts/backfill_intelligence.py
  - src/data/storage.py
  - src/features/intelligence_features.py
  - src/intelligence/metrics.py
  - src/features/pipeline.py
  - src/models/trainer.py
  - tests/test_gap015_backfill.py
  - .project-intel/HANDOFF.md
  - .project-intel/SESSION_STATE.json

## Session History (last 5)
  [2026-07-09 18:32:40] claude — interrupted: shell exited with 5 uncommitted file(s)
  [2026-07-09 01:31:05] claude — interrupted: shell session started — GAP-015 follow-on: provision GLASSNODE_API_KEY/CRYPTOQUA
  [2026-07-07 18:34:44] claude — interrupted: shell exited with 2 uncommitted file(s)
  [2026-07-07 18:34:39] claude — interrupted: shell exited with 2 uncommitted file(s)
  [2026-07-07 18:34:30] claude — interrupted: shell session started — GAP-015 follow-on: provision GLASSNO

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