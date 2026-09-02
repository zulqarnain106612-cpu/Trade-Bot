"""Reuses the RAG component's Mongo connection settings (same Atlas
cluster/database, separate collections) -- no new infra to provision."""

import certifi
from pymongo import MongoClient

from rag_mongo.config import MONGODB_DB, MONGODB_URI

from .config import KG_EDGES_COLLECTION, KG_NODES_COLLECTION


def _require_uri() -> str:
    if not MONGODB_URI:
        raise RuntimeError(
            "MONGODB_URI not set. Locally: copy .env.example to .env and fill it in. "
            "In a Claude Code cloud session: set it in the environment's variables."
        )
    return MONGODB_URI


def _client() -> MongoClient:
    # Same certifi pinning as rag_mongo.db.get_collection: Atlas's chain is not
    # verifiable against a bare CI runner's system trust store, and the failure
    # reads as an opaque SSL handshake error. Ignored for a non-TLS URI.
    return MongoClient(_require_uri(), tlsCAFile=certifi.where())


def get_nodes_collection():
    return _client()[MONGODB_DB][KG_NODES_COLLECTION]


def get_edges_collection():
    return _client()[MONGODB_DB][KG_EDGES_COLLECTION]
