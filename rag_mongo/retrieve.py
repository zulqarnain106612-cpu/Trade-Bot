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
