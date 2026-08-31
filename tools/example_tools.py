"""
Reference tools showing the orchestratable / direct-only split.
Replace bodies with real integrations (DB, REST API, MCP client, CLI wrapper).
Keep the decorator flags -- that's the safety boundary the skill enforces.
"""
from __future__ import annotations

import asyncio
from typing import Any

from tools.registry import tool

# --- Safe for batch/looped use from an orchestration script -----------------


@tool("get_team_members", orchestratable=True)
async def get_team_members(department: str) -> list[dict[str, Any]]:
    """Read-only, idempotent, cheap to repeat -> safe to call in a loop/gather."""
    await asyncio.sleep(0)  # placeholder for a real network/DB call
    raise NotImplementedError("wire this up to your real data source")


@tool("get_expenses", orchestratable=True)
async def get_expenses(employee_id: str, quarter: str) -> list[dict[str, Any]]:
    """Read-only; returns large, metadata-rich records -> exactly the PTC case."""
    await asyncio.sleep(0)
    raise NotImplementedError("wire this up to your real data source")


@tool("get_custom_budget", orchestratable=True)
async def get_custom_budget(user_id: str) -> dict[str, Any]:
    """Read-only lookup, depends on prior filtering -> safe for the second pass."""
    await asyncio.sleep(0)
    raise NotImplementedError("wire this up to your real data source")


# --- Direct-only: never call these from a loop/batch script -----------------


@tool("send_email", orchestratable=False)
def send_email(to: str, subject: str, body: str) -> None:
    """Side-effecting, irreversible -> must stay one-at-a-time, in-conversation."""
    raise NotImplementedError("wire this up to your real mail sender")


@tool("delete_record", orchestratable=False)
def delete_record(record_id: str) -> None:
    """Destructive -> must stay one-at-a-time, in-conversation, with confirmation."""
    raise NotImplementedError("wire this up to your real data store")
