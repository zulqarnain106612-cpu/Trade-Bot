"""Tests for kg/assemble.py."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from kg.assemble import assemble


def _mock_update_result(upserted: bool) -> MagicMock:
    res = MagicMock()
    res.upserted_id = "id" if upserted else None
    return res


def test_assemble_counts_upserts_and_uses_canonical_names():
    triples = [
        {"subject": "Alice", "predicate": "works_with", "object": "Bob", "source": "doc.txt"},
    ]
    nodes_col = MagicMock()
    nodes_col.update_one.return_value = _mock_update_result(upserted=True)
    edges_col = MagicMock()
    edges_col.update_one.return_value = _mock_update_result(upserted=True)

    with (
        patch("kg.assemble.get_nodes_collection", return_value=nodes_col),
        patch("kg.assemble.get_edges_collection", return_value=edges_col),
        patch("kg.assemble.resolve_entities", return_value={"Alice": "Alice Smith"}),
    ):
        stats = assemble(triples)

    assert stats == {"nodes_upserted": 2, "edges_upserted": 1}
    edge_filter = edges_col.update_one.call_args[0][0]
    assert edge_filter["subject"] == "Alice Smith"
    assert edge_filter["object"] == "Bob"


def test_assemble_no_upserts_when_already_present():
    triples = [{"subject": "X", "predicate": "p", "object": "Y"}]
    nodes_col = MagicMock()
    nodes_col.update_one.return_value = _mock_update_result(upserted=False)
    edges_col = MagicMock()
    edges_col.update_one.return_value = _mock_update_result(upserted=False)

    with (
        patch("kg.assemble.get_nodes_collection", return_value=nodes_col),
        patch("kg.assemble.get_edges_collection", return_value=edges_col),
        patch("kg.assemble.resolve_entities", return_value={}),
    ):
        stats = assemble(triples)

    assert stats == {"nodes_upserted": 0, "edges_upserted": 0}
    # source defaults to "unknown" when the triple has none
    call_kwargs = edges_col.update_one.call_args[0][1]
    assert call_kwargs["$addToSet"]["sources"] == "unknown"


def test_assemble_empty_triples():
    with (
        patch("kg.assemble.get_nodes_collection"),
        patch("kg.assemble.get_edges_collection"),
        patch("kg.assemble.resolve_entities", return_value={}),
    ):
        assert assemble([]) == {"nodes_upserted": 0, "edges_upserted": 0}
