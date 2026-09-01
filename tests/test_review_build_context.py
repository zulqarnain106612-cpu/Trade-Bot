"""build_context must degrade rather than fail the pull request.

The cloud review it feeds is advisory -- docs/CLOUD_REVIEW.md says it never
approves or merges -- so an unreachable Atlas cluster should cost the
reviewer its grounding, not turn every open PR red. That is exactly what
happened: a TLS handshake failure against the Atlas replica set failed
retrieve-context, which claude-review depends on, on all four open PRs at
once.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from review import build_context as bc


_REAL_DIFF_TEXT = bc._diff_text


@pytest.fixture(autouse=True)
def _no_git(monkeypatch):
    monkeypatch.setattr(bc, "_diff_text", lambda _b, _h: "def some_function(): pass")


def test_a_successful_retrieval_is_shaped_for_the_prompt():
    docs = [{"source": "docs/a.md", "text": "x" * 500}]
    edges = [{"subject": "A", "predicate": "uses", "object": "B"}]

    with (
        patch.object(bc, "hybrid", return_value=docs),
        patch.object(bc, "graph", return_value=edges),
    ):
        ctx = bc.build_context("base", "head")

    assert ctx["related_documents"] == [{"source": "docs/a.md", "excerpt": "x" * 300}]
    assert ctx["related_facts"] == ["(A) -[uses]-> (B)"]
    assert "retrieval_error" not in ctx


def test_an_unreachable_store_yields_an_empty_context_not_an_exception():
    def _boom(*_a, **_k):
        raise RuntimeError("SSL handshake failed: ac-shard-00-02.mongodb.net:27017")

    with patch.object(bc, "hybrid", _boom):
        ctx = bc.build_context("base", "head")

    assert ctx["related_documents"] == []
    assert ctx["related_facts"] == []
    assert "SSL handshake failed" in ctx["retrieval_error"]


def test_a_graph_failure_degrades_the_same_way():
    def _boom(*_a, **_k):
        raise TimeoutError("server selection timed out")

    with patch.object(bc, "hybrid", return_value=[]), patch.object(bc, "graph", _boom):
        ctx = bc.build_context("base", "head")

    assert ctx["related_facts"] == []
    assert "timed out" in ctx["retrieval_error"]


def test_an_empty_diff_still_produces_a_query():
    """No identifiers in the diff must not mean an empty query string."""
    with patch.object(bc, "_diff_text", lambda _b, _h: ""):
        with patch.object(bc, "hybrid") as hybrid, patch.object(bc, "graph", return_value=[]):
            hybrid.return_value = []
            bc.build_context("base", "head")

    assert hybrid.call_args.args[0] == "code change"


def test_the_term_list_is_capped_for_a_huge_diff():
    diff = " ".join(f"identifier_{i}" for i in range(500))
    assert len(bc._candidate_terms(diff)) == 50


def test_the_diff_is_read_with_a_three_dot_range(monkeypatch):
    """base...head, so the review sees the branch's own changes only."""
    import subprocess

    seen = {}

    def _run(cmd, **kwargs):
        seen["cmd"] = cmd
        seen["kwargs"] = kwargs
        return subprocess.CompletedProcess(cmd, 0, stdout="diff text", stderr="")

    monkeypatch.setattr(bc.subprocess, "run", _run)
    # the autouse fixture stubs _diff_text out; this test wants the real one
    monkeypatch.setattr(bc, "_diff_text", _REAL_DIFF_TEXT)

    assert bc._diff_text("aaa", "bbb") == "diff text"
    assert seen["cmd"] == ["git", "diff", "aaa...bbb"]
    assert seen["kwargs"]["check"] is True
