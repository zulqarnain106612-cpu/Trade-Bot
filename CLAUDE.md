
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

## Command execution is enforced, not advised

`COMMAND_EXEC_SCHEMA` is version **1.1.0** (`common/command_schema.py`). Beyond
the output caps above, every declaration may and usually should carry:

- `classification`: `read_only` | `mutating` | `destructive`. **Mandatory in
  effect** -- `shell_exec.run()` refuses to execute when the declared class is
  weaker than what `classify()` detects in the command string. Misdeclaring is
  a hard error, not a warning.
- `confirm_destructive`: required alongside `classification="destructive"`.
  Destructive commands are never retried, whatever `retry_policy` says.
- `timeout_s`: per-attempt wall clock, 1-900s. Declare it; do not rely on the
  `run()` kwarg default.
- `cwd`: validated to exist before anything runs.
- `env`: `{inherit, allowlist, overrides}`. Use `allowlist` whenever the child
  does not need the parent's secrets, which is almost always.
- `output_policy.max_bytes`: byte cap, default 65536. `max_lines` alone is not
  a bound -- one minified or base64 line can be megabytes.
- `output_policy.redact`: secret masking, **on by default**. Turn it off only
  when you have established the output cannot contain a credential.

`result` now also carries `bytes_truncated`, `timed_out`, `duration_s`,
`classification`, `redactions_applied`, `command_sha256` and `started_at`.

A `PreToolUse` hook (`.claude/hooks/pre_tool_use.py`, policy in
`config/command_policy.json`) enforces the same rules on raw Bash calls, so
these constraints hold in a session that never read this file:

- Unbounded reads are refused. Use `sed -n '1,5p'`, `head -5`, `grep -m 5`,
  `-n 5`. Then fetch the next five in a separate call.
- A bound larger than 5 lines is refused. Widening the first fetch is the
  specific thing the directive forbids; page instead.
- Destructive commands are refused, with the `shell_exec` path named in the
  refusal.
- Commands that would print credentials into the transcript are refused.

The hook shares `classify()` with the runtime, so the two can never disagree.
It fails **open** on its own misconfiguration and can be relaxed for one
session with `TB_COMMAND_POLICY=warn|off` -- that override is the rollback
path, not a way around a refusal you disagree with.

## Mathematical foundations registry

`config/math_registry.json` is the single source of truth for every
mathematical object this project relies on or explicitly refuses, with a
verdict on each (`load_bearing`, `performance_critical`, `attack_surface`,
`provenance_only`, `folklore`). Load it through `src/mathcore/registry.py`,
which validates it strictly on read.

Rules:

- **Never claim an implementation that does not exist.** An entry marked
  `implemented` must name an owning module present on disk; the loader checks
  the filesystem and refuses otherwise.
- **Never mark a `folklore` entry `implemented`.** Numerology cannot become a
  live signal by editing one field.
- `depends_on` must resolve and the graph must stay acyclic. Both were violated
  by the registry's first draft and caught by the loader; that is why the
  checks exist.
- `docs/MATH_FOUNDATIONS.md` is **generated**. Edit the registry, then run
  `python3 scripts/generate_math_docs.py`. CI runs it with `--check`.

Design laws, module contracts and wiring points: `docs/MATH_ARCHITECTURE.md`.
Build order and exit gates: `docs/MATH_ROADMAP.md`.

## Cloud review + retrieval (Component 5)

Every pull request is automatically reviewed by `.github/workflows/claude-review.yml`,
grounded in this project's MongoDB Atlas RAG + knowledge graph via
`review/retrieval.py` (vector/full-text/hybrid/graph patterns). This is
advisory only -- it never approves or merges. See `docs/CLOUD_REVIEW.md` for
setup and the one-time secrets/GitHub App requirements.
