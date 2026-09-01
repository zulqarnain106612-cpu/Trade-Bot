"""Tests for kg/query.py."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from kg.query import _candidate_names, _subgraph, query


def test_candidate_names_no_words_returns_empty():
    assert _candidate_names("? ! .") == []


def test_candidate_names_exact_hits():
    nodes_col = MagicMock()
    nodes_col.find.return_value = [{"name": "Alice"}]
    with patch("kg.query.get_nodes_collection", return_value=nodes_col):
        names = _candidate_names("Who is Alice?")
    assert names == ["Alice"]


def test_candidate_names_falls_back_to_regex_when_no_exact_hits():
    regex_cursor = MagicMock()
    regex_cursor.limit.return_value = [{"name": "Alice Smith"}]
    nodes_col = MagicMock()
    nodes_col.find.side_effect = [
        [],  # exact $in match: none, iterated directly
        regex_cursor,  # regex fallback: chained .limit(10)
    ]
    with patch("kg.query.get_nodes_collection", return_value=nodes_col):
        names = _candidate_names("alice")
    assert names == ["Alice Smith"]
    assert nodes_col.find.call_count == 2
    regex_cursor.limit.assert_called_once_with(10)


def test_subgraph_no_names_returns_unfiltered():
    edges_col = MagicMock()
    edges_col.find.return_value.limit.return_value = [{"subject": "A"}]
    with patch("kg.query.get_edges_collection", return_value=edges_col):
        result = _subgraph([], limit=10)
    assert result == [{"subject": "A"}]
    edges_col.find.assert_called_once_with({}, {"_id": 0})


def test_subgraph_with_names_filters():
    edges_col = MagicMock()
    edges_col.find.return_value.limit.return_value = [{"subject": "Alice"}]
    with patch("kg.query.get_edges_collection", return_value=edges_col):
        result = _subgraph(["Alice"], limit=5)
    assert result == [{"subject": "Alice"}]
    call_filter = edges_col.find.call_args[0][0]
    assert call_filter["$or"][0]["subject"]["$in"] == ["Alice"]


def test_query_end_to_end():
    triples = [
        {"subject": "Alice", "predicate": "works_with", "object": "Bob", "sources": ["doc.txt"]}
    ]
    with (
        patch("kg.query._candidate_names", return_value=["Alice"]),
        patch("kg.query._subgraph", return_value=triples),
        patch("kg.query.run_claude", return_value={"result": "Alice works with Bob."}) as mock_run,
    ):
        out = query("Who does Alice work with?")

    assert out == {"answer": "Alice works with Bob.", "triples_used": 1}
    prompt = mock_run.call_args[0][0]
    assert "Alice" in prompt and "Bob" in prompt


def test_query_with_no_triples_found():
    with (
        patch("kg.query._candidate_names", return_value=[]),
        patch("kg.query._subgraph", return_value=[]),
        patch("kg.query.run_claude", return_value={"result": "No data."}),
    ):
        out = query("anything?")
    assert out == {"answer": "No data.", "triples_used": 0}
