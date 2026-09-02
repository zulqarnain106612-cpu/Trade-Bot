
# Project directives

## Programmatic Tool Calling (PTC) pattern

For any task involving 3+ dependent/looped tool calls, or a large/metadata-heavy
tool result: do not call tools one at a time in conversation. Instead follow
`.claude/skills/programmatic-tool-calling/SKILL.md` -- write one script under
`scripts/`, importing only functions from `tools/registry.py`'s
`registry.namespace()` (orchestratable=True tools), and run it once via bash.
Return the answer from the script's printed digest, not from raw tool output
pasted into the conversation.

Never mark a destructive, rate-limited, or side-effecting tool
`orchestratable=True` in `tools/registry.py`. Those stay direct-call-only.

See `docs/PTC_PATTERN.md` for the full rationale and the API-vs-local mapping.

## Orchestrator (plan-big / execute-small)

For a task that naturally splits into independent research/lookup subtasks
followed by a synthesis step, use `orchestrator_cli.py "<task>"` instead of
doing all the reading yourself. It delegates subtasks to a cheap model
(`WORKER_MODEL`, default haiku) and only has the coordinator model
(`COORDINATOR_MODEL`, default sonnet) see short digests, not raw output.
See `docs/ORCHESTRATOR.md`.

## Knowledge Graph (Extract -> Resolve -> Assemble -> Query)

For questions that span multiple documents and require chaining facts
("who works with people who worked on project X"), use `kg_cli.py query`
instead of re-reading every source document. Ingest sources once with
`kg_cli.py ingest <file>` -- this persists structured facts in MongoDB so
future questions don't re-pay the extraction cost. Queries only load a
bounded subgraph (`KG_MAX_SUBGRAPH_TRIPLES`), never the whole graph.
See `docs/KNOWLEDGE_GRAPH.md`.

## Command Execution (output-capped, schema-enforced)

Every shell command Claude runs must be declared as a `COMMAND_EXEC_SCHEMA`
object (see `common/command_schema.py`) and executed via `common/shell_exec.run()`.
Raw stdout/stderr never enters context. Works in local terminal sessions and
cloud containers (`CLAUDE_CODE_REMOTE=true`).
Skill: `.claude/skills/command-execution/SKILL.md`

Hard rules:
- `max_lines` cap is mandatory on every command. Default: 50.
- Use `filter_mode` (grep/tail/regex/fields) to extract only needed data.
- `filter_mode=jq` requires `jq` on PATH -- use `fields`/`regex` on cloud containers.
- If `result["truncated"]` is True -> fix `filter_expr`, never raise `max_lines`.
- Never retry destructive commands (rm, DROP, DELETE).
- Never set `max_lines` > 100 without explicit justification.

## Cloud review + retrieval (Component 5)

Every pull request is automatically reviewed by `.github/workflows/claude-review.yml`,
grounded in this project's MongoDB Atlas RAG + knowledge graph via
`review/retrieval.py` (vector/full-text/hybrid/graph patterns). This is
advisory only -- it never approves or merges. See `docs/CLOUD_REVIEW.md` for
setup and the one-time secrets/GitHub App requirements.
