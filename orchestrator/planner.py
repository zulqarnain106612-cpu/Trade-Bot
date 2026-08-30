"""Coordinator: plans big, exactly once. The cheap worker model never sees
this prompt, and this prompt never sees any worker's raw output."""
from __future__ import annotations

import json

from common.claude_cli import run_claude
from .config import COORDINATOR_MODEL, MAX_SUBTASKS

_SCHEMA_HINT = (
    "Respond with ONLY a JSON array of up to {n} short, independent, "
    "self-contained subtask instructions (strings) that together accomplish "
    "the goal. No prose, no markdown fences, just the JSON array."
)


def plan(task: str) -> list[str]:
    prompt = f"{_SCHEMA_HINT.format(n=MAX_SUBTASKS)}\n\nGOAL: {task}"
    out = run_claude(prompt, model=COORDINATOR_MODEL, max_turns=1)
    text = out["result"].strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text.split("\n", 1)[1] if "\n" in text else text
    subtasks = json.loads(text)
    if not isinstance(subtasks, list):
        raise ValueError(f"Planner did not return a JSON list: {text[:200]}")
    return [str(s) for s in subtasks[:MAX_SUBTASKS]]
