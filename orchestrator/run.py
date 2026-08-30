"""Plan-big / execute-small orchestrator: local adaptation of Anthropic's
Managed Agents 'CMA_plan_big_execute_small' cookbook pattern for Claude Code
on a Pro/Max subscription -- no Managed Agents API/beta access needed.

Context/token discipline enforced here:
  1. Every `claude -p` call is stateless (no --continue/--resume) so context
     never accumulates across calls.
  2. Workers return a short digest only; their raw tool output/transcript
     never reaches the coordinator.
  3. The coordinator makes exactly two calls total (plan, synthesize) --
     never one call per subtask.
  4. Subtasks run concurrently (threads), bounded by MAX_SUBTASKS, so wall
     time doesn't scale linearly with subtask count.
"""
from __future__ import annotations

import json
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

from .planner import plan
from .worker import execute
from .synthesizer import synthesize


def run(task: str) -> dict:
    subtasks = plan(task)

    digests: list[dict] = []
    total_cost = 0.0
    with ThreadPoolExecutor(max_workers=max(len(subtasks), 1)) as pool:
        futures = {pool.submit(execute, st): st for st in subtasks}
        for fut in as_completed(futures):
            d = fut.result()
            digests.append(d)
            total_cost += d["cost_usd"]

    final = synthesize(task, digests)
    total_cost += final["cost_usd"]

    return {
        "task": task,
        "subtasks": subtasks,
        "worker_digests": digests,
        "answer": final["result"],
        "usage_estimate_usd": round(total_cost, 4),
        "note": "Estimate only -- billed against your Pro/Max usage limits, "
                "not metered API dollars.",
    }


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print('usage: python -m orchestrator.run "<task description>"', file=sys.stderr)
        sys.exit(1)
    result = run(" ".join(sys.argv[1:]))
    print(json.dumps(result, indent=2))
