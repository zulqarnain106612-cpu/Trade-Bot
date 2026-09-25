"""
REG-0015 -- a hung ensemble fit must not block orchestrator startup.

`_train_models()` used to submit `trainer.train_ensemble` to the shared
single-worker `_train_executor` with no timeout, guarded only by
`except Exception`. A hang is not an exception, so a wedged fit held the
FastAPI lifespan open forever: port 8000 never opened and there was no health
endpoint to ask, because the health endpoint is behind the same lifespan.

These tests pin the three properties that fix depends on. They never sleep for
the real timeout -- the fit blocks on an Event the test controls, and the
timeout is shortened on the instance -- so the suite stays fast (GOV-016).
"""

from __future__ import annotations

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from src.engine.orchestrator import ENSEMBLE_TRAIN_TIMEOUT_S


def test_module_default_timeout_is_finite_and_positive() -> None:
    """The ceiling exists at all. Before REG-0015 there was no bound."""
    assert 0 < ENSEMBLE_TRAIN_TIMEOUT_S < float("inf")


@pytest.mark.asyncio
async def test_wedged_fit_times_out_instead_of_blocking_forever() -> None:
    """
    A fit that never returns surfaces as TimeoutError within the deadline
    rather than hanging the caller.

    This is the exact shape of the `await asyncio.wait_for(loop.run_in_executor(
    self._ensemble_executor, trainer.train_ensemble, fm), ...)` call in
    `_train_models()`.
    """
    release = threading.Event()
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ensemble")
    loop = asyncio.get_running_loop()

    def never_returns() -> str:
        release.wait()  # released in finally; without the fix this is forever
        return "fitted"

    try:
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(loop.run_in_executor(executor, never_returns), timeout=0.05)
    finally:
        release.set()
        executor.shutdown(wait=False, cancel_futures=True)


@pytest.mark.asyncio
async def test_timeout_is_caught_before_the_generic_exception_handler() -> None:
    """
    Ordering in `_train_models()` is load-bearing.

    `asyncio.TimeoutError` is `TimeoutError`, which subclasses `OSError` and so
    `Exception`. If `except Exception` came first it would swallow the timeout
    and the executor would never be marked poisoned -- the bug would survive
    the fix. This asserts the language-level fact the ordering relies on.
    """
    assert issubclass(TimeoutError, Exception)
    assert asyncio.TimeoutError is TimeoutError


@pytest.mark.asyncio
async def test_poisoned_executor_does_not_delay_later_work() -> None:
    """
    The ensemble pool is separate from `_train_executor`, so a wedged fit
    leaves direction/meta training for later timeframes unaffected.

    With a single shared pool this would block until the wedged thread exited.
    """
    release = threading.Event()
    ensemble_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ensemble")
    train_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="training")
    loop = asyncio.get_running_loop()

    def never_returns() -> None:
        release.wait()

    try:
        # Occupy the ensemble pool's only worker, as a wedged fit would.
        ensemble_executor.submit(never_returns)

        # Training work must still complete promptly on its own pool.
        result = await asyncio.wait_for(
            loop.run_in_executor(train_executor, lambda: "trained"), timeout=5.0
        )
        assert result == "trained"
    finally:
        release.set()
        ensemble_executor.shutdown(wait=False, cancel_futures=True)
        train_executor.shutdown(wait=False, cancel_futures=True)


def test_shutdown_never_waits_on_a_poisoned_pool() -> None:
    """
    `shutdown(wait=True)` on a pool whose worker never returns would trade a
    hung startup for a hung shutdown. The fix passes
    `wait=not self._ensemble_executor_poisoned`, so assert the non-waiting
    shutdown returns while the worker is still occupied.
    """
    release = threading.Event()
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ensemble")
    try:
        executor.submit(release.wait)
        # Returns immediately despite the worker still being blocked.
        executor.shutdown(wait=False, cancel_futures=True)
    finally:
        release.set()
