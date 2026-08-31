"""Tests for rag_mongo/db.py."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

import rag_mongo.db as rag_db


def test_get_collection_raises_when_uri_unset():
    with patch.object(rag_db, "MONGODB_URI", ""):
        with pytest.raises(RuntimeError, match="MONGODB_URI not set"):
            rag_db.get_collection()


def test_get_collection_returns_configured_collection():
    fake_client = MagicMock()
    with (
        patch.object(rag_db, "MONGODB_URI", "mongodb://x"),
        patch.object(rag_db, "MongoClient", return_value=fake_client),
    ):
        col = rag_db.get_collection()
    assert col is fake_client[rag_db.MONGODB_DB][rag_db.MONGODB_COLLECTION]


def test_ensure_vector_index_skips_when_already_present():
    col = MagicMock()
    col.list_search_indexes.return_value = [{"name": rag_db.VECTOR_INDEX_NAME}]
    with patch.object(rag_db, "get_collection", return_value=col):
        rag_db.ensure_vector_index()
    col.create_search_index.assert_not_called()


def test_ensure_vector_index_creates_when_missing():
    col = MagicMock()
    col.list_search_indexes.return_value = []
    with patch.object(rag_db, "get_collection", return_value=col):
        rag_db.ensure_vector_index()
    col.create_search_index.assert_called_once()


def test_ensure_fulltext_index_skips_when_already_present():
    col = MagicMock()
    col.list_search_indexes.return_value = [{"name": "fulltext_index"}]
    with patch.object(rag_db, "get_collection", return_value=col):
        rag_db.ensure_fulltext_index()
    col.create_search_index.assert_not_called()


def test_ensure_fulltext_index_creates_when_missing_with_custom_name():
    col = MagicMock()
    col.list_search_indexes.return_value = []
    with patch.object(rag_db, "get_collection", return_value=col):
        rag_db.ensure_fulltext_index(name="custom_index")
    col.create_search_index.assert_called_once()
