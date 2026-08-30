---
name: programmatic-tool-calling
description: >
  Use whenever a task needs 3+ dependent tool/API calls, a loop over many
  entities (per-user, per-file, per-record), a sequential dependency (call B
  needs the filtered output of call A), or any tool result that is large or
  metadata-heavy. Also trigger on phrases like "for each", "for all X",
  "aggregate", "summarize across", "check budgets/status for everyone who...".
  Do NOT trigger for a single, independent tool call.
---

# Programmatic Tool Calling (PTC) -- local adaptation for Claude Code

## Why this exists

Anthropic's hosted API has a beta feature, Programmatic Tool Calling
(`advanced-tool-use-2025-11-20` + `allowed_callers` +
`code_execution_20250825`), where the model writes code that calls tools
inside a remote sandbox, so raw tool payloads never re-enter the model's
context. This project has **no API access** -- only Claude Pro / Claude Code.
Claude Code already runs code locally via the bash tool in the same
environment as this repo's tool implementations, so the API's
`allowed_callers` / `container_id` / `caller` machinery (which exists only to
bridge a *remote* sandbox back to Anthropic's tool-execution loop) is not
needed here. The same benefit -- token reduction, latency reduction on
sequential/looped calls -- is achieved more directly: **write one script,
run it once, return only the digest.**

## Decision checklist (stop and check before calling any tool)

Use the orchestration-script workflow below if ANY of these are true:

- [ ] The task loops over 3+ entities (users, files, records, endpoints).
- [ ] A later call depends on filtering/aggregating an earlier call's result
      (e.g. "check X only for the ones that failed Y").
- [ ] Any single tool call's raw output would be large, deeply nested, or
      full of fields irrelevant to the final answer (logs, receipts,
      metadata, pagination noise).
- [ ] The final answer requires arithmetic, sorting, filtering, or joins
      across multiple calls' results.

Otherwise, just call the tool directly -- do not add orchestration overhead
for a single trivial call.

## Mandatory workflow when the checklist matches

1. **Identify orchestratable tools.** Only call functions registered with
   `orchestratable=True` in `tools/registry.py` from a script. Tools without
   that flag (destructive, rate-limited, requiring human-in-the-loop
   confirmation -- e.g. sending email, deleting records, making payments)
   must stay in-conversation, one call at a time, never in a loop.
   `registry.assert_orchestratable(name)` raises `PermissionError` if
   violated -- the generated script must call it, or import only from
   `registry.namespace()` which already filters to safe tools.

2. **Write one script** under `scripts/` (not inline in the chat), based on
   `scripts/orchestration_template.py`. The script must:
   - Import tool functions only via `tools.registry.registry.namespace()`
     or explicit `from tools.example_tools import ...` for functions marked
     orchestratable.
   - Use `asyncio.gather` (or a thread/process pool for sync-only tools) to
     parallelize independent calls -- never a naive sequential `for` loop
     over independent entities.
   - Do all filtering, aggregation, joins, and arithmetic in Python, not in
     the model's head.
   - Print **only** a compact JSON or plain-text digest to stdout --
     the minimum needed to answer the user. Never `print()` full raw
     payloads (receipts, tokens, full record dumps).

3. **Run it once** via the bash tool: `python3 scripts/<name>.py`.
   Read only stdout. If the script errors, fix the script and re-run --
   do not fall back to manual per-item tool calls unless the script
   approach is fundamentally infeasible (say so explicitly if that happens).

4. **Answer from the digest.** The conversational reply is built from the
   script's printed summary, not from re-deriving totals manually.

## Explicit non-goals

- This is not a re-implementation of the API's `code_execution_20250825`
  server tool, container lifecycle, or `caller` field -- those are
  hosted-API-only constructs with no Claude-Pro/Claude-Code equivalent.
- This does not bypass any tool's own rate limits, auth, or side-effect
  safety -- `orchestratable=True` is an explicit, per-tool opt-in the repo
  owner sets in `tools/registry.py`, not a default.
