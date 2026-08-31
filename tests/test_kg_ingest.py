"""Tests for kg/ingest.py."""

from __future__ import annotations

from unittest.mock import patch

from kg.ingest import ingest_document


def test_ingest_document_combines_extract_and_assemble():
    triples = [{"subject": "A", "predicate": "p", "object": "B", "source": "doc.txt"}]
    with (
        patch("kg.ingest.extract_triples", return_value=triples) as mock_extract,
        patch(
            "kg.ingest.assemble", return_value={"nodes_upserted": 2, "edges_upserted": 1}
        ) as mock_assemble,
    ):
        stats = ingest_document("A works with B.", source="doc.txt")

    mock_extract.assert_called_once_with("A works with B.", "doc.txt")
    mock_assemble.assert_called_once_with(triples)
    assert stats == {"nodes_upserted": 2, "edges_upserted": 1, "triples_extracted": 1}
