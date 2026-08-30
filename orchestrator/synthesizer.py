"""Coordinator's second and final call: synthesize digests into one answer.
Only the short digests are included -- never the workers' raw sessions."""
from __future__ import annotations

from common.claude_cli import run_claude
from .config import COORDINATOR_MODEL


def synthesize(task: str, digests: list[dict]) -> dict:
    bullet_list = "\n".join(f"- {d['subtask']}: {d['digest']}" for d in digests)
    prompt = (
        f"Original goal: {task}\n\n"
        f"Findings from delegated subtasks:\n{bullet_list}\n\n"
        "Write the final answer to the original goal using only these findings."
    )
    return run_claude(prompt, model=COORDINATOR_MODEL, max_turns=1)
