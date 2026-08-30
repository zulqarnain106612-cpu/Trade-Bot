from pymongo import MongoClient
from pymongo.operations import SearchIndexModel
from .config import MONGODB_URI, MONGODB_DB, MONGODB_COLLECTION, \
    VECTOR_INDEX_NAME, EMBEDDING_DIM


def get_collection():
    if not MONGODB_URI:
        raise RuntimeError(
            "MONGODB_URI not set. Copy .env.example to .env and fill it in."
        )
    client = MongoClient(MONGODB_URI)
    return client[MONGODB_DB][MONGODB_COLLECTION]


def ensure_vector_index() -> None:
    """Idempotent Atlas Vector Search index creation (Atlas M0+ only)."""
    col = get_collection()
    existing = {ix["name"] for ix in col.list_search_indexes()}
    if VECTOR_INDEX_NAME in existing:
        return
    definition = {
        "fields": [{
            "type": "vector",
            "path": "embedding",
            "numDimensions": EMBEDDING_DIM,
            "similarity": "cosine",
        }]
    }
    model = SearchIndexModel(
        definition=definition, name=VECTOR_INDEX_NAME, type="vectorSearch"
    )
    col.create_search_index(model=model)
