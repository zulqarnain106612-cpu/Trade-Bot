"""Tests for rag_mongo/ingest.py."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from rag_mongo.ingest import chunk_text, ingest_text


def test_chunk_text_shorter_than_size_returns_one_chunk():
    assert chunk_text("hello", size=800, overlap=100) == ["hello"]


def test_chunk_text_splits_with_overlap():
    text = "a" * 25
    chunks = chunk_text(text, size=10, overlap=2)
    assert chunks[0] == "a" * 10
    assert len(chunks) == 4  # step = size - overlap = 8; starts at 0, 8, 16, 24


def test_chunk_text_empty_string_returns_no_chunks():
    assert chunk_text("", size=10, overlap=2) == []


def test_ingest_text_inserts_documents_and_returns_count():
    col = MagicMock()
    with (
        patch("rag_mongo.ingest.get_collection", return_value=col),
        patch("rag_mongo.ingest.embed_batch", return_value=[[0.1], [0.2]]),
        patch("rag_mongo.ingest.chunk_text", return_value=["chunk1", "chunk2"]),
    ):
        n = ingest_text("some long text", source="doc.txt")
    assert n == 2
    col.insert_many.assert_called_once()
    docs = col.insert_many.call_args[0][0]
    assert docs[0] == {"text": "chunk1", "embedding": [0.1], "source": "doc.txt"}


def test_ingest_text_no_chunks_skips_insert():
    col = MagicMock()
    with (
        patch("rag_mongo.ingest.get_collection", return_value=col),
        patch("rag_mongo.ingest.embed_batch", return_value=[]),
        patch("rag_mongo.ingest.chunk_text", return_value=[]),
    ):
        n = ingest_text("", source="doc.txt")
    assert n == 0
    col.insert_many.assert_not_called()
