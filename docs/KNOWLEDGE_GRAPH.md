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
