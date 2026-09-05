"""Reuses the RAG component's Mongo connection settings (same Atlas
cluster/database, separate collections) -- no new infra to provision."""

from functools import lru_cache

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


@lru_cache(maxsize=1)
def _client_for(uri: str) -> MongoClient:
    # Same certifi pinning as rag_mongo.db: Atlas's chain is not verifiable
    # against a bare CI runner's system trust store, and the failure reads as
    # an opaque SSL handshake error. Ignored for a non-TLS URI.
    return MongoClient(uri, tlsCAFile=certifi.where())


def _client() -> MongoClient:
    # lru_cache: one MongoClient per process, which is what PyMongo asks for.
    # Each client owns a connection pool and its own monitoring threads, and
    # nothing here closes them -- building one per call left a pool behind on
    # every call. rag_smoke.py alone calls into this five times, and
    # ingest_repo.py runs for the better part of an hour.
    #
    # The URI check stays outside the cached call: lru_cache does not cache
    # exceptions, so an unset MONGODB_URI keeps raising rather than being
    # remembered as a result.
    return _client_for(_require_uri())


def get_nodes_collection():
    return _client()[MONGODB_DB][KG_NODES_COLLECTION]


def get_edges_collection():
    return _client()[MONGODB_DB][KG_EDGES_COLLECTION]
