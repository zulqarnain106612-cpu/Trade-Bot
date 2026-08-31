"""Tests for rag_mongo/pipeline.py."""

from __future__ import annotations

from unittest.mock import patch

from rag_mongo.pipeline import answer_query


def test_answer_query_wires_retrieval_into_generation():
    docs = [{"text": "chunk", "source": "doc.txt", "score": 0.9}]
    with (
        patch("rag_mongo.pipeline.vector_search", return_value=docs) as mock_search,
        patch(
            "rag_mongo.pipeline.generate", return_value={"answer": "the answer", "sources": docs}
        ) as mock_generate,
    ):
        out = answer_query("what is this about?")

    mock_search.assert_called_once_with("what is this about?")
    mock_generate.assert_called_once_with("what is this about?", docs)
    assert out == {"answer": "the answer", "sources": docs}
