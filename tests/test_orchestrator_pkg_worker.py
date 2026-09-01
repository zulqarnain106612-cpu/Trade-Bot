"""Tests for orchestrator/worker.py."""

from __future__ import annotations

from unittest.mock import patch

from orchestrator.worker import execute


def test_execute_returns_digest_and_cost():
    with patch(
        "orchestrator.worker.run_claude",
        return_value={"result": "short digest", "cost_usd": 0.003},
    ) as mock_run:
        out = execute("look something up")

    assert out == {"subtask": "look something up", "digest": "short digest", "cost_usd": 0.003}
    assert "look something up" in mock_run.call_args[0][0]
