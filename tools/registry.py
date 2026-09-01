"""
Local adaptation of Anthropic's Programmatic Tool Calling (PTC) `allowed_callers`
concept for Claude Code (no Messages API, no code_execution_20250825 server tool).

Reference: https://platform.claude.com/cookbook/tool-use-programmatic-tool-calling-ptc

Usage:

    from tools.registry import tool

    @tool("get_team_members", orchestratable=True)
    def get_team_members(department: str) -> list[dict]: ...

    @tool("send_email", orchestratable=False)
    def send_email(to: str, body: str) -> None: ...

Only functions registered with orchestratable=True may be imported into a
script under scripts/ (see registry.namespace()). Everything else must be
called one at a time, in-conversation.
"""

from __future__ import annotations

import functools
import inspect
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("ptc.registry")


@dataclass(frozen=True)
class ToolSpec:
    name: str
    fn: Callable[..., Any]
    orchestratable: bool
    description: str
    is_async: bool = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "is_async", inspect.iscoroutinefunction(self.fn))


class ToolRegistry:
    """Process-wide registry of tools, partitioned by orchestratable flag."""

    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}

    def register(
        self,
        name: str,
        fn: Callable[..., Any],
        orchestratable: bool,
        description: str = "",
    ) -> None:
        if name in self._tools:
            raise ValueError(f"Tool '{name}' is already registered")
        self._tools[name] = ToolSpec(
            name=name,
            fn=fn,
            orchestratable=orchestratable,
            description=description or (inspect.getdoc(fn) or ""),
        )
        logger.debug("registered tool=%s orchestratable=%s", name, orchestratable)

    def get(self, name: str) -> ToolSpec:
        try:
            return self._tools[name]
        except KeyError as exc:
            raise KeyError(
                f"Unknown tool '{name}'. Registered tools: {sorted(self._tools)}"
            ) from exc

    def list_orchestratable(self) -> dict[str, ToolSpec]:
        return {n: t for n, t in self._tools.items() if t.orchestratable}

    def list_direct_only(self) -> dict[str, ToolSpec]:
        return {n: t for n, t in self._tools.items() if not t.orchestratable}

    def assert_orchestratable(self, name: str) -> ToolSpec:
        """Raise if a script tries to batch-call a tool that isn't opted in."""
        spec = self.get(name)
        if not spec.orchestratable:
            raise PermissionError(
                f"Tool '{name}' is not orchestratable=True. It must be called "
                "one at a time, in-conversation, never from a loop/batch script "
                "(e.g. it is destructive, rate-limited, or needs human review)."
            )
        return spec

    def namespace(self) -> dict[str, Callable[..., Any]]:
        """{name: fn} for every orchestratable tool -- safe to import into scripts."""
        return {n: t.fn for n, t in self.list_orchestratable().items()}


registry = ToolRegistry()


def tool(name: str, orchestratable: bool = False, description: str = ""):
    """Decorator: register `fn` under `name`, flagging it for PTC-style batch use."""

    def _wrap(fn: Callable[..., Any]) -> Callable[..., Any]:
        registry.register(name, fn, orchestratable=orchestratable, description=description)

        @functools.wraps(fn)
        def _inner(*args: Any, **kwargs: Any) -> Any:
            return fn(*args, **kwargs)

        return _inner

    return _wrap
