"""Tests for orchestrator/planner.py (the new plan-big/execute-small package,
distinct from src/engine/orchestrator.py covered by tests/test_orchestrator*.py)."""

from __future__ import annotations

import json

import pytest
from unittest.mock import patch

from orchestrator.planner import plan


def test_plan_parses_plain_json_array():
    with patch("orchestrator.planner.run_claude", return_value={"result": '["a", "b"]'}):
        assert plan("goal") == ["a", "b"]


def test_plan_strips_fenced_code_block_with_language_tag():
    fenced = '```json\n["a", "b"]\n```'
    with patch("orchestrator.planner.run_claude", return_value={"result": fenced}):
        assert plan("goal") == ["a", "b"]


def test_plan_strips_bare_fence_no_newline():
    # "```[\"a\"]```" -> strip("`") leaves "[\"a\"]" with no "\n" to split on
    fenced = "```" + json.dumps(["a"]) + "```"
    with patch("orchestrator.planner.run_claude", return_value={"result": fenced}):
        assert plan("goal") == ["a"]


def test_plan_raises_on_non_list_response():
    with patch("orchestrator.planner.run_claude", return_value={"result": '{"not": "a list"}'}):
        with pytest.raises(ValueError, match="did not return a JSON list"):
            plan("goal")


def test_plan_truncates_to_max_subtasks():
    with (
        patch("orchestrator.planner.MAX_SUBTASKS", 2),
        patch("orchestrator.planner.run_claude", return_value={"result": '["a", "b", "c"]'}),
    ):
        assert plan("goal") == ["a", "b"]
