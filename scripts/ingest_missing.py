#!/usr/bin/env python3
"""Incremental top-up for the RAG store.

scripts/ingest_repo.py is a clear-and-rebuild: correct, but it re-embeds the
whole repo. When a rebuild is interrupted, or a handful of files land, this
embeds only the tracked files that have no chunks in the collection yet and
inserts those. It never deletes, so it is safe to re-run.
"""

from __future__ import annotations

import sys
import time

from rag_mongo.db import get_collection
from rag_mongo.embeddings import embed_batch
from rag_mongo.ingest import chunk_text

sys.path.insert(0, ".")

from scripts.ingest_repo import EMBED_BATCH_SIZE, tracked_files  # noqa: E402


def main() -> None:
    col = get_collection()
    present = set(col.distinct("source"))
    missing = [p for p in tracked_files() if p not in present]
    print(f"{len(present)} sources indexed; {len(missing)} tracked files missing.")
    if not missing:
        print("Nothing to do -- the store already covers every tracked file.")
        return

    chunks: list[str] = []
    sources: list[str] = []
    for path in missing:
        try:
            with open(path, encoding="utf-8") as f:
                text = f.read()
        except (UnicodeDecodeError, OSError) as exc:
            print(f"  skipped {path}: {exc}")
            continue
        if not text.strip():
            continue
        file_chunks = chunk_text(text)
        chunks.extend(file_chunks)
        sources.extend([path] * len(file_chunks))

    print(f"Chunked {len(chunks)} new chunks.")
    t0 = time.time()
    inserted = 0
    for i in range(0, len(chunks), EMBED_BATCH_SIZE):
        batch = chunks[i : i + EMBED_BATCH_SIZE]
        batch_sources = sources[i : i + EMBED_BATCH_SIZE]
        vectors = embed_batch(batch)
        col.insert_many(
            [
                {"text": c, "embedding": v, "source": s}
                for c, v, s in zip(batch, vectors, batch_sources, strict=True)
            ]
        )
        inserted += len(batch)
        print(f"  embedded+inserted {inserted}/{len(chunks)}", file=sys.stderr, flush=True)

    print(f"Inserted {inserted} chunks for {len(missing)} files in {time.time() - t0:.1f}s.")


if __name__ == "__main__":
    main()
