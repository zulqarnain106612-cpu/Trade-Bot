"""Worker: executes one subtask with a cheap model. Only its final digest
(the `result` field) ever leaves this module -- raw tool transcripts,
intermediate reasoning, and any file contents the worker read are discarded
here, never forwarded to the coordinator."""
from __future__ import annotations

from common.claude_cli import run_claude
from .config import WORKER_MODEL, MAX_WORKER_TURNS, WORKER_ALLOWED_TOOLS

_DIGEST_HINT = (
    "Do the task below. Reply with a SHORT digest only (a few sentences or a "
    "small table) -- the minimum needed for someone else to use your finding. "
    "Do not paste raw file contents or command output.\n\nTASK: "
)


def execute(subtask: str) -> dict:
    prompt = _DIGEST_HINT + subtask
    out = run_claude(
        prompt,
        model=WORKER_MODEL,
        allowed_tools=WORKER_ALLOWED_TOOLS,
        max_turns=MAX_WORKER_TURNS,
    )
    return {"subtask": subtask, "digest": out["result"], "cost_usd": out["cost_usd"]}
