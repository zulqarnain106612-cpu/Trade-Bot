"""Tests for rag_mongo/embeddings.py."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from rag_mongo import embeddings


def _fake_vector(values):
    v = MagicMock()
    v.tolist.return_value = values
    return v


def test_embed_normalizes_and_returns_list():
    embeddings._model.cache_clear()
    fake_model = MagicMock()
    fake_model.encode.return_value = _fake_vector([0.1, 0.2, 0.3])
    with patch("rag_mongo.embeddings.SentenceTransformer", return_value=fake_model):
        result = embeddings.embed("hello world")
    assert result == [0.1, 0.2, 0.3]
    fake_model.encode.assert_called_once_with("hello world", normalize_embeddings=True)
    embeddings._model.cache_clear()


def test_embed_batch_returns_list_of_lists():
    embeddings._model.cache_clear()
    fake_model = MagicMock()
    fake_model.encode.return_value = [_fake_vector([1.0]), _fake_vector([2.0])]
    with patch("rag_mongo.embeddings.SentenceTransformer", return_value=fake_model):
        result = embeddings.embed_batch(["a", "b"])
    assert result == [[1.0], [2.0]]
    fake_model.encode.assert_called_once_with(["a", "b"], normalize_embeddings=True, batch_size=32)
    embeddings._model.cache_clear()


def test_model_is_cached_across_calls():
    embeddings._model.cache_clear()
    fake_model = MagicMock()
    fake_model.encode.return_value = _fake_vector([0.0])
    with patch("rag_mongo.embeddings.SentenceTransformer", return_value=fake_model) as mock_cls:
        embeddings.embed("a")
        embeddings.embed("b")
    mock_cls.assert_called_once()
    embeddings._model.cache_clear()
