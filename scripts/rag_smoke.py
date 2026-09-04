#!/usr/bin/env python3
"""End-to-end smoke check: Atlas connectivity + all four retrieval patterns."""

from __future__ import annotations

import sys
from pathlib import Path

# `python scripts/x.py` puts scripts/ on sys.path, not the repo root, so the
# first-party imports below are not resolvable on their own -- the RAG ingest
# workflow failed on exactly that (ModuleNotFoundError: rag_mongo). Anchoring
# on __file__ rather than the cwd keeps it correct from any directory.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kg.db import get_edges_collection, get_nodes_collection  # noqa: E402
from rag_mongo.db import get_collection  # noqa: E402
from review import retrieval  # noqa: E402

QUERIES = [
    "vector search retrieval",
    "how are risk gates evaluated",
    "order execution router fees",
]


def main() -> None:
    col = get_collection()
    total = col.count_documents({})
    sources = len(col.distinct("source"))
    missing_embed = col.count_documents({"embedding": {"$exists": False}})
    print(f"documents: {total} chunks / {sources} sources / {missing_embed} missing embeddings")
    print(
        f"kg_nodes: {get_nodes_collection().count_documents({})}  "
        f"kg_edges: {get_edges_collection().count_documents({})}"
    )
    print(
        "search indexes: "
        + ", ".join(
            f"{ix['name']}({ix['type']},{ix['status']},queryable={ix.get('queryable')})"
            for ix in col.list_search_indexes()
        )
    )

    failures = []
    for q in QUERIES:
        for name in ("vector", "full_text", "hybrid"):
            try:
                hits = getattr(retrieval, name)(q)
            except Exception as exc:  # noqa: BLE001
                failures.append(f"{name}({q!r}): {type(exc).__name__}: {exc}")
                continue
            if not hits:
                failures.append(f"{name}({q!r}): 0 hits")
                continue
            top = hits[0]
            score = f" ({top['score']:.3f})" if "score" in top else ""
            print(f"{name:<10} {q!r:<42} -> {top.get('source')}{score}")

    try:
        edges = retrieval.graph([])
        print(f"graph        (unfiltered)                     -> {len(edges)} edges")
        if not edges:
            failures.append("graph([]): 0 edges")
    except Exception as exc:  # noqa: BLE001
        failures.append(f"graph: {type(exc).__name__}: {exc}")

    if failures:
        print("\nFAILURES:")
        for f in failures:
            print("  - " + f)
        raise SystemExit(1)
    print("\nALL RETRIEVAL PATHS OK")


if __name__ == "__main__":
    main()
