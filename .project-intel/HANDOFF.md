# Agent Handoff State
> Updated: 2026-07-05 19:45:28 | Read this before starting any work.

## Current Status
**Agent**:   claude
**Status**:  🟢 ACTIVE
**Task**:    shell session started — GAP-015 follow-on: provision GLASSNODE_API_KEY/CRYPTOQUANT_API_KEY (see DECISION_LOG.md), then build
**Started**: 2026-07-03 22:01:55
**Last checkpoint**: 2026-07-05 19:45:28

## ⚠ ANOTHER AGENT IS ACTIVE
If claude is no longer running, status is stale.
Check: `git log --oneline -3` — if no recent commits, agent likely crashed.
Safe to take over: run `python3 .project-intel/scripts/handoff.py start --agent YOUR_AGENT --task 'resume'`

## Next Step for Incoming Agent
  GAP-015 follow-on: provision GLASSNODE_API_KEY/CRYPTOQUANT_API_KEY (see DECISION_LOG.md), then build historical intellig

## Files to Check
  - .project-intel/ARCHITECTURE.md
  - .project-intel/HANDOFF.md
  - .project-intel/MODULE_MAP.json
  - .project-intel/SESSION_STATE.json
  - tests/test_signal_engine.py
  - src/diagnostics/signal_debugger.py
  - tests/test_performance_drift.py
  - .env
  - .project-intel/MODULE_MAP_SLIM.json
  - .project-intel/RAW_SCAN.json
  - src/api/main.py
  - src/api/metrics.py
  - requirements.txt
  - tests/test_metrics.py
  - src/engine/orchestrator.py
  - src/intelligence/risk_quantification.py
  - src/intelligence/probabilistic_adapter.py
  - src/engine/signal_engine.py
  - .project-intel/PROBABILISTIC_LAYER_CALIBRATION_AUDIT.md
  - src/data/storage.py
  - scripts/backfill_intelligence.py
  - src/features/pipeline.py
  - src/models/trainer.py
  - tests/test_gap015_backfill.py
  - src/intelligence/providers/base.py
  - src/intelligence/providers/okx_provider.py
  - src/intelligence/providers/blockchain_provider.py
  - src/intelligence/providers/coingecko_provider.py
  - src/intelligence/providers/aggregator.py
  - tests/test_intelligence_providers.py
  - src/intelligence/providers/binance_provider.py
  - .claude/CLAUDE.md
  - .project-intel/GAPS.md
  - src/diagnostics/runtime_monitor.py
  - src/execution/live_fsm_integration.py
  - src/execution/order_fsm.py
  - src/features/intelligence_features.py
  - src/intelligence/__init__.py
  - src/intelligence/causal_inference.py
  - src/intelligence/client.py
  - src/intelligence/ensemble_predictor.py
  - src/intelligence/metrics.py
  - src/intelligence/probabilistic.py
  - src/risk/drift_integration.py
  - src/risk/gates.py
  - src/risk/performance_drift.py
  - src/risk/portfolio_correlation.py
  - src/execution/live.py
  - .project-intel/ISSUES.md
  - frontend/src/App.jsx
  - .project-intel/CONTEXT_PRIMER.md

## Session History (last 5)
  [2026-07-04 00:23:37] claude — interrupted: shell exited with 2 uncommitted file(s)
  [2026-07-04 00:23:37] claude — interrupted: shell exited with 2 uncommitted file(s)
  [2026-07-03 22:01:55] claude — interrupted: shell session started — GAP-015 follow-on: provision GLASSNODE_API_KEY/CRYPTOQUA
  [2026-07-03 21:58:54] claude — interrupted: shell exited with 3 uncommitted file(s)
  [2026-07-03 21:42:44] claude — interrupted: shell session started — GAP-015 follow-on: provision GLASSNODE_API_KEY/CRYPTOQUA

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

## ⚠ MANDATORY COMPLETION CRITERIA (Debt-010 — enforced 2026-07-05)
A task/module is ONLY COMPLETE when ALL THREE are true:
  1. Unit tests pass (`pytest tests/test_<module>.py -q`)
  2. Coverage is nonzero for the file (`pytest --cov=src/path/module.py` → not 0%)
  3. The module is imported by signal_engine.py, gates.py, or orchestrator.py
     (verify: `grep -rn "from src.<module>" src/engine/ src/risk/gates.py`)

If criterion 3 is not met: mark the module EXPERIMENTAL/UNUSED in MODULE_MAP.json
and CONTEXT_PRIMER.md — do NOT mark it COMPLETE in SESSION_STATE.json.

This rule exists because multiple prior sessions marked modules COMPLETE based
on passing unit tests alone, while the modules were fully disconnected from the
live signal path (Gap-015, Gap-017, Debt-010).