"""
The pipeline self-test endpoint is expensive and was the only ungoverned POST.

It generates 800 synthetic bars and runs the full feature build on a shared
executor. Every other mutating POST is rate limited -- resolve_approval,
set_risk_controls and the self-tuning trio via check_endpoint_rate_limit,
set_execution_mode via the stricter 3-per-hour mode limiter. This one was
not, so any holder of a valid key could keep the box building throwaway
matrices while the live tick loop waited for a thread.
"""

from __future__ import annotations

import ast
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException


def _request(ip: str = "10.0.0.1") -> MagicMock:
    request = MagicMock()
    request.client.host = ip
    return request


@pytest.mark.asyncio
async def test_the_limiter_is_consulted() -> None:
    from src.api.main import _state, debug_selftest

    with (
        patch.object(_state, "check_endpoint_rate_limit") as limiter,
        patch(
            "src.diagnostics.signal_debugger.run_pipeline_selftest",
            return_value={"passed": True},
        ),
    ):
        await debug_selftest(_request())
    limiter.assert_called_once()
    assert limiter.call_args.args[0] == "debug_selftest"


@pytest.mark.asyncio
async def test_it_is_keyed_by_client_ip() -> None:
    """One caller must not exhaust the budget for everyone else."""
    from src.api.main import _state, debug_selftest

    with (
        patch.object(_state, "check_endpoint_rate_limit") as limiter,
        patch(
            "src.diagnostics.signal_debugger.run_pipeline_selftest",
            return_value={"passed": True},
        ),
    ):
        await debug_selftest(_request("203.0.113.9"))
    assert limiter.call_args.args[1] == "203.0.113.9"


@pytest.mark.asyncio
async def test_the_expensive_work_never_runs_when_throttled() -> None:
    """Rejecting after the build would defeat the entire point."""
    from src.api.main import _state, debug_selftest

    with (
        patch.object(
            _state,
            "check_endpoint_rate_limit",
            side_effect=HTTPException(status_code=429, detail="slow down"),
        ),
        patch("src.diagnostics.signal_debugger.run_pipeline_selftest") as selftest,
        pytest.raises(HTTPException) as exc,
    ):
        await debug_selftest(_request())
    assert exc.value.status_code == 429
    selftest.assert_not_called()


@pytest.mark.asyncio
async def test_a_missing_client_does_not_crash_the_endpoint() -> None:
    from src.api.main import _state, debug_selftest

    request = MagicMock()
    request.client = None
    with (
        patch.object(_state, "check_endpoint_rate_limit") as limiter,
        patch(
            "src.diagnostics.signal_debugger.run_pipeline_selftest",
            return_value={"passed": True},
        ),
    ):
        await debug_selftest(request)
    assert limiter.call_args.args[1] == ""


def test_every_post_endpoint_is_governed_by_some_limiter() -> None:
    """
    The gap was a single unrated POST among six governed ones, which is the
    kind of omission that only shows up when the set is checked as a set.
    """
    with open("src/api/main.py", encoding="utf-8") as fh:
        tree = ast.parse(fh.read(), "src/api/main.py")
    ungoverned = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncFunctionDef):
            continue
        is_post = any(
            isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute) and d.func.attr == "post"
            for d in node.decorator_list
        )
        if not is_post:
            continue
        body = ast.dump(node)
        if "check_endpoint_rate_limit" not in body and "check_mode_change_rate_limit" not in body:
            ungoverned.append(node.name)
    assert not ungoverned, f"POST endpoints with no rate limiter: {ungoverned}"
