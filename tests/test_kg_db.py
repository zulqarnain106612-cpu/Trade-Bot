"""Tests for kg/db.py -- MongoDB collection accessors."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import certifi
import pytest

import kg.db as kg_db


def test_require_uri_raises_when_unset():
    with patch.object(kg_db, "MONGODB_URI", ""):
        with pytest.raises(RuntimeError, match="MONGODB_URI not set"):
            kg_db._require_uri()


def test_require_uri_returns_value_when_set():
    with patch.object(kg_db, "MONGODB_URI", "mongodb://x"):
        assert kg_db._require_uri() == "mongodb://x"


def test_get_nodes_collection():
    fake_client = MagicMock()
    with (
        patch.object(kg_db, "MONGODB_URI", "mongodb://x"),
        patch.object(kg_db, "MongoClient", return_value=fake_client) as mock_client_cls,
    ):
        col = kg_db.get_nodes_collection()
    mock_client_cls.assert_called_once_with("mongodb://x", tlsCAFile=certifi.where())
    assert col is fake_client[kg_db.MONGODB_DB][kg_db.KG_NODES_COLLECTION]


def test_get_edges_collection():
    fake_client = MagicMock()
    with (
        patch.object(kg_db, "MONGODB_URI", "mongodb://x"),
        patch.object(kg_db, "MongoClient", return_value=fake_client),
    ):
        col = kg_db.get_edges_collection()
    assert col is fake_client[kg_db.MONGODB_DB][kg_db.KG_EDGES_COLLECTION]
