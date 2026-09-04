#!/usr/bin/env python3
"""Repo-wide RAG ingestion (Component 5 support).

Walks every first-party source/doc file tracked by git, chunks + embeds them
with the local zero-cost embedding model, and replaces the `documents`
collection's contents with the result. Run this any time the repo has moved
on from what's indexed -- it's idempotent (clears and rebuilds), not
incremental.

Excludes frontend/, .venv/, crypto-predictor/ to match this project's
existing standalone-sub-project / non-first-party exclusions (see
pyproject.toml's [tool.coverage.run] comment and tool.ruff.extend-exclude).
"""

from __future__ import annotations

import os
import subprocess
import sys
import time

from rag_mongo.db import get_collection
from rag_mongo.embeddings import embed_batch
from rag_mongo.ingest import chunk_text

EXCLUDE_PATHSPECS = [":!:frontend/*", ":!:.venv/*", ":!:crypto-predictor/*"]
INCLUDE_GLOBS = ["*.py", "*.md", "*.toml", "*.yml", "*.yaml"]
EMBED_BATCH_SIZE = 256


def _exclude_pathspecs() -> list[str]:
    """tests/ is the bulk of the corpus (roughly two thirds of all chunks).
    RAG_INGEST_INCLUDE_TESTS=false drops it for a much faster rebuild when
    only source and docs need to be searchable."""
    include_tests = os.getenv("RAG_INGEST_INCLUDE_TESTS", "true").lower() not in {
        "false",
        "0",
        "no",
    }
    return EXCLUDE_PATHSPECS if include_tests else [*EXCLUDE_PATHSPECS, ":!:tests/*"]


def tracked_files() -> list[str]:
    out = subprocess.run(
        ["git", "ls-files", "--", *INCLUDE_GLOBS, *_exclude_pathspecs()],
        capture_output=True,
        text=True,
        check=True,
    )
    return [line for line in out.stdout.splitlines() if line]


def main() -> None:
    files = tracked_files()
    print(f"Found {len(files)} tracked files to ingest.")

    all_chunks: list[str] = []
    all_sources: list[str] = []
    skipped: list[str] = []

    for path in files:
        try:
            with open(path, encoding="utf-8") as f:
                text = f.read()
        except (UnicodeDecodeError, OSError) as exc:
            skipped.append(f"{path}: {exc}")
            continue
        if not text.strip():
            continue
        chunks = chunk_text(text)
        all_chunks.extend(chunks)
        all_sources.extend([path] * len(chunks))

    print(f"Chunked into {len(all_chunks)} chunks ({len(skipped)} files skipped).")

    col = get_collection()
    deleted = col.delete_many({}).deleted_count
    print(f"Cleared {deleted} stale documents from the collection.")

    t0 = time.time()
    inserted = 0
    for i in range(0, len(all_chunks), EMBED_BATCH_SIZE):
        batch_chunks = all_chunks[i : i + EMBED_BATCH_SIZE]
        batch_sources = all_sources[i : i + EMBED_BATCH_SIZE]
        vectors = embed_batch(batch_chunks)
        docs = [
            {"text": c, "embedding": v, "source": s}
            for c, v, s in zip(batch_chunks, vectors, batch_sources, strict=True)
        ]
        if docs:
            col.insert_many(docs)
            inserted += len(docs)
        print(f"  embedded+inserted {inserted}/{len(all_chunks)}", file=sys.stderr)

    elapsed = time.time() - t0
    print(f"Inserted {inserted} chunks from {len(files) - len(skipped)} files in {elapsed:.1f}s.")
    if skipped:
        print(f"Skipped {len(skipped)} files:")
        for s in skipped[:20]:
            print(f"  {s}")


if __name__ == "__main__":
    main()
