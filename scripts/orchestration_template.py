#!/usr/bin/env python3
"""
Orchestration script template (PTC pattern, local/Claude Code adaptation).

Copy this file to scripts/<task_name>.py per task. Do not edit tool
implementations here -- only orchestrate calls into tools/example_tools.py
(or your real module) and do filtering/aggregation in this process.

Contract:
  - Only import functions from tools.registry.registry.namespace(), or
    explicitly from a tools module where the function is decorated
    orchestratable=True. Importing a direct-only tool here is a bug.
  - Parallelize independent calls with asyncio.gather.
  - Do all filtering / aggregation / arithmetic here, in Python.
  - Print ONLY a compact digest (JSON) to stdout. No raw record dumps.
  - Exit non-zero on unrecoverable error so the caller notices.
"""

from __future__ import annotations

import asyncio
import json
import sys

from tools.registry import registry

# Fail fast if this script is later edited to call a non-orchestratable tool.
NAMESPACE = registry.namespace()


async def run() -> dict:
    get_team_members = NAMESPACE["get_team_members"]
    get_expenses = NAMESPACE["get_expenses"]
    get_custom_budget = NAMESPACE["get_custom_budget"]

    department = "engineering"
    quarter = "Q3"
    standard_budget = 5000.0

    members = json.loads(await get_team_members(department))

    expense_results = await asyncio.gather(*(get_expenses(m["id"], quarter) for m in members))

    travel_totals: dict[str, float] = {}
    for member, raw in zip(members, expense_results):
        expenses = json.loads(raw)
        travel_totals[member["id"]] = sum(
            e["amount"] for e in expenses if e["category"] == "travel" and e["status"] == "approved"
        )

    over_standard = {
        emp_id: total for emp_id, total in travel_totals.items() if total > standard_budget
    }

    budget_results = await asyncio.gather(*(get_custom_budget(emp_id) for emp_id in over_standard))

    exceeded = []
    for emp_id, budget_raw in zip(over_standard, budget_results):
        budget = json.loads(budget_raw)
        limit = budget.get("travel_budget", standard_budget)
        total = travel_totals[emp_id]
        if total > limit:
            exceeded.append({"employee_id": emp_id, "spent": total, "limit": limit})

    return {"quarter": quarter, "department": department, "exceeded": exceeded}


if __name__ == "__main__":
    try:
        digest = asyncio.run(run())
    except Exception as exc:  # noqa: BLE001 - surface any failure to the caller
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        sys.exit(1)
    print(json.dumps(digest, indent=2))
