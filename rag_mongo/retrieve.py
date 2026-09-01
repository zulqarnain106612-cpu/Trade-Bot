from .config import TOP_K, VECTOR_INDEX_NAME
from .db import get_collection
from .embeddings import embed


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
        {"$project": {"_id": 0, "text": 1, "source": 1, "score": {"$meta": "vectorSearchScore"}}},
    ]
    return list(get_collection().aggregate(pipeline))
