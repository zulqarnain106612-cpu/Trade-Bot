from pymongo import MongoClient
from pymongo.operations import SearchIndexModel

from .config import EMBEDDING_DIM, MONGODB_COLLECTION, MONGODB_DB, MONGODB_URI, VECTOR_INDEX_NAME


def get_collection():
    if not MONGODB_URI:
        raise RuntimeError("MONGODB_URI not set. Copy .env.example to .env and fill it in.")
    client = MongoClient(MONGODB_URI)
    return client[MONGODB_DB][MONGODB_COLLECTION]


def ensure_vector_index() -> None:
    """Idempotent Atlas Vector Search index creation (Atlas M0+ only)."""
    col = get_collection()
    existing = {ix["name"] for ix in col.list_search_indexes()}
    if VECTOR_INDEX_NAME in existing:
        return
    definition = {
        "fields": [
            {
                "type": "vector",
                "path": "embedding",
                "numDimensions": EMBEDDING_DIM,
                "similarity": "cosine",
            }
        ]
    }
    model = SearchIndexModel(definition=definition, name=VECTOR_INDEX_NAME, type="vectorSearch")
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
