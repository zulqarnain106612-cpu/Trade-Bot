# Local plan-big / execute-small orchestrator

Adapts Anthropic's Managed Agents cookbook (`CMA_plan_big_execute_small.ipynb`)
for Claude Pro / Claude Code -- no Managed Agents beta or API key required.
Coordinator (`COORDINATOR_MODEL`) plans once, cheap workers (`WORKER_MODEL`)
execute subtasks in parallel threads, coordinator synthesizes from digests
only. See `orchestrator/run.py` docstring for the full discipline.

Usage:

    python orchestrator_cli.py "Summarize what changed across all files in src/ this week"
