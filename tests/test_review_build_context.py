"""Tests for review/build_context.py."""

from __future__ import annotations

import subprocess
from unittest.mock import patch

from review.build_context import _candidate_terms, _diff_text, build_context


def test_diff_text_runs_git_diff():
    completed = subprocess.CompletedProcess(args=[], returncode=0, stdout="diff output", stderr="")
    with patch("review.build_context.subprocess.run", return_value=completed) as mock_run:
        out = _diff_text("base_sha", "head_sha")
    assert out == "diff output"
    cmd = mock_run.call_args[0][0]
    assert cmd == ["git", "diff", "base_sha...head_sha"]


def test_candidate_terms_extracts_unique_sorted_identifiers():
    diff = "def foo_bar(): return baz_qux + foo_bar"
    terms = _candidate_terms(diff)
    assert terms == sorted(set(terms))
    assert "foo_bar" in terms
    assert "baz_qux" in terms


def test_candidate_terms_caps_at_fifty():
    diff = " ".join(f"identifier_{i:04d}" for i in range(80))
    terms = _candidate_terms(diff)
    assert len(terms) == 50


def test_build_context_combines_hybrid_and_graph_results():
    docs = [{"source": "a.py", "text": "x" * 400}]
    edges = [{"subject": "Foo", "predicate": "calls", "object": "Bar"}]
    with (
        patch("review.build_context._diff_text", return_value="def foo(): pass"),
        patch("review.build_context.hybrid", return_value=docs),
        patch("review.build_context.graph", return_value=edges),
    ):
        ctx = build_context("base", "head")

    assert ctx["related_documents"] == [{"source": "a.py", "excerpt": "x" * 300}]
    assert ctx["related_facts"] == ["(Foo) -[calls]-> (Bar)"]


def test_build_context_empty_diff_uses_fallback_query():
    with (
        patch("review.build_context._diff_text", return_value=""),
        patch("review.build_context.hybrid", return_value=[]) as mock_hybrid,
        patch("review.build_context.graph", return_value=[]),
    ):
        build_context("base", "head")
    # no identifiers found -> query_text falls back to "code change"
    assert mock_hybrid.call_args[0][0] == "code change"
