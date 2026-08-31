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
        {"$project": {"_id": 0, "text": 1, "source": 1, "score": {"$meta": "searchScore"}}},
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
