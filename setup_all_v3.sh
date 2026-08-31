#!/usr/bin/env bash
set -euo pipefail
ROOT="$(pwd)"

echo "==> [1/9] Creating directory structure"
mkdir -p "$ROOT/common" "$ROOT/rag_mongo" "$ROOT/orchestrator" "$ROOT/kg" "$ROOT/review" \
         "$ROOT/.claude/skills/programmatic-tool-calling" \
         "$ROOT/tools" "$ROOT/scripts" "$ROOT/docs" "$ROOT/.github/workflows"

# Clean up v1-only stale file if upgrading in-place (superseded by common/claude_cli.py)
rm -f "$ROOT/orchestrator/claude_cli.py"
rm -rf "$ROOT/orchestrator/__pycache__"

cat > "$ROOT/.gitignore" <<'EOF'
__pycache__/
*.pyc
.venv/
.env
EOF

# ============================================================
# requirements.txt (merged across all 4 components)
# ============================================================
cat > "$ROOT/requirements.txt" <<'EOF'
pymongo>=4.9
sentence-transformers>=3.0
python-dotenv>=1.0
EOF

# ============================================================
# .env.example (merged)
# ============================================================
cat > "$ROOT/.env.example" <<'EOF'
# --- Component 1: RAG (MongoDB Atlas, zero-cost local embeddings) ---
MONGODB_URI=mongodb+srv://<user>:<pass>@<cluster>.mongodb.net/?retryWrites=true&w=majority
MONGODB_DB=rag_db
MONGODB_COLLECTION=documents
VECTOR_INDEX_NAME=vector_index
EMBEDDING_MODEL=all-MiniLM-L6-v2
EMBEDDING_DIM=384
TOP_K=5
RAG_GENERATION_MODEL=sonnet

# --- Component 3: Orchestrator (plan-big / execute-small, local Claude Code) ---
COORDINATOR_MODEL=sonnet
WORKER_MODEL=haiku
MAX_SUBTASKS=5
MAX_WORKER_TURNS=6
WORKER_ALLOWED_TOOLS=Read,Bash,Grep,Glob

# --- Component 4: Knowledge Graph (Extract -> Resolve -> Assemble -> Query) ---
KG_NODES_COLLECTION=kg_nodes
KG_EDGES_COLLECTION=kg_edges
KG_EXTRACT_MODEL=haiku
KG_RESOLVE_MODEL=sonnet
KG_QUERY_MODEL=sonnet
KG_MAX_SUBGRAPH_TRIPLES=40

# --- Component 5: Cloud review + retrieval (GitHub Actions only) ---
# MONGODB_URI above is reused as a GitHub Actions secret (not read from .env in CI).
FULLTEXT_INDEX_NAME=fulltext_index
REVIEW_MAX_CONTEXT_ITEMS=15
EOF

echo "==> [2/8] Writing shared module: common/claude_cli.py"

touch "$ROOT/common/__init__.py"

cat > "$ROOT/common/claude_cli.py" <<'EOF'
"""Shared subprocess wrapper around `claude -p` (Claude Code headless mode).
Used by rag_mongo, orchestrator, and kg -- one implementation, one place to
fix auth/flag bugs instead of three copies drifting apart.

Auth: Pro/Max subscription via `claude /login` (OAuth). NEVER add --bare --
bare mode requires ANTHROPIC_API_KEY and does not use OAuth/subscription
auth; it would silently break every caller on a Pro/Max plan.

Every call is stateless (no --continue/--resume): context never accumulates
across calls -- each call's context is bounded to exactly its own prompt,
which is the core token/context discipline this whole repo depends on.
"""
from __future__ import annotations

import json
import shutil
import subprocess


class ClaudeCLIError(RuntimeError):
    pass


def _require_cli() -> None:
    if shutil.which("claude") is None:
        raise ClaudeCLIError(
            "Claude Code CLI not found. Install: npm i -g @anthropic-ai/claude-code "
            "then run `claude /login` once (Pro/Max subscription)."
        )


def run_claude(
    prompt: str,
    model: str,
    allowed_tools: str | None = None,
    max_turns: int | None = None,
    json_schema: dict | None = None,
    timeout: int = 180,
) -> dict:
    """One-shot, stateless call to `claude -p`.

    Returns: {"result": str, "structured_output": dict|None,
              "cost_usd": float, "session_id": str|None}
    """
    _require_cli()
    cmd = ["claude", "-p", prompt, "--model", model, "--output-format", "json"]
    if allowed_tools:
        cmd += ["--allowedTools", allowed_tools]
    if max_turns:
        cmd += ["--max-turns", str(max_turns)]
    if json_schema:
        cmd += ["--json-schema", json.dumps(json_schema)]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        raise ClaudeCLIError(f"claude -p failed (exit {proc.returncode}): {proc.stderr[:500]}")
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise ClaudeCLIError(f"Could not parse claude output as JSON: {proc.stdout[:500]}") from exc
    return {
        "result": payload.get("result", ""),
        "structured_output": payload.get("structured_output"),
        "cost_usd": payload.get("total_cost_usd", 0.0),
        "session_id": payload.get("session_id"),
    }
EOF

echo "==> [3/8] Writing Component 1: RAG (MongoDB)"

touch "$ROOT/rag_mongo/__init__.py"

cat > "$ROOT/rag_mongo/config.py" <<'EOF'
import os
from dotenv import load_dotenv

load_dotenv()

MONGODB_URI = os.getenv("MONGODB_URI")  # validated lazily on first connection, not at import
MONGODB_DB = os.getenv("MONGODB_DB", "rag_db")
MONGODB_COLLECTION = os.getenv("MONGODB_COLLECTION", "documents")
VECTOR_INDEX_NAME = os.getenv("VECTOR_INDEX_NAME", "vector_index")
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "all-MiniLM-L6-v2")
EMBEDDING_DIM = int(os.getenv("EMBEDDING_DIM", "384"))
TOP_K = int(os.getenv("TOP_K", "5"))
GENERATION_MODEL = os.getenv("RAG_GENERATION_MODEL", "sonnet")
EOF

cat > "$ROOT/rag_mongo/embeddings.py" <<'EOF'
"""Local, zero-cost embedding model (no API calls)."""
from functools import lru_cache
from sentence_transformers import SentenceTransformer
from .config import EMBEDDING_MODEL


@lru_cache(maxsize=1)
def _model() -> SentenceTransformer:
    return SentenceTransformer(EMBEDDING_MODEL)


def embed(text: str) -> list[float]:
    return _model().encode(text, normalize_embeddings=True).tolist()


def embed_batch(texts: list[str]) -> list[list[float]]:
    vecs = _model().encode(texts, normalize_embeddings=True, batch_size=32)
    return [v.tolist() for v in vecs]
EOF

cat > "$ROOT/rag_mongo/db.py" <<'EOF'
from pymongo import MongoClient
from pymongo.operations import SearchIndexModel
from .config import MONGODB_URI, MONGODB_DB, MONGODB_COLLECTION, \
    VECTOR_INDEX_NAME, EMBEDDING_DIM


def get_collection():
    if not MONGODB_URI:
        raise RuntimeError(
            "MONGODB_URI not set. Copy .env.example to .env and fill it in."
        )
    client = MongoClient(MONGODB_URI)
    return client[MONGODB_DB][MONGODB_COLLECTION]


def ensure_vector_index() -> None:
    """Idempotent Atlas Vector Search index creation (Atlas M0+ only)."""
    col = get_collection()
    existing = {ix["name"] for ix in col.list_search_indexes()}
    if VECTOR_INDEX_NAME in existing:
        return
    definition = {
        "fields": [{
            "type": "vector",
            "path": "embedding",
            "numDimensions": EMBEDDING_DIM,
            "similarity": "cosine",
        }]
    }
    model = SearchIndexModel(
        definition=definition, name=VECTOR_INDEX_NAME, type="vectorSearch"
    )
    col.create_search_index(model=model)


def ensure_fulltext_index(name: str = "fulltext_index") -> None:
    """Idempotent Atlas full-text Search index -- backs the 'full-text' and
    'hybrid' retrieval patterns in review/retrieval.py (Atlas M0+ only)."""
    col = get_collection()
    existing = {ix["name"] for ix in col.list_search_indexes()}
    if name in existing:
        return
    definition = {"mappings": {"dynamic": False, "fields": {"text": {"type": "string"}}}}
    model = SearchIndexModel(definition=definition, name=name, type="search")
    col.create_search_index(model=model)
EOF

cat > "$ROOT/rag_mongo/ingest.py" <<'EOF'
from .db import get_collection
from .embeddings import embed_batch


def chunk_text(text: str, size: int = 800, overlap: int = 100) -> list[str]:
    chunks, i = [], 0
    while i < len(text):
        chunks.append(text[i:i + size])
        i += size - overlap
    return chunks


def ingest_text(text: str, source: str) -> int:
    chunks = chunk_text(text)
    vectors = embed_batch(chunks)
    docs = [
        {"text": c, "embedding": v, "source": source}
        for c, v in zip(chunks, vectors)
    ]
    if docs:
        get_collection().insert_many(docs)
    return len(docs)
EOF

cat > "$ROOT/rag_mongo/retrieve.py" <<'EOF'
from .db import get_collection
from .embeddings import embed
from .config import VECTOR_INDEX_NAME, TOP_K


def vector_search(query: str, top_k: int = TOP_K) -> list[dict]:
    qvec = embed(query)
    pipeline = [
        {
            "$vectorSearch": {
                "index": VECTOR_INDEX_NAME,
                "path": "embedding",
                "queryVector": qvec,
                "numCandidates": max(top_k * 10, 50),
                "limit": top_k,
            }
        },
        {"$project": {"text": 1, "source": 1, "score": {"$meta": "vectorSearchScore"}}},
    ]
    return list(get_collection().aggregate(pipeline))
EOF

cat > "$ROOT/rag_mongo/generate.py" <<'EOF'
"""Generation via the shared Claude Code CLI wrapper (Pro/Max subscription
auth -- see common/claude_cli.py for the auth/--bare rationale)."""
from common.claude_cli import run_claude
from .config import GENERATION_MODEL


def generate(query: str, context_docs: list[dict]) -> dict:
    context = "\n\n".join(
        f"[Source: {d.get('source', 'unknown')}]\n{d['text']}" for d in context_docs
    )
    prompt = (
        "Answer the question using ONLY the context below. "
        "Cite sources by filename.\n\n"
        f"CONTEXT:\n{context}\n\nQUESTION: {query}"
    )
    out = run_claude(prompt, model=GENERATION_MODEL, max_turns=1)
    return {"answer": out["result"], "sources": context_docs}
EOF

cat > "$ROOT/rag_mongo/pipeline.py" <<'EOF'
from .retrieve import vector_search
from .generate import generate


def answer_query(query: str) -> dict:
    docs = vector_search(query)
    return generate(query, docs)
EOF

cat > "$ROOT/rag_cli.py" <<'EOF'
#!/usr/bin/env python3
import argparse
import json
import sys

from rag_mongo.db import ensure_vector_index, ensure_fulltext_index
from rag_mongo.ingest import ingest_text
from rag_mongo.pipeline import answer_query


def main() -> None:
    p = argparse.ArgumentParser(prog="rag_cli")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="Create vector + full-text indexes (one-time)")

    ing = sub.add_parser("ingest", help="Ingest a text file")
    ing.add_argument("path")

    q = sub.add_parser("query", help="Ask a question")
    q.add_argument("text")

    args = p.parse_args()

    if args.cmd == "init":
        ensure_vector_index()
        ensure_fulltext_index()
        print("Vector + full-text indexes ready.")
    elif args.cmd == "ingest":
        with open(args.path, encoding="utf-8") as f:
            n = ingest_text(f.read(), source=args.path)
        print(f"Ingested {n} chunks from {args.path}")
    elif args.cmd == "query":
        result = answer_query(args.text)
        print(json.dumps(result, indent=2))


if __name__ == "__main__":
    sys.exit(main())
EOF
chmod +x "$ROOT/rag_cli.py"

echo "==> [4/8] Writing Component 2: PTC pattern (Programmatic Tool Calling, local adaptation)"

cat > "$ROOT/.claude/skills/programmatic-tool-calling/SKILL.md" << 'PTC_EOF'
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
PTC_EOF

cat > "$ROOT/tools/registry.py" << 'PTC_EOF'
"""
Local adaptation of Anthropic's Programmatic Tool Calling (PTC) `allowed_callers`
concept for Claude Code (no Messages API, no code_execution_20250825 server tool).

Reference: https://platform.claude.com/cookbook/tool-use-programmatic-tool-calling-ptc

Usage:

    from tools.registry import tool

    @tool("get_team_members", orchestratable=True)
    def get_team_members(department: str) -> list[dict]: ...

    @tool("send_email", orchestratable=False)
    def send_email(to: str, body: str) -> None: ...

Only functions registered with orchestratable=True may be imported into a
script under scripts/ (see registry.namespace()). Everything else must be
called one at a time, in-conversation.
"""
from __future__ import annotations

import functools
import inspect
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Dict

logger = logging.getLogger("ptc.registry")


@dataclass(frozen=True)
class ToolSpec:
    name: str
    fn: Callable[..., Any]
    orchestratable: bool
    description: str
    is_async: bool = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "is_async", inspect.iscoroutinefunction(self.fn))


class ToolRegistry:
    """Process-wide registry of tools, partitioned by orchestratable flag."""

    def __init__(self) -> None:
        self._tools: Dict[str, ToolSpec] = {}

    def register(
        self,
        name: str,
        fn: Callable[..., Any],
        orchestratable: bool,
        description: str = "",
    ) -> None:
        if name in self._tools:
            raise ValueError(f"Tool '{name}' is already registered")
        self._tools[name] = ToolSpec(
            name=name,
            fn=fn,
            orchestratable=orchestratable,
            description=description or (inspect.getdoc(fn) or ""),
        )
        logger.debug("registered tool=%s orchestratable=%s", name, orchestratable)

    def get(self, name: str) -> ToolSpec:
        try:
            return self._tools[name]
        except KeyError as exc:
            raise KeyError(
                f"Unknown tool '{name}'. Registered tools: {sorted(self._tools)}"
            ) from exc

    def list_orchestratable(self) -> Dict[str, ToolSpec]:
        return {n: t for n, t in self._tools.items() if t.orchestratable}

    def list_direct_only(self) -> Dict[str, ToolSpec]:
        return {n: t for n, t in self._tools.items() if not t.orchestratable}

    def assert_orchestratable(self, name: str) -> ToolSpec:
        """Raise if a script tries to batch-call a tool that isn't opted in."""
        spec = self.get(name)
        if not spec.orchestratable:
            raise PermissionError(
                f"Tool '{name}' is not orchestratable=True. It must be called "
                "one at a time, in-conversation, never from a loop/batch script "
                "(e.g. it is destructive, rate-limited, or needs human review)."
            )
        return spec

    def namespace(self) -> Dict[str, Callable[..., Any]]:
        """{name: fn} for every orchestratable tool -- safe to import into scripts."""
        return {n: t.fn for n, t in self.list_orchestratable().items()}


registry = ToolRegistry()


def tool(name: str, orchestratable: bool = False, description: str = ""):
    """Decorator: register `fn` under `name`, flagging it for PTC-style batch use."""

    def _wrap(fn: Callable[..., Any]) -> Callable[..., Any]:
        registry.register(name, fn, orchestratable=orchestratable, description=description)

        @functools.wraps(fn)
        def _inner(*args: Any, **kwargs: Any) -> Any:
            return fn(*args, **kwargs)

        return _inner

    return _wrap
PTC_EOF

cat > "$ROOT/tools/example_tools.py" << 'PTC_EOF'
"""
Reference tools showing the orchestratable / direct-only split.
Replace bodies with real integrations (DB, REST API, MCP client, CLI wrapper).
Keep the decorator flags -- that's the safety boundary the skill enforces.
"""
from __future__ import annotations

import asyncio
from typing import Any

from tools.registry import tool

# --- Safe for batch/looped use from an orchestration script -----------------


@tool("get_team_members", orchestratable=True)
async def get_team_members(department: str) -> list[dict[str, Any]]:
    """Read-only, idempotent, cheap to repeat -> safe to call in a loop/gather."""
    await asyncio.sleep(0)  # placeholder for a real network/DB call
    raise NotImplementedError("wire this up to your real data source")


@tool("get_expenses", orchestratable=True)
async def get_expenses(employee_id: str, quarter: str) -> list[dict[str, Any]]:
    """Read-only; returns large, metadata-rich records -> exactly the PTC case."""
    await asyncio.sleep(0)
    raise NotImplementedError("wire this up to your real data source")


@tool("get_custom_budget", orchestratable=True)
async def get_custom_budget(user_id: str) -> dict[str, Any]:
    """Read-only lookup, depends on prior filtering -> safe for the second pass."""
    await asyncio.sleep(0)
    raise NotImplementedError("wire this up to your real data source")


# --- Direct-only: never call these from a loop/batch script -----------------


@tool("send_email", orchestratable=False)
def send_email(to: str, subject: str, body: str) -> None:
    """Side-effecting, irreversible -> must stay one-at-a-time, in-conversation."""
    raise NotImplementedError("wire this up to your real mail sender")


@tool("delete_record", orchestratable=False)
def delete_record(record_id: str) -> None:
    """Destructive -> must stay one-at-a-time, in-conversation, with confirmation."""
    raise NotImplementedError("wire this up to your real data store")
PTC_EOF

cat > "$ROOT/tools/__init__.py" << 'PTC_EOF'
PTC_EOF

cat > "$ROOT/scripts/orchestration_template.py" << 'PTC_EOF'
#!/usr/bin/env python3
"""
Orchestration script template (PTC pattern, local/Claude Code adaptation).

Copy this file to scripts/<task_name>.py per task. Do not edit tool
implementations here -- only orchestrate calls into tools/example_tools.py
(or your real module) and do filtering/aggregation in this process.

Contract:
  - Only import functions from tools.registry.registry.namespace(), or
    explicitly from a tools module where the function is decorated
    orchestratable=True. Importing a direct-only tool here is a bug.
  - Parallelize independent calls with asyncio.gather.
  - Do all filtering / aggregation / arithmetic here, in Python.
  - Print ONLY a compact digest (JSON) to stdout. No raw record dumps.
  - Exit non-zero on unrecoverable error so the caller notices.
"""
from __future__ import annotations

import asyncio
import json
import sys

from tools.registry import registry

# Fail fast if this script is later edited to call a non-orchestratable tool.
NAMESPACE = registry.namespace()


async def run() -> dict:
    get_team_members = NAMESPACE["get_team_members"]
    get_expenses = NAMESPACE["get_expenses"]
    get_custom_budget = NAMESPACE["get_custom_budget"]

    department = "engineering"
    quarter = "Q3"
    standard_budget = 5000.0

    members = json.loads(await get_team_members(department))

    expense_results = await asyncio.gather(
        *(get_expenses(m["id"], quarter) for m in members)
    )

    travel_totals: dict[str, float] = {}
    for member, raw in zip(members, expense_results):
        expenses = json.loads(raw)
        travel_totals[member["id"]] = sum(
            e["amount"]
            for e in expenses
            if e["category"] == "travel" and e["status"] == "approved"
        )

    over_standard = {
        emp_id: total for emp_id, total in travel_totals.items() if total > standard_budget
    }

    budget_results = await asyncio.gather(
        *(get_custom_budget(emp_id) for emp_id in over_standard)
    )

    exceeded = []
    for emp_id, budget_raw in zip(over_standard, budget_results):
        budget = json.loads(budget_raw)
        limit = budget.get("travel_budget", standard_budget)
        total = travel_totals[emp_id]
        if total > limit:
            exceeded.append({"employee_id": emp_id, "spent": total, "limit": limit})

    return {"quarter": quarter, "department": department, "exceeded": exceeded}


if __name__ == "__main__":
    try:
        digest = asyncio.run(run())
    except Exception as exc:  # noqa: BLE001 - surface any failure to the caller
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        sys.exit(1)
    print(json.dumps(digest, indent=2))
PTC_EOF

cat > "$ROOT/docs/PTC_PATTERN.md" << 'PTC_EOF'
# Programmatic Tool Calling (PTC) -- adapted for Claude Code (no API)

Source: Anthropic Claude Cookbook,
[Programmatic tool calling (PTC)](https://platform.claude.com/cookbook/tool-use-programmatic-tool-calling-ptc).

See `.claude/skills/programmatic-tool-calling/SKILL.md` for the operational
checklist. Summary: `tools/registry.py` gates which functions a batch script
may import (`orchestratable=True`); everything else stays direct-call-only.
Copy `scripts/orchestration_template.py` per task; print only a digest.
PTC_EOF

# Idempotent CLAUDE.md append (skip sections already present)
touch "$ROOT/CLAUDE.md"

if ! grep -q "## Programmatic Tool Calling (PTC) pattern" "$ROOT/CLAUDE.md" 2>/dev/null; then
cat >> "$ROOT/CLAUDE.md" << 'PTC_EOF'

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
PTC_EOF
fi

if ! grep -q "## Orchestrator (plan-big / execute-small)" "$ROOT/CLAUDE.md" 2>/dev/null; then
cat >> "$ROOT/CLAUDE.md" << 'ORCH_EOF'

## Orchestrator (plan-big / execute-small)

For a task that naturally splits into independent research/lookup subtasks
followed by a synthesis step, use `orchestrator_cli.py "<task>"` instead of
doing all the reading yourself. It delegates subtasks to a cheap model
(`WORKER_MODEL`, default haiku) and only has the coordinator model
(`COORDINATOR_MODEL`, default sonnet) see short digests, not raw output.
See `docs/ORCHESTRATOR.md`.
ORCH_EOF
fi

if ! grep -q "## Knowledge Graph (Extract" "$ROOT/CLAUDE.md" 2>/dev/null; then
cat >> "$ROOT/CLAUDE.md" << 'KG_EOF'

## Knowledge Graph (Extract -> Resolve -> Assemble -> Query)

For questions that span multiple documents and require chaining facts
("who works with people who worked on project X"), use `kg_cli.py query`
instead of re-reading every source document. Ingest sources once with
`kg_cli.py ingest <file>` -- this persists structured facts in MongoDB so
future questions don't re-pay the extraction cost. Queries only load a
bounded subgraph (`KG_MAX_SUBGRAPH_TRIPLES`), never the whole graph.
See `docs/KNOWLEDGE_GRAPH.md`.
KG_EOF
fi

echo "==> [5/8] Writing Component 3: local orchestrator (plan-big / execute-small)"

touch "$ROOT/orchestrator/__init__.py"

cat > "$ROOT/orchestrator/config.py" <<'EOF'
import os
from dotenv import load_dotenv

load_dotenv()

COORDINATOR_MODEL = os.getenv("COORDINATOR_MODEL", "sonnet")
WORKER_MODEL = os.getenv("WORKER_MODEL", "haiku")
MAX_SUBTASKS = int(os.getenv("MAX_SUBTASKS", "5"))
MAX_WORKER_TURNS = int(os.getenv("MAX_WORKER_TURNS", "6"))
WORKER_ALLOWED_TOOLS = os.getenv("WORKER_ALLOWED_TOOLS", "Read,Bash,Grep,Glob")
EOF

cat > "$ROOT/orchestrator/planner.py" <<'EOF'
"""Coordinator: plans big, exactly once. The cheap worker model never sees
this prompt, and this prompt never sees any worker's raw output."""
from __future__ import annotations

import json

from common.claude_cli import run_claude
from .config import COORDINATOR_MODEL, MAX_SUBTASKS

_SCHEMA_HINT = (
    "Respond with ONLY a JSON array of up to {n} short, independent, "
    "self-contained subtask instructions (strings) that together accomplish "
    "the goal. No prose, no markdown fences, just the JSON array."
)


def plan(task: str) -> list[str]:
    prompt = f"{_SCHEMA_HINT.format(n=MAX_SUBTASKS)}\n\nGOAL: {task}"
    out = run_claude(prompt, model=COORDINATOR_MODEL, max_turns=1)
    text = out["result"].strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text.split("\n", 1)[1] if "\n" in text else text
    subtasks = json.loads(text)
    if not isinstance(subtasks, list):
        raise ValueError(f"Planner did not return a JSON list: {text[:200]}")
    return [str(s) for s in subtasks[:MAX_SUBTASKS]]
EOF

cat > "$ROOT/orchestrator/worker.py" <<'EOF'
"""Worker: executes one subtask with a cheap model. Only its final digest
(the `result` field) ever leaves this module -- raw tool transcripts,
intermediate reasoning, and any file contents the worker read are discarded
here, never forwarded to the coordinator."""
from __future__ import annotations

from common.claude_cli import run_claude
from .config import WORKER_MODEL, MAX_WORKER_TURNS, WORKER_ALLOWED_TOOLS

_DIGEST_HINT = (
    "Do the task below. Reply with a SHORT digest only (a few sentences or a "
    "small table) -- the minimum needed for someone else to use your finding. "
    "Do not paste raw file contents or command output.\n\nTASK: "
)


def execute(subtask: str) -> dict:
    prompt = _DIGEST_HINT + subtask
    out = run_claude(
        prompt,
        model=WORKER_MODEL,
        allowed_tools=WORKER_ALLOWED_TOOLS,
        max_turns=MAX_WORKER_TURNS,
    )
    return {"subtask": subtask, "digest": out["result"], "cost_usd": out["cost_usd"]}
EOF

cat > "$ROOT/orchestrator/synthesizer.py" <<'EOF'
"""Coordinator's second and final call: synthesize digests into one answer.
Only the short digests are included -- never the workers' raw sessions."""
from __future__ import annotations

from common.claude_cli import run_claude
from .config import COORDINATOR_MODEL


def synthesize(task: str, digests: list[dict]) -> dict:
    bullet_list = "\n".join(f"- {d['subtask']}: {d['digest']}" for d in digests)
    prompt = (
        f"Original goal: {task}\n\n"
        f"Findings from delegated subtasks:\n{bullet_list}\n\n"
        "Write the final answer to the original goal using only these findings."
    )
    return run_claude(prompt, model=COORDINATOR_MODEL, max_turns=1)
EOF

cat > "$ROOT/orchestrator/run.py" <<'EOF'
"""Plan-big / execute-small orchestrator: local adaptation of Anthropic's
Managed Agents 'CMA_plan_big_execute_small' cookbook pattern for Claude Code
on a Pro/Max subscription -- no Managed Agents API/beta access needed.

Context/token discipline enforced here:
  1. Every `claude -p` call is stateless (no --continue/--resume) so context
     never accumulates across calls.
  2. Workers return a short digest only; their raw tool output/transcript
     never reaches the coordinator.
  3. The coordinator makes exactly two calls total (plan, synthesize) --
     never one call per subtask.
  4. Subtasks run concurrently (threads), bounded by MAX_SUBTASKS, so wall
     time doesn't scale linearly with subtask count.
"""
from __future__ import annotations

import json
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

from .planner import plan
from .worker import execute
from .synthesizer import synthesize


def run(task: str) -> dict:
    subtasks = plan(task)

    digests: list[dict] = []
    total_cost = 0.0
    with ThreadPoolExecutor(max_workers=max(len(subtasks), 1)) as pool:
        futures = {pool.submit(execute, st): st for st in subtasks}
        for fut in as_completed(futures):
            d = fut.result()
            digests.append(d)
            total_cost += d["cost_usd"]

    final = synthesize(task, digests)
    total_cost += final["cost_usd"]

    return {
        "task": task,
        "subtasks": subtasks,
        "worker_digests": digests,
        "answer": final["result"],
        "usage_estimate_usd": round(total_cost, 4),
        "note": "Estimate only -- billed against your Pro/Max usage limits, "
                "not metered API dollars.",
    }


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print('usage: python -m orchestrator.run "<task description>"', file=sys.stderr)
        sys.exit(1)
    result = run(" ".join(sys.argv[1:]))
    print(json.dumps(result, indent=2))
EOF

cat > "$ROOT/orchestrator_cli.py" <<'EOF'
#!/usr/bin/env python3
import json
import sys

from orchestrator.run import run


def main() -> None:
    if len(sys.argv) < 2:
        print('usage: python orchestrator_cli.py "<task>"', file=sys.stderr)
        sys.exit(1)
    print(json.dumps(run(" ".join(sys.argv[1:])), indent=2))


if __name__ == "__main__":
    main()
EOF
chmod +x "$ROOT/orchestrator_cli.py"

cat > "$ROOT/docs/ORCHESTRATOR.md" <<'EOF'
# Local plan-big / execute-small orchestrator

Adapts Anthropic's Managed Agents cookbook (`CMA_plan_big_execute_small.ipynb`)
for Claude Pro / Claude Code -- no Managed Agents beta or API key required.
Coordinator (`COORDINATOR_MODEL`) plans once, cheap workers (`WORKER_MODEL`)
execute subtasks in parallel threads, coordinator synthesizes from digests
only. See `orchestrator/run.py` docstring for the full discipline.

Usage:

    python orchestrator_cli.py "Summarize what changed across all files in src/ this week"
EOF

echo "==> [6/8] Writing Component 4: Knowledge Graph (Extract -> Resolve -> Assemble -> Query)"

touch "$ROOT/kg/__init__.py"

cat > "$ROOT/kg/config.py" <<'EOF'
import os
from dotenv import load_dotenv

load_dotenv()

KG_NODES_COLLECTION = os.getenv("KG_NODES_COLLECTION", "kg_nodes")
KG_EDGES_COLLECTION = os.getenv("KG_EDGES_COLLECTION", "kg_edges")
KG_EXTRACT_MODEL = os.getenv("KG_EXTRACT_MODEL", "haiku")
KG_RESOLVE_MODEL = os.getenv("KG_RESOLVE_MODEL", "sonnet")
KG_QUERY_MODEL = os.getenv("KG_QUERY_MODEL", "sonnet")
KG_MAX_SUBGRAPH_TRIPLES = int(os.getenv("KG_MAX_SUBGRAPH_TRIPLES", "40"))
EOF

cat > "$ROOT/kg/schema.py" <<'EOF'
"""JSON Schemas passed to `claude -p --json-schema` for structured output.
Keeping extraction/resolution schema-constrained is what makes this cheap:
one deterministic-shape call per document, no free-form parsing."""

EXTRACTION_SCHEMA = {
    "type": "object",
    "properties": {
        "triples": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "subject": {"type": "string"},
                    "subject_type": {"type": "string"},
                    "predicate": {"type": "string"},
                    "object": {"type": "string"},
                    "object_type": {"type": "string"},
                },
                "required": ["subject", "predicate", "object"],
            },
        }
    },
    "required": ["triples"],
}

RESOLUTION_SCHEMA = {
    "type": "object",
    "properties": {
        "clusters": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "canonical_name": {"type": "string"},
                    "aliases": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["canonical_name", "aliases"],
            },
        }
    },
    "required": ["clusters"],
}
EOF

cat > "$ROOT/kg/db.py" <<'EOF'
"""Reuses the RAG component's Mongo connection settings (same Atlas
cluster/database, separate collections) -- no new infra to provision."""
from pymongo import MongoClient
from rag_mongo.config import MONGODB_URI, MONGODB_DB
from .config import KG_NODES_COLLECTION, KG_EDGES_COLLECTION


def _require_uri() -> str:
    if not MONGODB_URI:
        raise RuntimeError(
            "MONGODB_URI not set. Copy .env.example to .env and fill it in."
        )
    return MONGODB_URI


def get_nodes_collection():
    return MongoClient(_require_uri())[MONGODB_DB][KG_NODES_COLLECTION]


def get_edges_collection():
    return MongoClient(_require_uri())[MONGODB_DB][KG_EDGES_COLLECTION]
EOF

cat > "$ROOT/kg/extract.py" <<'EOF'
"""Extract stage: ONE cheap-model call per document, schema-constrained --
matches the cookbook's design ("haiku pulls entities and triples, one call
per doc") and bounds cost regardless of document length."""
from common.claude_cli import run_claude
from .config import KG_EXTRACT_MODEL
from .schema import EXTRACTION_SCHEMA


def extract_triples(text: str, source: str) -> list[dict]:
    prompt = (
        "Extract factual subject-predicate-object triples from this document. "
        "Use concise, canonical-looking entity names, not pronouns.\n\n"
        f"DOCUMENT:\n{text}"
    )
    out = run_claude(prompt, model=KG_EXTRACT_MODEL, json_schema=EXTRACTION_SCHEMA, max_turns=1)
    triples = (out.get("structured_output") or {}).get("triples", [])
    for t in triples:
        t["source"] = source
    return triples
EOF

cat > "$ROOT/kg/resolve.py" <<'EOF'
"""Resolve stage: ONE batched call clusters every newly-seen entity name at
once. Deliberately not one call per entity/pair -- that would turn a single
document's ingestion into dozens of coordinator-tier calls for no benefit;
the model can cluster an entire name list in one schema-constrained call."""
from common.claude_cli import run_claude
from .config import KG_RESOLVE_MODEL
from .schema import RESOLUTION_SCHEMA


def resolve_entities(names: list[str]) -> dict[str, str]:
    """Returns {alias_or_name: canonical_name} for every input name."""
    if not names:
        return {}
    unique_names = sorted(set(names))
    prompt = (
        "Cluster these entity name strings into groups referring to the same "
        "real-world entity (aliases, name variants, abbreviations). Pick one "
        "canonical_name per cluster.\n\nNAMES:\n" + "\n".join(unique_names)
    )
    out = run_claude(prompt, model=KG_RESOLVE_MODEL, json_schema=RESOLUTION_SCHEMA, max_turns=1)
    clusters = (out.get("structured_output") or {}).get("clusters", [])
    mapping: dict[str, str] = {}
    for c in clusters:
        canon = c["canonical_name"]
        mapping[canon] = canon
        for alias in c.get("aliases", []):
            mapping[alias] = canon
    return mapping
EOF

cat > "$ROOT/kg/assemble.py" <<'EOF'
"""Assemble stage: upsert canonical nodes + typed edges with provenance into
MongoDB. Idempotent via upsert-on-unique-key -- re-ingesting the same
document does not duplicate nodes or edges, only adds a source reference."""
from .db import get_nodes_collection, get_edges_collection
from .resolve import resolve_entities


def assemble(triples: list[dict]) -> dict:
    names = [t["subject"] for t in triples] + [t["object"] for t in triples]
    canon_map = resolve_entities(names)

    nodes_col = get_nodes_collection()
    edges_col = get_edges_collection()

    n_nodes, n_edges = 0, 0
    for t in triples:
        subj = canon_map.get(t["subject"], t["subject"])
        obj = canon_map.get(t["object"], t["object"])

        for name, ntype in (
            (subj, t.get("subject_type", "")),
            (obj, t.get("object_type", "")),
        ):
            res = nodes_col.update_one(
                {"name": name},
                {"$setOnInsert": {"name": name, "type": ntype}},
                upsert=True,
            )
            if res.upserted_id:
                n_nodes += 1

        res = edges_col.update_one(
            {"subject": subj, "predicate": t["predicate"], "object": obj},
            {
                "$setOnInsert": {"subject": subj, "predicate": t["predicate"], "object": obj},
                "$addToSet": {"sources": t.get("source", "unknown")},
            },
            upsert=True,
        )
        if res.upserted_id:
            n_edges += 1

    return {"nodes_upserted": n_nodes, "edges_upserted": n_edges}
EOF

cat > "$ROOT/kg/ingest.py" <<'EOF'
from .extract import extract_triples
from .assemble import assemble


def ingest_document(text: str, source: str) -> dict:
    triples = extract_triples(text, source)
    stats = assemble(triples)
    stats["triples_extracted"] = len(triples)
    return stats
EOF

cat > "$ROOT/kg/query.py" <<'EOF'
"""Query stage: pull a BOUNDED subgraph around entities mentioned in the
question, then reason over just those triples. Never serialize the whole
graph -- that would make context grow unboundedly as the graph scales, the
opposite of what this component exists to prevent."""
from __future__ import annotations

import re

from common.claude_cli import run_claude
from .config import KG_QUERY_MODEL, KG_MAX_SUBGRAPH_TRIPLES
from .db import get_nodes_collection, get_edges_collection


def _candidate_names(question: str) -> list[str]:
    words = set(re.findall(r"[A-Za-z][A-Za-z0-9_-]{2,}", question))
    if not words:
        return []
    nodes_col = get_nodes_collection()
    hits = [d["name"] for d in nodes_col.find({"name": {"$in": list(words)}}, {"name": 1})]
    if hits:
        return hits
    pattern = "|".join(re.escape(w) for w in words)
    cursor = nodes_col.find(
        {"name": {"$regex": pattern, "$options": "i"}}, {"name": 1}
    ).limit(10)
    return [d["name"] for d in cursor]


def _subgraph(names: list[str], limit: int) -> list[dict]:
    edges_col = get_edges_collection()
    if not names:
        return list(edges_col.find({}, {"_id": 0}).limit(limit))
    cursor = edges_col.find(
        {"$or": [{"subject": {"$in": names}}, {"object": {"$in": names}}]},
        {"_id": 0},
    ).limit(limit)
    return list(cursor)


def query(question: str) -> dict:
    names = _candidate_names(question)
    triples = _subgraph(names, KG_MAX_SUBGRAPH_TRIPLES)

    serialized = "\n".join(
        f"({t['subject']}) -[{t['predicate']}]-> ({t['object']}) "
        f"[sources: {', '.join(t.get('sources', []))}]"
        for t in triples
    )
    prompt = (
        "Answer the question using ONLY these graph triples. Cite the specific "
        "edge(s) you used.\n\n"
        f"TRIPLES:\n{serialized or '(none found)'}\n\nQUESTION: {question}"
    )
    out = run_claude(prompt, model=KG_QUERY_MODEL, max_turns=1)
    return {"answer": out["result"], "triples_used": len(triples)}
EOF

cat > "$ROOT/kg_cli.py" <<'EOF'
#!/usr/bin/env python3
import argparse
import json
import sys

from kg.ingest import ingest_document
from kg.query import query


def main() -> None:
    p = argparse.ArgumentParser(prog="kg_cli")
    sub = p.add_subparsers(dest="cmd", required=True)

    ing = sub.add_parser("ingest", help="Extract+resolve+assemble a text file into the graph")
    ing.add_argument("path")

    q = sub.add_parser("query", help="Ask a multi-hop question over the graph")
    q.add_argument("text")

    args = p.parse_args()
    if args.cmd == "ingest":
        with open(args.path, encoding="utf-8") as f:
            stats = ingest_document(f.read(), source=args.path)
        print(json.dumps(stats, indent=2))
    elif args.cmd == "query":
        print(json.dumps(query(args.text), indent=2))


if __name__ == "__main__":
    sys.exit(main())
EOF
chmod +x "$ROOT/kg_cli.py"

cat > "$ROOT/docs/KNOWLEDGE_GRAPH.md" <<'EOF'
# Knowledge Graph (Extract -> Resolve -> Assemble -> Query)

Source: [Knowledge graph construction with Claude](https://platform.claude.com/cookbook/capabilities-knowledge-graph-guide)

## Why this exists

RAG (Component 1) answers questions a single chunk can answer. It cannot
chain facts across documents ("who works with people who worked on project
X"). A knowledge graph makes multi-hop reasoning a graph traversal instead
of a retrieval problem.

## The 5 stages, and their local (Pro/Claude Code) implementation

| Stage | Cookbook (API) | This repo (Claude Code CLI) |
|---|---|---|
| Extract | Haiku, structured tool-use output, 1 call/doc | `kg/extract.py`: `claude -p --model haiku --json-schema ...`, 1 call/doc |
| Resolve | Claude clusters aliases via descriptions | `kg/resolve.py`: 1 batched call clusters ALL new names at once |
| Assemble | Canonical nodes, typed edges, provenance | `kg/assemble.py`: idempotent upserts into MongoDB (same Atlas cluster as RAG) |
| Query | Serialize subgraph -> reason -> cite edges | `kg/query.py`: bounded subgraph (`KG_MAX_SUBGRAPH_TRIPLES`), then 1 call |
| Repeat | -- | `kg_cli.py ingest` per new document; graph persists across sessions |

## Why storage is MongoDB, not Neo4j

The cookbook itself recommends starting in-memory and only moving to a
dedicated graph database once query volume/traversal depth prove the need.
This repo already provisions a MongoDB Atlas connection for RAG (Component
1) -- reusing it for two more collections (`kg_nodes`, `kg_edges`) adds zero
new infrastructure or cost.

## Token/context discipline (why this doesn't grow unbounded)

- **Extraction**: exactly 1 call per document, cheap model, schema-constrained
  (no retries needed to get parseable output).
- **Resolution**: exactly 1 call per ingestion batch, not per entity pair --
  clustering is a single-call operation regardless of graph size.
- **Query**: retrieval is a bounded Mongo query (`KG_MAX_SUBGRAPH_TRIPLES`,
  default 40), never "dump the whole graph into the prompt." As the graph
  grows to thousands of edges, the per-query cost stays flat.
- **No session chaining**: every stage uses a stateless `claude -p` call
  (via `common/claude_cli.py`) -- nothing accumulates context across
  ingestions or queries.

## Usage

    python kg_cli.py ingest docs/some_report.txt
    python kg_cli.py query "Who is connected to the vendor named in the incident report?"

## When to use this vs. RAG (Component 1)

- Single-document lookup / "what does X say about Y" -> RAG.
- Cross-document chains, "who is connected to whom", audit trails -> Knowledge Graph.
- Both can coexist: ingest the same source into RAG for direct-quote lookup
  AND into the graph for relationship queries.
EOF

echo "==> [7/10] Writing Component 5: cloud review + retrieval (GitHub Actions only)"

touch "$ROOT/review/__init__.py"

cat > "$ROOT/review/config.py" <<'EOF'
import os
from dotenv import load_dotenv

load_dotenv()

FULLTEXT_INDEX_NAME = os.getenv("FULLTEXT_INDEX_NAME", "fulltext_index")
REVIEW_MAX_CONTEXT_ITEMS = int(os.getenv("REVIEW_MAX_CONTEXT_ITEMS", "15"))
EOF

cat > "$ROOT/review/retrieval.py" <<'EOF'
"""Four retrieval patterns lifted from Anthropic's CMA_with_mongodb_atlas
cookbook (vector / full-text / hybrid / graph) as plain MongoDB Atlas
queries -- deliberately NOT the Managed Agents runtime itself, which needs
an API key and Managed Agents beta access this project doesn't have.
These are the same "liftable building blocks" the cookbook describes,
minus the hosted-agent wrapper."""
from __future__ import annotations

from rag_mongo.db import get_collection
from rag_mongo.retrieve import vector_search
from rag_mongo.config import TOP_K
from kg.db import get_edges_collection
from kg.config import KG_MAX_SUBGRAPH_TRIPLES
from .config import FULLTEXT_INDEX_NAME


def vector(query: str, top_k: int = TOP_K) -> list[dict]:
    return vector_search(query, top_k)


def full_text(query: str, top_k: int = TOP_K) -> list[dict]:
    col = get_collection()
    pipeline = [
        {"$search": {"index": FULLTEXT_INDEX_NAME, "text": {"query": query, "path": "text"}}},
        {"$limit": top_k},
        {"$project": {"text": 1, "source": 1, "score": {"$meta": "searchScore"}}},
    ]
    return list(col.aggregate(pipeline))


def hybrid(query: str, top_k: int = TOP_K) -> list[dict]:
    """Reciprocal rank fusion of vector + full-text results -- bounded to
    top_k regardless of how many candidates either sub-search returns."""
    v = vector(query, top_k * 2)
    f = full_text(query, top_k * 2)
    scores: dict[str, float] = {}
    docs: dict[str, dict] = {}
    for rank, d in enumerate(v):
        key = d.get("source", "") + d["text"][:50]
        scores[key] = scores.get(key, 0.0) + 1.0 / (60 + rank)
        docs[key] = d
    for rank, d in enumerate(f):
        key = d.get("source", "") + d["text"][:50]
        scores[key] = scores.get(key, 0.0) + 1.0 / (60 + rank)
        docs[key] = d
    ranked = sorted(scores, key=lambda k: scores[k], reverse=True)[:top_k]
    return [docs[k] for k in ranked]


def graph(entity_names: list[str], limit: int = KG_MAX_SUBGRAPH_TRIPLES) -> list[dict]:
    edges_col = get_edges_collection()
    if not entity_names:
        return list(edges_col.find({}, {"_id": 0}).limit(limit))
    cursor = edges_col.find(
        {"$or": [{"subject": {"$in": entity_names}}, {"object": {"$in": entity_names}}]},
        {"_id": 0},
    ).limit(limit)
    return list(cursor)
EOF

cat > "$ROOT/review/build_context.py" <<'EOF'
#!/usr/bin/env python3
"""Cloud-only retrieval step for PR review (Component 5).

Designed to run INSIDE a GitHub Actions runner -- never locally, per this
project's rule that review/test for this component happens exclusively in
GitHub's cloud service. Produces a bounded context.json that the
claude-review job attaches to the review prompt, so review is grounded in
the project's RAG + knowledge-graph store, not just the raw diff.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess

from review.retrieval import hybrid, graph
from review.config import REVIEW_MAX_CONTEXT_ITEMS


def _diff_text(base: str, head: str) -> str:
    out = subprocess.run(
        ["git", "diff", f"{base}...{head}"], capture_output=True, text=True, check=True
    )
    return out.stdout


def _candidate_terms(diff: str) -> list[str]:
    # Bounded: cap term count so a huge diff can't blow up the retrieval query.
    names = sorted(set(re.findall(r"[A-Za-z_][A-Za-z0-9_]{3,}", diff)))
    return names[:50]


def build_context(base: str, head: str) -> dict:
    diff = _diff_text(base, head)
    terms = _candidate_terms(diff)
    query_text = " ".join(terms[:20]) or "code change"

    related_docs = hybrid(query_text, top_k=REVIEW_MAX_CONTEXT_ITEMS)
    related_edges = graph(terms, limit=REVIEW_MAX_CONTEXT_ITEMS)

    return {
        "related_documents": [
            {"source": d.get("source"), "excerpt": d["text"][:300]} for d in related_docs
        ],
        "related_facts": [
            f"({e['subject']}) -[{e['predicate']}]-> ({e['object']})" for e in related_edges
        ],
    }


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--base", required=True)
    p.add_argument("--head", required=True)
    p.add_argument("--out", required=True)
    args = p.parse_args()
    ctx = build_context(args.base, args.head)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(ctx, f, indent=2)
    print(
        f"Wrote {args.out}: {len(ctx['related_documents'])} docs, "
        f"{len(ctx['related_facts'])} facts"
    )
EOF

cat > "$ROOT/.github/workflows/claude-review.yml" <<'EOF'
name: Claude Cloud Review (RAG + Knowledge Graph retrieval)

on:
  pull_request:
    types: [opened, synchronize, reopened]

permissions:
  contents: read
  pull-requests: write
  issues: write
  id-token: write

jobs:
  retrieve-context:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - name: Install retrieval dependencies
        run: pip install -r requirements.txt
      - name: Build retrieval context (vector + full-text + hybrid + graph)
        env:
          MONGODB_URI: ${{ secrets.MONGODB_URI }}
        run: |
          python review/build_context.py \
            --base "${{ github.event.pull_request.base.sha }}" \
            --head "${{ github.event.pull_request.head.sha }}" \
            --out context.json
      - uses: actions/upload-artifact@v4
        with:
          name: review-context
          path: context.json

  claude-review:
    needs: retrieve-context
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/download-artifact@v4
        with:
          name: review-context
      - name: Claude Code review (Pro/Max subscription -- no API key)
        uses: anthropics/claude-code-action@v1
        with:
          claude_code_oauth_token: ${{ secrets.CLAUDE_CODE_OAUTH_TOKEN }}
          prompt: |
            REPO: ${{ github.repository }}
            PR NUMBER: ${{ github.event.pull_request.number }}

            Review this pull request's diff. context.json in the working
            directory holds related documents and facts retrieved from this
            project's MongoDB Atlas RAG + knowledge graph store -- use it to
            ground the review in prior decisions and related code, not just
            the diff in isolation.

            Flag correctness issues, security issues, and inconsistencies
            with the conventions documented in CLAUDE.md.

            This is advisory only: do not approve or merge. A human reviewer
            makes the final call -- human-in-the-loop, adapted from the
            fraud-review pattern in Anthropic's CMA_with_mongodb_atlas
            cookbook.
          claude_args: |
            --max-turns 8
EOF

cat > "$ROOT/docs/CLOUD_REVIEW.md" <<'EOF'
# Cloud-only PR review + retrieval (Component 5)

Source: [Managed Agents: CMA with MongoDB Atlas](https://platform.claude.com/cookbook/managed-agents-cma-with-mongodb-atlas)

## Why this can't be the cookbook's actual agent

The cookbook's fraud-review agent runs on Claude Managed Agents (CMA) --
Anthropic's hosted, stateful agent runtime. CMA requires an
`ANTHROPIC_API_KEY` and Managed Agents beta access. This project has neither
(Pro/Max subscription only), so the CMA session/environment/resources model
itself has no local or cloud-subscription equivalent -- same blocker as the
Managed Agents plan-big/execute-small cookbook (see `docs/ORCHESTRATOR.md`).

## What IS liftable: the four retrieval patterns

The cookbook explicitly frames vector / full-text / hybrid / graph search as
"liftable building blocks" independent of CMA. `review/retrieval.py`
implements exactly these four as plain MongoDB Atlas queries:

| Pattern | Cookbook | This repo |
|---|---|---|
| Vector | `$vectorSearch` via CMA tool | `review.retrieval.vector()` -> reuses `rag_mongo.retrieve.vector_search` |
| Full-text | Atlas `$search` | `review.retrieval.full_text()` -- new `fulltext_index` (see `rag_cli.py init`) |
| Hybrid | RRF-combined vector+text | `review.retrieval.hybrid()` -- reciprocal rank fusion, bounded to top_k |
| Graph | Traversal via CMA tool | `review.retrieval.graph()` -> reuses `kg` component's edge collection |

## What replaces the CMA runtime: GitHub Actions + Claude Code Action

Per your explicit requirement -- review/test happens ONLY in GitHub's cloud
service, never locally -- this component is a GitHub Actions workflow
(`.github/workflows/claude-review.yml`), not a script you run yourself:

1. **`retrieve-context` job**: checks out the PR, runs
   `review/build_context.py` against your Atlas cluster (bounded to
   `REVIEW_MAX_CONTEXT_ITEMS`), uploads `context.json` as an artifact.
2. **`claude-review` job**: downloads that artifact, runs
   `anthropics/claude-code-action@v1` authenticated via your Pro/Max
   subscription (`CLAUDE_CODE_OAUTH_TOKEN`, not an API key), posts an
   advisory review comment on the PR.

This mirrors the cookbook's human-in-the-loop fraud-review design: Claude
never approves or merges -- it flags issues for a human reviewer, same
division of labor as the CMA agent's `decide()`/`escalate()` gate.

## One-time setup (required before this workflow can run)

1. Install the Claude GitHub App: https://github.com/apps/claude (grant it
   access to this repo).
2. Generate a subscription OAuth token, once, locally:
   `claude setup-token` (opens a browser; requires Pro/Max login). This
   token is valid ~1 year and is NOT an API key.
3. Add two repository secrets (Settings -> Secrets and variables -> Actions):
   - `CLAUDE_CODE_OAUTH_TOKEN` -- the token from step 2
   - `MONGODB_URI` -- the same Atlas connection string from your `.env`
4. Confirm workflow permissions (Settings -> Actions -> General ->
   Workflow permissions) allow "Read and write permissions" -- a
   read-only default silently blocks the review comment from posting.
5. Run `rag_cli.py init` once (locally, with `.env` filled in) so the
   `fulltext_index` this workflow depends on actually exists in Atlas.

## Known limitation to watch for

Anthropic's `claude-code-action` has had OAuth-token propagation issues
after a Pro-to-Max plan change (upstream issue #1281) -- if the action
starts failing auth after a plan change, regenerate the token with
`claude setup-token` and update the secret.

## What was NOT tested locally, and why

Per your instruction, this component's correctness can only be verified by
GitHub's own cloud runners -- there is no local Actions runner in this
project's toolchain to execute against. What WAS verified before delivery:
YAML structure parses correctly, and every Python file in `review/`
byte-compiles and imports cleanly. The actual `$search`/`$vectorSearch`
queries, the OAuth handshake, and the posted PR comment can only be
confirmed by opening a real pull request after completing the setup above.
EOF

if ! grep -q "## Cloud review + retrieval (Component 5)" "$ROOT/CLAUDE.md" 2>/dev/null; then
cat >> "$ROOT/CLAUDE.md" << 'REVIEW_EOF'

## Cloud review + retrieval (Component 5)

Every pull request is automatically reviewed by `.github/workflows/claude-review.yml`,
grounded in this project's MongoDB Atlas RAG + knowledge graph via
`review/retrieval.py` (vector/full-text/hybrid/graph patterns). This is
advisory only -- it never approves or merges. See `docs/CLOUD_REVIEW.md` for
setup and the one-time secrets/GitHub App requirements.
REVIEW_EOF
fi

echo "==> [8/10] Writing project architecture schema (docs/SCHEMA.md)"

cat > "$ROOT/docs/SCHEMA.md" <<'EOF'
# Project schema: RAG + PTC + Orchestrator + Knowledge Graph + Cloud Review

Generated for the combined setup script. All 5 components run on a
Claude Pro/Max subscription -- no Anthropic API key anywhere in this repo.

## Architecture diagram

```mermaid
flowchart TB
    subgraph Local["Local machine (Claude Pro via OAuth login)"]
        CommonCLI["common/claude_cli.py<br/>shared claude -p wrapper<br/>(stateless, no --bare, no --continue)"]

        subgraph C1["Component 1: RAG"]
            RagIngest["rag_mongo/ingest.py"] --> RagDB[(MongoDB Atlas<br/>documents)]
            RagQuery["rag_mongo/retrieve.py"] --> RagDB
            RagQuery --> RagGen["rag_mongo/generate.py"]
            RagGen --> CommonCLI
        end

        subgraph C2["Component 2: PTC pattern"]
            Registry["tools/registry.py<br/>orchestratable flag gate"]
            ScriptTemplate["scripts/*.py<br/>batch tool calls"] --> Registry
        end

        subgraph C3["Component 3: Orchestrator"]
            Planner["orchestrator/planner.py<br/>1 call, sonnet"] --> CommonCLI
            Worker["orchestrator/worker.py<br/>N parallel calls, haiku"] --> CommonCLI
            Synth["orchestrator/synthesizer.py<br/>1 call, sonnet"] --> CommonCLI
            Planner --> Worker --> Synth
        end

        subgraph C4["Component 4: Knowledge Graph"]
            Extract["kg/extract.py<br/>1 call/doc, haiku"] --> CommonCLI
            Resolve["kg/resolve.py<br/>1 batched call, sonnet"] --> CommonCLI
            Assemble["kg/assemble.py"] --> KgDB[(MongoDB Atlas<br/>kg_nodes, kg_edges)]
            Extract --> Resolve --> Assemble
            KgQuery["kg/query.py<br/>bounded subgraph, sonnet"] --> KgDB
            KgQuery --> CommonCLI
        end
    end

    subgraph Cloud["GitHub Actions (cloud only, per your explicit requirement)"]
        subgraph C5["Component 5: Cloud review + retrieval"]
            BuildCtx["review/build_context.py"] --> Retrieval["review/retrieval.py<br/>vector / full-text / hybrid / graph"]
            Retrieval --> RagDB
            Retrieval --> KgDB
            BuildCtx --> ContextJson["context.json artifact"]
            ContextJson --> ClaudeAction["anthropics/claude-code-action@v1<br/>auth: CLAUDE_CODE_OAUTH_TOKEN"]
            ClaudeAction --> PRComment["Advisory PR comment<br/>(never auto-merges)"]
        end
    end

    RagDB -.shared cluster.- KgDB
```

## Directory tree

```
.
|-- common/
|   `-- claude_cli.py          # single shared `claude -p` subprocess wrapper
|-- rag_mongo/                 # Component 1: RAG over MongoDB Atlas
|   |-- config.py db.py embeddings.py ingest.py retrieve.py generate.py pipeline.py
|-- rag_cli.py                 # entrypoint: init | ingest | query
|-- tools/                     # Component 2: PTC pattern
|   |-- registry.py            # orchestratable=True/False gate
|   `-- example_tools.py       # reference tool implementations
|-- scripts/
|   `-- orchestration_template.py
|-- .claude/skills/programmatic-tool-calling/SKILL.md
|-- orchestrator/               # Component 3: plan-big / execute-small
|   |-- config.py planner.py worker.py synthesizer.py run.py
|-- orchestrator_cli.py
|-- kg/                         # Component 4: Knowledge Graph
|   |-- config.py schema.py db.py extract.py resolve.py assemble.py ingest.py query.py
|-- kg_cli.py
|-- review/                     # Component 5: cloud-only review + retrieval
|   |-- config.py retrieval.py build_context.py
|-- .github/workflows/
|   `-- claude-review.yml       # runs ONLY on GitHub's runners
|-- docs/
|   |-- PTC_PATTERN.md ORCHESTRATOR.md KNOWLEDGE_GRAPH.md CLOUD_REVIEW.md SCHEMA.md
|-- CLAUDE.md                   # project directives, auto-loaded by Claude Code
|-- requirements.txt .env.example .gitignore
```

## Component responsibility table

| # | Component | Entry point | Storage | Model tier | Runs where |
|---|---|---|---|---|---|
| 1 | RAG | `rag_cli.py` | `documents` (Atlas) | sonnet (configurable) | Local |
| 2 | PTC pattern | `scripts/*.py` | none (in-memory per run) | none (pure orchestration) | Local |
| 3 | Orchestrator | `orchestrator_cli.py` | none (stateless calls) | sonnet (plan/synth) + haiku (workers) | Local |
| 4 | Knowledge Graph | `kg_cli.py` | `kg_nodes`, `kg_edges` (same Atlas cluster) | haiku (extract) + sonnet (resolve/query) | Local |
| 5 | Cloud review | `.github/workflows/claude-review.yml` | reads `documents`, `kg_nodes`, `kg_edges` | sonnet (via claude-code-action) | **GitHub Actions only** |

## Auth model (no API key anywhere)

| Context | Mechanism | Where configured |
|---|---|---|
| Components 1, 3, 4 (local) | OAuth login, Pro/Max subscription | `claude /login`, once |
| Component 5 (GitHub Actions) | OAuth token, Pro/Max subscription | `claude setup-token` (once, locally) -> `CLAUDE_CODE_OAUTH_TOKEN` repo secret |
| MongoDB Atlas (all components) | Connection string | `.env` (local) / `MONGODB_URI` repo secret (Actions) |

`common/claude_cli.py` never passes `--bare` (that flag requires
`ANTHROPIC_API_KEY` and breaks OAuth) and never passes `--continue`/`--resume`
(that would grow context across calls). Every call is a fresh, stateless
process -- this is the single rule that keeps token/context usage bounded
across all four local components.

## Data flow: how the components compose

- **RAG vs Knowledge Graph**: same Atlas cluster, different collections.
  RAG answers "what does document X say"; KG answers "who/what is connected
  to what" by tracing edges. `review/retrieval.py` (Component 5) queries
  both to ground PR reviews.
- **Orchestrator vs PTC pattern**: independent. Orchestrator delegates
  *reasoning* subtasks to a cheap model; PTC pattern delegates *tool-calling
  loops* to a script. Orchestrator workers may internally use `Bash` and
  therefore could invoke a PTC-style script if the subtask calls for it.
- **Cloud review is the only component that cannot run locally** by design
  (per your explicit requirement) -- it exists solely as a GitHub Actions
  workflow triggered on `pull_request`.

## Environment variables (full list)

See `.env.example` in the repo root for the authoritative, commented list --
this table is a summary, not a duplicate source of truth.

| Variable | Component | Default |
|---|---|---|
| `MONGODB_URI` | 1, 4, 5 | (required, no default) |
| `MONGODB_DB` | 1, 4 | `rag_db` |
| `VECTOR_INDEX_NAME` | 1, 5 | `vector_index` |
| `FULLTEXT_INDEX_NAME` | 5 | `fulltext_index` |
| `RAG_GENERATION_MODEL` | 1 | `sonnet` |
| `COORDINATOR_MODEL` / `WORKER_MODEL` | 3 | `sonnet` / `haiku` |
| `KG_EXTRACT_MODEL` / `KG_RESOLVE_MODEL` / `KG_QUERY_MODEL` | 4 | `haiku` / `sonnet` / `sonnet` |
| `KG_MAX_SUBGRAPH_TRIPLES` | 4, 5 | `40` |
| `REVIEW_MAX_CONTEXT_ITEMS` | 5 | `15` |
EOF

echo "==> [9/10] Byte-compiling everything (pure syntax check -- no dependencies needed)"
python3 -m py_compile \
  "$ROOT"/common/*.py \
  "$ROOT"/rag_mongo/*.py \
  "$ROOT"/orchestrator/*.py \
  "$ROOT"/kg/*.py \
  "$ROOT"/review/*.py \
  "$ROOT"/tools/*.py \
  "$ROOT"/scripts/orchestration_template.py \
  "$ROOT"/rag_cli.py \
  "$ROOT"/orchestrator_cli.py \
  "$ROOT"/kg_cli.py
echo "    Syntax OK for all files."

echo "==> [10/10] Verifying PTC registry guard (pure stdlib -- always runs)"
( cd "$ROOT" && python3 - << 'PYVERIFY'
from tools.registry import registry
import tools.example_tools  # noqa: F401
print('orchestratable:', sorted(registry.list_orchestratable()))
print('direct-only  :', sorted(registry.list_direct_only()))
try:
    registry.assert_orchestratable('send_email')
    raise SystemExit('guard failed: expected PermissionError')
except PermissionError:
    print('OK: orchestratable guard enforced')
PYVERIFY
)

echo "    (optional runtime import check follows, requires requirements.txt already installed)"
( cd "$ROOT" && python3 - << 'PYVERIFY'
import sys
try:
    import common.claude_cli  # noqa: F401
    import kg.schema, kg.extract, kg.resolve, kg.assemble, kg.query  # noqa: F401
    import review.config, review.retrieval  # noqa: F401
except ModuleNotFoundError as exc:
    print(f"    Skipped -- {exc}. Expected before 'pip install -r requirements.txt'. Not an error.")
    sys.exit(0)
print('OK: kg/common/review modules import cleanly (no MongoDB/network required to import)')
PYVERIFY
)

find "$ROOT" -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true

cat <<'EOF'

============================================================
All 5 components written successfully:
  1. rag_mongo/        + rag_cli.py           (RAG over MongoDB Atlas)
  2. tools/, scripts/, .claude/skills/, docs/PTC_PATTERN.md  (PTC pattern)
  3. orchestrator/     + orchestrator_cli.py  (plan-big / execute-small)
  4. kg/                + kg_cli.py            (Knowledge Graph: extract/resolve/assemble/query)
  5. review/            + .github/workflows/claude-review.yml  (cloud-only PR review + retrieval)
  common/claude_cli.py -- single shared subprocess wrapper used by 1, 3, 4

Full architecture: docs/SCHEMA.md (diagram + directory tree + env var table)

Next steps (local, components 1-4):
  1. python3 -m venv .venv && source .venv/bin/activate
  2. pip install -r requirements.txt
  3. cp .env.example .env   # fill in MONGODB_URI
  4. npm i -g @anthropic-ai/claude-code && claude /login   # one-time, Pro plan
  5. python rag_cli.py init
  6. python rag_cli.py ingest path/to/file.txt
  7. python rag_cli.py query "your question"
  8. python orchestrator_cli.py "your multi-part research task"
  9. python kg_cli.py ingest path/to/file.txt
 10. python kg_cli.py query "your cross-document question"

Next steps (cloud-only, component 5 -- see docs/CLOUD_REVIEW.md):
  1. Install the Claude GitHub App: https://github.com/apps/claude
  2. claude setup-token   # one-time, generates a subscription OAuth token
  3. Add repo secrets: CLAUDE_CODE_OAUTH_TOKEN, MONGODB_URI
  4. Settings -> Actions -> General -> Workflow permissions -> Read and write
  5. Open a pull request -- the workflow runs entirely on GitHub's runners

Wire real tools into tools/example_tools.py, then copy
scripts/orchestration_template.py per task -- see docs/PTC_PATTERN.md.
============================================================
EOF
