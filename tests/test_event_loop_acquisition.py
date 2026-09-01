"""No production path may call `asyncio.get_event_loop()`.

Inside a coroutine it returns the running loop, but outside one it is
deprecated on 3.10+ and raises `RuntimeError: There is no current event loop`
on 3.12+ instead of creating one. The repo pins `requires-python >=3.11,<3.12`
while the dev venv here is newer, so the difference is invisible in CI and
fatal locally -- exactly the kind of gap this asserts away.
"""

from __future__ import annotations

import ast
import asyncio
import pathlib

import pytest


_SRC = pathlib.Path(__file__).resolve().parent.parent / "src"


def _get_event_loop_calls(path: pathlib.Path) -> list[int]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "get_event_loop"
    ]


def test_no_source_file_calls_get_event_loop():
    offenders = {
        str(path.relative_to(_SRC)): lines
        for path in sorted(_SRC.rglob("*.py"))
        if (lines := _get_event_loop_calls(path))
    }
    assert offenders == {}


def test_instrumentation_returns_when_there_is_no_loop_to_instrument():
    """It used to reach get_event_loop() here, which raises on 3.12+."""
    from src.diagnostics.instrumentation import _install_asyncio_task_factory

    with pytest.raises(RuntimeError):
        asyncio.get_running_loop()  # precondition: no loop in this thread

    assert _install_asyncio_task_factory() is None


@pytest.mark.asyncio
async def test_instrumentation_installs_onto_the_running_loop():
    from src.diagnostics.instrumentation import _install_asyncio_task_factory

    loop = asyncio.get_running_loop()
    _install_asyncio_task_factory()

    assert loop.get_task_factory() is not None
