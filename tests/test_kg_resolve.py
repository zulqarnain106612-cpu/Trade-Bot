"""Tests for kg/resolve.py."""

from __future__ import annotations

from unittest.mock import patch

from kg.resolve import resolve_entities


def test_resolve_entities_empty_input_short_circuits():
    with patch("kg.resolve.run_claude") as mock_run:
        assert resolve_entities([]) == {}
    mock_run.assert_not_called()


def test_resolve_entities_builds_canonical_mapping():
    fake_out = {
        "structured_output": {
            "clusters": [
                {"canonical_name": "Alice Smith", "aliases": ["Alice", "A. Smith"]},
                {"canonical_name": "Bob"},
            ]
        }
    }
    with patch("kg.resolve.run_claude", return_value=fake_out):
        mapping = resolve_entities(["Alice", "A. Smith", "Bob", "Alice Smith"])
    assert mapping == {
        "Alice Smith": "Alice Smith",
        "Alice": "Alice Smith",
        "A. Smith": "Alice Smith",
        "Bob": "Bob",
    }


def test_resolve_entities_missing_structured_output():
    with patch("kg.resolve.run_claude", return_value={"structured_output": None}):
        assert resolve_entities(["X"]) == {}
