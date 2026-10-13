---
name: trade-bot-parallel-development
description: Use when a Trade-Bot task can be split into genuinely independent analysis or implementation streams and needs coordinated parallel agent/sub-agent work.
---

# Parallel agentic and sub-agentic development

## Decide whether to parallelize

Parallelize only when subtasks have distinct deliverables, bounded ownership, and limited dependencies. Good examples are independent CI failure triage, separate module audits, documentation/skill research, or independent tests. Do not spawn agents just to appear faster, for a tiny single-file fix, or when multiple agents would edit the same shared surface.

## Plan before delegation

1. Establish current repository state, task goal, user constraints, security policy, validation policy, and a shared acceptance checklist.
2. Decompose into the smallest independent workstreams. Assign each stream an explicit question or deliverable, allowed paths, forbidden paths, required evidence, and stop condition.
3. Give every agent the same governing repository instructions and tell it to preserve existing user work. Never send secrets, API keys, exchange credentials, or private user data as task context.
4. Prefer read-only parallel analysis first. Ask each analyst for verified findings, source references, hypotheses clearly labeled as such, conflicts, and the minimum recommended change.
5. Use isolated worktrees/branches for concurrent writes. Avoid shared working directories and concurrent edits to the same files. A sub-agent must not commit, push, merge, deploy, or change account settings unless explicitly authorized for that exact action.

## Integrate results

1. The coordinating agent owns architecture, shared contracts, conflict resolution, and final delivery. Do not blindly concatenate independently generated patches.
2. Check each proposed change against the current base SHA; rebase/review stale findings before applying them.
3. Resolve duplicated abstractions, inconsistent assumptions, dependency drift, test overlap, and contradictory documentation before merging workstreams.
4. Preserve provenance: record which agent handled which bounded question, the evidence it inspected, and which findings were accepted or rejected. Do not claim an agent ran a test unless the result is visible and policy permits that test.
5. Run the repository's permitted validation path only. For Trade-Bot, do not execute tests/build/lint/format/type/security checks locally; after an authorized push use `python3 scripts/local_checks.py prepare` and accept only a notice tied to the exact current HEAD SHA.
6. Review the combined diff as one change, verify all touched paths are in scope, and produce one coherent PR unless the user explicitly requests separate delivery units.

## Failure and escalation

- If an agent returns incomplete or unsupported evidence, ask for the missing bounded evidence or perform the read-only inspection yourself.
- If streams discover conflicting facts, do not average them: return to source-of-truth code/config/current GitHub state.
- If a dependency blocks another stream, pause that stream instead of duplicating the blocked work.
- Keep parallelism bounded; use a small number of independent workstreams rather than an uncontrolled agent swarm.

## References

- Official Cookbook: [Iterating development workflows with Codex](https://github.com/openai/openai-cookbook/blob/main/examples/codex/iterating-development-workflows-with-codex.md)
- Official Cookbook: [Multi-agent portfolio collaboration example](https://github.com/openai/openai-cookbook/blob/main/examples/agents_sdk/multi-agent-portfolio-collaboration/multi_agent_portfolio_collaboration.ipynb) (archived example; use for orchestration concepts only)
- Current Codex configuration and supported capabilities must be checked against official Codex docs, not inferred from a Cookbook notebook.
