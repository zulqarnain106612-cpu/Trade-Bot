"""Tests for rag_mongo/retrieve.py."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from rag_mongo.retrieve import vector_search


def test_vector_search_builds_pipeline_and_returns_results():
    col = MagicMock()
    col.aggregate.return_value = [{"text": "chunk", "source": "doc.txt", "score": 0.8}]
    with (
        patch("rag_mongo.retrieve.embed", return_value=[0.1, 0.2]),
        patch("rag_mongo.retrieve.get_collection", return_value=col),
    ):
        results = vector_search("a query", top_k=3)

    assert results == [{"text": "chunk", "source": "doc.txt", "score": 0.8}]
    pipeline = col.aggregate.call_args[0][0]
    assert pipeline[0]["$vectorSearch"]["queryVector"] == [0.1, 0.2]
    assert pipeline[0]["$vectorSearch"]["limit"] == 3
    assert pipeline[0]["$vectorSearch"]["numCandidates"] == 50  # max(3*10, 50)
    assert pipeline[1]["$project"]["_id"] == 0


def test_vector_search_num_candidates_scales_with_top_k():
    col = MagicMock()
    col.aggregate.return_value = []
    with (
        patch("rag_mongo.retrieve.embed", return_value=[0.0]),
        patch("rag_mongo.retrieve.get_collection", return_value=col),
    ):
        vector_search("q", top_k=20)
    pipeline = col.aggregate.call_args[0][0]
    assert pipeline[0]["$vectorSearch"]["numCandidates"] == 200
