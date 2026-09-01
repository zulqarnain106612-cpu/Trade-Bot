from .db import get_collection
from .embeddings import embed_batch


def chunk_text(text: str, size: int = 800, overlap: int = 100) -> list[str]:
    chunks, i = [], 0
    while i < len(text):
        chunks.append(text[i : i + size])
        i += size - overlap
    return chunks


def ingest_text(text: str, source: str) -> int:
    chunks = chunk_text(text)
    vectors = embed_batch(chunks)
    docs = [
        {"text": c, "embedding": v, "source": source} for c, v in zip(chunks, vectors, strict=True)
    ]
    if docs:
        get_collection().insert_many(docs)
    return len(docs)
