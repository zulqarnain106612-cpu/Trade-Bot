"""Tests for orchestrator/run.py (the plan-big/execute-small entry point)."""

from __future__ import annotations

from unittest.mock import patch

from orchestrator.run import run


def test_run_delegates_plans_executes_and_synthesizes():
    with (
        patch("orchestrator.run.plan", return_value=["sub1", "sub2"]),
        patch(
            "orchestrator.run.execute",
            side_effect=lambda st: {"subtask": st, "digest": f"digest for {st}", "cost_usd": 0.01},
        ),
        patch(
            "orchestrator.run.synthesize",
            return_value={"result": "final answer", "cost_usd": 0.02},
        ),
    ):
        out = run("do the thing")

    assert out["task"] == "do the thing"
    assert set(out["subtasks"]) == {"sub1", "sub2"}
    assert len(out["worker_digests"]) == 2
    assert out["answer"] == "final answer"
    assert out["usage_estimate_usd"] == round(0.01 + 0.01 + 0.02, 4)
    assert "Estimate only" in out["note"]


def test_run_with_no_subtasks_still_synthesizes():
    with (
        patch("orchestrator.run.plan", return_value=[]),
        patch("orchestrator.run.synthesize", return_value={"result": "final", "cost_usd": 0.0}),
    ):
        out = run("trivial goal")
    assert out["worker_digests"] == []
    assert out["answer"] == "final"
