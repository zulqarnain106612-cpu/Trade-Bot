"""Tests for orchestrator/synthesizer.py."""

from __future__ import annotations

from unittest.mock import patch

from orchestrator.synthesizer import synthesize


def test_synthesize_builds_bullet_list_from_digests():
    digests = [
        {"subtask": "find X", "digest": "X is 1"},
        {"subtask": "find Y", "digest": "Y is 2"},
    ]
    with patch(
        "orchestrator.synthesizer.run_claude", return_value={"result": "final", "cost_usd": 0.01}
    ) as mock_run:
        out = synthesize("original goal", digests)

    assert out == {"result": "final", "cost_usd": 0.01}
    prompt = mock_run.call_args[0][0]
    assert "original goal" in prompt
    assert "find X: X is 1" in prompt
    assert "find Y: Y is 2" in prompt
