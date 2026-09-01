"""Tests for rag_mongo/generate.py."""

from __future__ import annotations

from unittest.mock import patch

from rag_mongo.generate import generate


def test_generate_builds_context_with_sources_and_returns_answer():
    docs = [
        {"text": "doc one text", "source": "a.txt"},
        {"text": "doc two text"},  # no "source" key -> defaults to "unknown"
    ]
    with patch("rag_mongo.generate.run_claude", return_value={"result": "the answer"}) as mock_run:
        out = generate("what happened?", docs)

    assert out == {"answer": "the answer", "sources": docs}
    prompt = mock_run.call_args[0][0]
    assert "[Source: a.txt]" in prompt
    assert "[Source: unknown]" in prompt
    assert "what happened?" in prompt


def test_generate_with_no_docs():
    with patch("rag_mongo.generate.run_claude", return_value={"result": "no context"}):
        out = generate("q", [])
    assert out == {"answer": "no context", "sources": []}
