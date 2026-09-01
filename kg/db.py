"""Reuses the RAG component's Mongo connection settings (same Atlas
cluster/database, separate collections) -- no new infra to provision."""

from pymongo import MongoClient
from rag_mongo.config import MONGODB_URI, MONGODB_DB
from .config import KG_NODES_COLLECTION, KG_EDGES_COLLECTION


def _require_uri() -> str:
    if not MONGODB_URI:
        raise RuntimeError(
            "MONGODB_URI not set. Locally: copy .env.example to .env and fill it in. "
            "In a Claude Code cloud session: set it in the environment's variables."
        )
    return MONGODB_URI


def get_nodes_collection():
    return MongoClient(_require_uri())[MONGODB_DB][KG_NODES_COLLECTION]


def get_edges_collection():
    return MongoClient(_require_uri())[MONGODB_DB][KG_EDGES_COLLECTION]
