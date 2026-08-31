"""Shared subprocess wrapper around `claude -p` (Claude Code headless mode).
Used by rag_mongo, orchestrator, and kg -- one implementation, one place to
fix auth/flag bugs instead of three copies drifting apart.

Auth: Pro/Max subscription via `claude /login` (OAuth). NEVER add --bare --
bare mode requires ANTHROPIC_API_KEY and does not use OAuth/subscription
auth; it would silently break every caller on a Pro/Max plan.

Every call is stateless (no --continue/--resume): context never accumulates
across calls -- each call's context is bounded to exactly its own prompt,
which is the core token/context discipline this whole repo depends on.
"""
from __future__ import annotations

import json
import shutil
import subprocess


class ClaudeCLIError(RuntimeError):
    pass


def _require_cli() -> None:
    if shutil.which("claude") is None:
        raise ClaudeCLIError(
            "Claude Code CLI not found. Install: npm i -g @anthropic-ai/claude-code "
            "then run `claude /login` once (Pro/Max subscription)."
        )


def run_claude(
    prompt: str,
    model: str,
    allowed_tools: str | None = None,
    max_turns: int | None = None,
    json_schema: dict | None = None,
    timeout: int = 180,
) -> dict:
    """One-shot, stateless call to `claude -p`.

    Returns: {"result": str, "structured_output": dict|None,
              "cost_usd": float, "session_id": str|None}
    """
    _require_cli()
    cmd = ["claude", "-p", prompt, "--model", model, "--output-format", "json"]
    if allowed_tools:
        cmd += ["--allowedTools", allowed_tools]
    if json_schema:
        # --json-schema drives structured output through an internal
        # tool-use round-trip the CLI manages itself. Capping --max-turns
        # here truncates that round-trip before it completes (exit 1,
        # stdout left as the raw in-progress event array) -- so let it
        # run to its own completion instead of bounding it.
        cmd += ["--json-schema", json.dumps(json_schema)]
    elif max_turns:
        cmd += ["--max-turns", str(max_turns)]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        raise ClaudeCLIError(f"claude -p failed (exit {proc.returncode}): {proc.stderr[:500]}")
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise ClaudeCLIError(f"Could not parse claude output as JSON: {proc.stdout[:500]}") from exc
    # --output-format json returns a single result object for a plain
    # text turn, but a JSON array of the full event stream (final result
    # last) whenever a tool call happened -- which --json-schema always
    # triggers via its internal structured-output tool.
    if isinstance(payload, list):
        payload = payload[-1] if payload else {}
    return {
        "result": payload.get("result", ""),
        "structured_output": payload.get("structured_output"),
        "cost_usd": payload.get("total_cost_usd", 0.0),
        "session_id": payload.get("session_id"),
    }
