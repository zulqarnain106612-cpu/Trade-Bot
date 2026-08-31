"""Tests for review/retrieval.py -- the four cloud-review retrieval patterns."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from review.retrieval import full_text, graph, hybrid, vector


def test_vector_delegates_to_vector_search():
    with patch("review.retrieval.vector_search", return_value=[{"text": "x"}]) as mock_search:
        result = vector("query", top_k=7)
    mock_search.assert_called_once_with("query", 7)
    assert result == [{"text": "x"}]


def test_full_text_builds_search_pipeline():
    col = MagicMock()
    col.aggregate.return_value = [{"text": "hit", "source": "a.txt", "score": 1.2}]
    with patch("review.retrieval.get_collection", return_value=col):
        result = full_text("query", top_k=4)
    assert result == [{"text": "hit", "source": "a.txt", "score": 1.2}]
    pipeline = col.aggregate.call_args[0][0]
    assert pipeline[0]["$search"]["text"]["query"] == "query"
    assert pipeline[1]["$limit"] == 4
    assert pipeline[2]["$project"]["_id"] == 0


def test_hybrid_merges_and_ranks_by_reciprocal_rank_fusion():
    shared = {"source": "a.txt", "text": "shared chunk text"}
    v_only = {"source": "b.txt", "text": "vector only chunk"}
    f_only = {"source": "c.txt", "text": "fulltext only chunk"}
    with (
        patch("review.retrieval.vector", return_value=[shared, v_only]),
        patch("review.retrieval.full_text", return_value=[shared, f_only]),
    ):
        ranked = hybrid("query", top_k=3)

    # "shared" appears in both lists, so its RRF score is boosted and it must
    # rank first; all three unique docs must be present since top_k=3.
    assert ranked[0] == shared
    assert {d["source"] for d in ranked} == {"a.txt", "b.txt", "c.txt"}


def test_hybrid_respects_top_k_truncation():
    docs = [{"source": f"{i}.txt", "text": f"chunk {i}"} for i in range(5)]
    with (
        patch("review.retrieval.vector", return_value=docs),
        patch("review.retrieval.full_text", return_value=[]),
    ):
        ranked = hybrid("query", top_k=2)
    assert len(ranked) == 2


def test_graph_no_entities_returns_unfiltered():
    edges_col = MagicMock()
    edges_col.find.return_value.limit.return_value = [{"subject": "A"}]
    with patch("review.retrieval.get_edges_collection", return_value=edges_col):
        result = graph([], limit=10)
    assert result == [{"subject": "A"}]
    edges_col.find.assert_called_once_with({}, {"_id": 0})


def test_graph_with_entities_filters():
    edges_col = MagicMock()
    edges_col.find.return_value.limit.return_value = [{"subject": "Foo"}]
    with patch("review.retrieval.get_edges_collection", return_value=edges_col):
        result = graph(["Foo"], limit=5)
    assert result == [{"subject": "Foo"}]
    call_filter = edges_col.find.call_args[0][0]
    assert call_filter["$or"][0]["subject"]["$in"] == ["Foo"]
