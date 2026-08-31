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
