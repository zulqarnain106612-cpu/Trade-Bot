# Agent Reliability and Evidence Control

Completion is a mechanically verified state, not a model assertion.

- CI diagnostics exposed to the model come from the single pull-request notice channel.
- Raw CI logs, check-run records, annotations, artifacts, and live monitoring are not model observation channels.
- CI verdicts are bound to the exact commit SHA they describe.
- A new HEAD requires a new CI verdict before a PR-delivery task can be complete.
- Repeated equivalent failures are recorded as one failure lineage and escalate instead of silently repeating the same correction.
- Agent-controlled policy disable switches do not exist.
- Control-plane paths are CODEOWNED.
- Missing reliability evidence blocks completion rather than being inferred.

Workflow: failure -> diagnose -> correct -> targeted verification -> CI -> green evidence -> completion.

Claude Code's PostToolUseFailure hook can add context but cannot replace the native failed tool result. The repository therefore bounds large diagnostic producers where possible and records a separate compact failure lineage.

GitHub Code Owner protection becomes merge-blocking only when the protected branch/ruleset requires Code Owner approval; that setting is outside repository contents and must be enabled by a repository administrator.
