# Agent Handoff State
> Updated: 2026-07-07 19:41:08 | Read this before starting any work.

## Current Status
**Agent**:   amazonq
**Status**:  🟢 ACTIVE
**Task**:    shell session started — GAP-015 follow-on: provision GLASSNODE_API_KEY/CRYPTOQUANT_API_KEY (see DECISION_LOG.md), then build
**Started**: 2026-07-07 18:34:30
**Last checkpoint**: 2026-07-07 19:41:08

## ⚠ ANOTHER AGENT IS ACTIVE
If amazonq is no longer running, status is stale.
Check: `git log --oneline -3` — if no recent commits, agent likely crashed.
Safe to take over: run `python3 .project-intel/scripts/handoff.py start --agent YOUR_AGENT --task 'resume'`

## Next Step for Incoming Agent
  Gap-018: add __init__.py stub to src/intelligence/onchain/ OR remove directory. Gap-020: add per-package coverage floors to pyproject.toml. SEC-008/SEC-010: remove push: trigger from auto-fix.yml or confirm branch protection.

## Files to Check
  - .amazonq/rules/project.md
  - .amazonq/rules/trade-bot.md
  - .claude/CLAUDE.md
  - .project-intel/ARCHITECTURE.md
  - .project-intel/GAPS.md
  - .project-intel/HANDOFF.md
  - .project-intel/ISSUES.md
  - .project-intel/MODULE_MAP.json
  - .project-intel/MODULE_MAP_SLIM.json
  - .project-intel/RAW_SCAN.json
  - .project-intel/RISK_LOG.md
  - .project-intel/SECURITY_ISSUES.md
  - .project-intel/SESSION_STATE.json
  - .project-intel/TECH_DEBT.md
  - .project-intel/scripts/context_builder.py
  - src/diagnostics/runtime_monitor.py
  - .env.example
  - src/intelligence/causal_inference.py
  - src/intelligence/ensemble_predictor.py
  - src/intelligence/risk_quantification.py
  - src/diagnostics/runtime_monitor.py .env.example src/intelligence/causal_inference.py src/intelligence/ensemble_predictor.py src/intelligence/risk_quantification.py .project-intel/scripts/context_builder.py .project-intel/GAPS.md .project-intel/ISSUES.md .project-intel/TECH_DEBT.md .project-intel/SECURITY_ISSUES.md .project-intel/RISK_LOG.md

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