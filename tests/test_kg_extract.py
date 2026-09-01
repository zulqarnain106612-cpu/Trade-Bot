"""Tests for kg/extract.py."""

from __future__ import annotations

from unittest.mock import patch

from kg.extract import extract_triples


def test_extract_triples_stamps_source():
    fake_out = {
        "result": "{}",
        "structured_output": {
            "triples": [
                {"subject": "Alice", "predicate": "works_with", "object": "Bob"},
            ]
        },
    }
    with patch("kg.extract.run_claude", return_value=fake_out) as mock_run:
        triples = extract_triples("Alice works with Bob.", source="doc.txt")
    assert triples == [
        {"subject": "Alice", "predicate": "works_with", "object": "Bob", "source": "doc.txt"}
    ]
    assert mock_run.call_args.kwargs["json_schema"] is not None


def test_extract_triples_handles_missing_structured_output():
    with patch("kg.extract.run_claude", return_value={"result": "", "structured_output": None}):
        triples = extract_triples("text", source="doc.txt")
    assert triples == []
