"""Runtime instrumentation to trace background task/process/thread starts.

This module installs lightweight wrappers for asyncio task creation, threading.Thread.start,
and multiprocessing.Process.start so we can capture stack traces and caller context for any
component that spawns background work. It is intended as temporary diagnostic instrumentation
— it is safe to leave in but can be disabled at runtime by setting the TRACE_STARTERS
environment variable to "false".

Usage: call install_instrumentation() early during process startup (e.g. in API lifespan()).

The wrappers are conservative: they log stack traces and basic context only.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import os
import threading
import traceback
from typing import Any

import structlog

log = structlog.get_logger(__name__)

_ENABLED = os.environ.get("TRACE_STARTERS", "true").lower() not in ("0", "false")


def _stack_snippet(max_frames: int = 12) -> str:
    """Return a compact stack snippet (exclude the last frame inside instrumentation)."""
    stack = traceback.format_stack(limit=max_frames + 2)
    # Drop the final frames that are inside this file's caller (the install function)
    if len(stack) > 2:
        stack = stack[:-2]
    return "".join(stack)


def _short_repr(obj: Any, limit: int = 240) -> str:
    try:
        s = repr(obj)
    except Exception:
        s = type(obj).__name__
    if len(s) > limit:
        return s[:limit] + "..."
    return s


def _install_asyncio_task_factory(loop: asyncio.AbstractEventLoop | None = None) -> None:
    loop = loop or asyncio.get_event_loop()

    with contextlib.suppress(Exception):
        loop.get_task_factory()

    def _task_factory(inner_loop: asyncio.AbstractEventLoop, coro: Any):
        # Create the task using the default behaviour (preserve origin factory if any)
        try:
            t = asyncio.Task(coro, loop=inner_loop)
        except Exception:
            # Fall back to loop.create_task() if Task() signature differs
            t = inner_loop.create_task(coro)
        # Never raise from instrumentation
        with contextlib.suppress(Exception):
            log.debug(
                "instrumentation.asyncio_task_created",
                task=_short_repr(t),
                coro=_short_repr(coro),
                stack=_stack_snippet(),
            )
        return t

    try:
        loop.set_task_factory(_task_factory)
        log.info("instrumentation.asyncio_task_factory_installed")
    except Exception as exc:
        log.warning("instrumentation.asyncio_task_factory_install_failed", error=str(exc))


def _wrap_thread_start() -> None:
    try:
        orig = threading.Thread.start
    except AttributeError:
        # The only way this lookup fails is a runtime without the attribute.
        return

    @functools.wraps(orig)
    def _start(self: threading.Thread, *a: Any, **kw: Any) -> Any:
        with contextlib.suppress(Exception):
            log.info(
                "instrumentation.thread_start",
                thread_name=getattr(self, "name", "<thread>"),
                target=_short_repr(getattr(self, "_target", None)),
                stack=_stack_snippet(),
            )
        return orig(self, *a, **kw)

    try:
        threading.Thread.start = _start  # type: ignore[assignment]
        log.info("instrumentation.thread_start_wrapper_installed")
    except Exception as exc:
        log.warning("instrumentation.thread_wrapper_install_failed", error=str(exc))


def _wrap_multiprocess_start() -> None:
    try:
        import multiprocessing

        orig = multiprocessing.Process.start
    except (ImportError, AttributeError):
        # multiprocessing is absent or reshaped on this runtime; instrumenting
        # it is optional, so leave process start unwrapped.
        return

    @functools.wraps(orig)
    def _mp_start(self: Any, *a: Any, **kw: Any) -> Any:
        # Observing a process start must never be able to prevent it, which is
        # why this is suppressed rather than logged — the logger is the thing
        # that just failed. Same shape as _wrap_thread_start above.
        with contextlib.suppress(Exception):
            # multiprocessing.Process stores target in _target for fork/spawn
            target = getattr(self, "_target", None)
            args = getattr(self, "_args", None)
            log.info(
                "instrumentation.process_start",
                process_name=getattr(self, "name", "<process>"),
                pid=getattr(self, "pid", None),
                target=_short_repr(target),
                args=_short_repr(args),
                stack=_stack_snippet(),
            )
        return orig(self, *a, **kw)

    try:
        multiprocessing.Process.start = _mp_start  # type: ignore[assignment]
        log.info("instrumentation.multiprocess_start_wrapper_installed")
    except Exception as exc:
        log.warning("instrumentation.multiprocess_wrapper_install_failed", error=str(exc))


def install_instrumentation(loop: asyncio.AbstractEventLoop | None = None) -> None:
    """Install all available instrumentation hooks (idempotent).

    By default this is enabled unless TRACE_STARTERS=false in the environment.
    """
    if not _ENABLED:
        log.info("instrumentation.disabled_by_env")
        return

    try:
        _install_asyncio_task_factory(loop)
    except Exception as exc:
        log.warning("instrumentation.failed_install_asyncio", error=str(exc))

    try:
        _wrap_thread_start()
    except Exception as exc:
        log.warning("instrumentation.failed_wrap_thread", error=str(exc))

    try:
        _wrap_multiprocess_start()
    except Exception as exc:
        log.warning("instrumentation.failed_wrap_multiprocess", error=str(exc))

    log.info("instrumentation.install_complete")


def log_manual_event(name: str, **kwargs: Any) -> None:
    """Log a manual instrumentation event (useful from component code)."""
    with contextlib.suppress(Exception):
        log.info("instrumentation.manual_event", name=name, stack=_stack_snippet(), **kwargs)
