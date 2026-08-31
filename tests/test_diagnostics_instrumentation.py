"""Tests for src/diagnostics/instrumentation.py -- background-task tracing.

_wrap_thread_start/_wrap_multiprocess_start mutate real class attributes
(threading.Thread.start, multiprocessing.Process.start), so every test that
exercises them does so inside patch.object(...), which restores the
original attribute on exit regardless of what the function under test
reassigned it to.
"""

from __future__ import annotations

import asyncio
import multiprocessing
import sys
import threading
from unittest.mock import MagicMock, patch

import src.diagnostics.instrumentation as instrumentation
from src.diagnostics.instrumentation import (
    _install_asyncio_task_factory,
    _short_repr,
    _stack_snippet,
    _wrap_multiprocess_start,
    _wrap_thread_start,
    install_instrumentation,
    log_manual_event,
)


def test_stack_snippet_default_returns_nonempty_and_trims_last_frames():
    snippet = _stack_snippet()
    assert isinstance(snippet, str) and snippet


def test_stack_snippet_short_stack_skips_trim_branch():
    # limit = max_frames + 2 = 2 -> at most 2 frames, so len(stack) > 2 is False
    snippet = _stack_snippet(max_frames=0)
    assert isinstance(snippet, str)


def test_short_repr_normal_object():
    assert _short_repr([1, 2, 3]) == "[1, 2, 3]"


def test_short_repr_truncates_long_repr():
    s = _short_repr("x" * 500, limit=10)
    assert s.endswith("...")
    assert len(s) == 13


def test_short_repr_falls_back_to_type_name_when_repr_raises():
    class _Bad:
        def __repr__(self):
            raise RuntimeError("no repr")

    assert _short_repr(_Bad()) == "_Bad"


def test_install_asyncio_task_factory_success_path():
    mock_loop = MagicMock()
    _install_asyncio_task_factory(loop=mock_loop)
    mock_loop.set_task_factory.assert_called_once()
    factory = mock_loop.set_task_factory.call_args[0][0]

    inner_loop = MagicMock()
    result = factory(inner_loop, object())
    # asyncio.Task(coro, loop=MagicMock()) fails internally -> falls back to
    # inner_loop.create_task, exercising the except branch.
    inner_loop.create_task.assert_called_once()
    assert result is inner_loop.create_task.return_value


def test_install_asyncio_task_factory_set_task_factory_failure_is_caught():
    mock_loop = MagicMock()
    mock_loop.set_task_factory.side_effect = RuntimeError("no loop")
    _install_asyncio_task_factory(loop=mock_loop)  # must not raise


async def test_install_asyncio_task_factory_defaults_to_running_loop():
    running_loop = asyncio.get_running_loop()
    with patch.object(running_loop, "set_task_factory") as mock_set:
        _install_asyncio_task_factory()
    mock_set.assert_called_once()


def test_wrap_thread_start_installs_wrapper_that_delegates():
    with patch.object(threading.Thread, "start", MagicMock(return_value="orig-result")) as orig:
        _wrap_thread_start()
        t = threading.Thread(target=lambda: None)
        result = t.start()
    orig.assert_called_once()
    assert result == "orig-result"


def test_wrap_multiprocess_start_missing_module_is_a_noop():
    with patch.dict(sys.modules, {"multiprocessing": None}):
        _wrap_multiprocess_start()  # must not raise


def test_wrap_multiprocess_start_installs_wrapper_that_delegates():
    with patch.object(
        multiprocessing.Process, "start", MagicMock(return_value="mp-result")
    ) as orig:
        _wrap_multiprocess_start()
        p = multiprocessing.Process(target=lambda: None)
        result = p.start()
    orig.assert_called_once()
    assert result == "mp-result"


def test_install_instrumentation_disabled_by_env_skips_everything():
    with (
        patch.object(instrumentation, "_ENABLED", False),
        patch.object(instrumentation, "_install_asyncio_task_factory") as mock_asyncio,
        patch.object(instrumentation, "_wrap_thread_start") as mock_thread,
        patch.object(instrumentation, "_wrap_multiprocess_start") as mock_mp,
    ):
        install_instrumentation()
    mock_asyncio.assert_not_called()
    mock_thread.assert_not_called()
    mock_mp.assert_not_called()


def test_install_instrumentation_enabled_calls_all_installers():
    with (
        patch.object(instrumentation, "_ENABLED", True),
        patch.object(instrumentation, "_install_asyncio_task_factory") as mock_asyncio,
        patch.object(instrumentation, "_wrap_thread_start") as mock_thread,
        patch.object(instrumentation, "_wrap_multiprocess_start") as mock_mp,
    ):
        install_instrumentation()
    mock_asyncio.assert_called_once()
    mock_thread.assert_called_once()
    mock_mp.assert_called_once()


def test_install_instrumentation_survives_each_installer_failing():
    with (
        patch.object(instrumentation, "_ENABLED", True),
        patch.object(
            instrumentation,
            "_install_asyncio_task_factory",
            side_effect=RuntimeError("boom"),
        ),
        patch.object(instrumentation, "_wrap_thread_start", side_effect=RuntimeError("boom")),
        patch.object(instrumentation, "_wrap_multiprocess_start", side_effect=RuntimeError("boom")),
    ):
        install_instrumentation()  # must not raise


def test_log_manual_event_does_not_raise():
    log_manual_event("custom.event", extra="value")
