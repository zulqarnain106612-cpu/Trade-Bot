"""Covers src/workers/trader.py and src/workers/__main__.py.

The headless worker is the trading loop without the HTTP surface, so what
matters is the boot order and -- more than that -- that the loop is always
handed back cleanly: `orchestrator.shutdown()` takes the closing equity
snapshot and `storage.close()` releases the DuckDB handle. Every exit the
module can take is asserted here, including the two that are failures:

  * a stop signal arrives            -> stop, shutdown, close
  * the orchestrator returns alone   -> stop, shutdown, close
  * the orchestrator raises          -> the error propagates, and shutdown
                                        and close still run
  * `startup()` raises               -> storage closed, error propagates,
                                        no shutdown (nothing was started)

Collaborators are patched at `src.workers.trader`, so no settings file,
storage backend, exchange fetcher or orchestrator is built. Nothing waits on
wall-clock time: the test is released by the fake orchestrator's own events.
"""

from __future__ import annotations

import asyncio
import contextlib
import runpy
import signal
from collections.abc import AsyncIterator, Iterator
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from src.workers import trader


class FakeStorage:
    """Records the two calls the worker owes the storage backend."""

    def __init__(self) -> None:
        self.initialized = False
        self.closed = 0

    async def initialize(self) -> None:
        self.initialized = True

    async def close(self) -> None:
        self.closed += 1


class FakeOrchestrator:
    """An orchestrator that exits the way each test needs it to."""

    def __init__(
        self,
        storage: FakeStorage,
        fetcher: object,
        *,
        startup_error: BaseException | None = None,
        run_error: BaseException | None = None,
        run_returns: bool = False,
    ) -> None:
        self.storage = storage
        self.fetcher = fetcher
        self._startup_error = startup_error
        self._run_error = run_error
        self._run_returns = run_returns
        self.started = asyncio.Event()
        self._stopped = asyncio.Event()
        self.stop_calls = 0
        self.shutdown_calls = 0

    async def startup(self) -> None:
        if self._startup_error is not None:
            raise self._startup_error
        self.started.set()

    async def run(self) -> None:
        if self._run_error is not None:
            raise self._run_error
        if self._run_returns:
            return
        await self._stopped.wait()

    def stop(self) -> None:
        self.stop_calls += 1
        self._stopped.set()

    async def shutdown(self) -> None:
        self.shutdown_calls += 1


def _orchestrators(**behaviour: object) -> tuple[object, list[FakeOrchestrator], asyncio.Event]:
    """A stand-in for the `Orchestrator` class, plus what it built."""
    made: list[FakeOrchestrator] = []
    built = asyncio.Event()

    def build(storage: FakeStorage, fetcher: object) -> FakeOrchestrator:
        orchestrator = FakeOrchestrator(storage, fetcher, **behaviour)  # type: ignore[arg-type]
        made.append(orchestrator)
        built.set()
        return orchestrator

    return build, made, built


@contextlib.contextmanager
def _patched(build: object) -> Iterator[SimpleNamespace]:
    settings = SimpleNamespace(
        strategy_portfolio=object(),
        trading_mode=SimpleNamespace(value="paper"),
    )
    storage = FakeStorage()
    fetcher = object()

    @contextlib.asynccontextmanager
    async def fake_open_fetcher(given: FakeStorage) -> AsyncIterator[object]:
        assert given is storage
        yield fetcher

    with (
        patch.object(trader, "get_settings", return_value=settings),
        patch.object(trader, "register_default_strategies") as register,
        patch.object(trader, "create_storage_backend", return_value=storage),
        patch.object(trader, "open_fetcher", fake_open_fetcher),
        patch.object(trader, "Orchestrator", build),
    ):
        yield SimpleNamespace(
            settings=settings,
            storage=storage,
            fetcher=fetcher,
            register=register,
        )


class TestTheWorkerBootsAndHandsBackCleanly:
    async def test_a_stop_signal_shuts_the_orchestrator_and_the_storage_down(self):
        build, made, built = _orchestrators()
        handlers: dict[int, tuple[object, tuple[object, ...]]] = {}
        loop = asyncio.get_running_loop()

        def capture(sig: int, callback: object, *args: object) -> None:
            handlers[sig] = (callback, args)

        with _patched(build) as env, patch.object(loop, "add_signal_handler", capture):
            task = asyncio.create_task(trader._run())
            await asyncio.wait_for(built.wait(), timeout=5)
            await asyncio.wait_for(made[0].started.wait(), timeout=5)

            # Registered before startup, so both are armed by now.
            assert set(handlers) == {signal.SIGINT, signal.SIGTERM}
            callback, args = handlers[signal.SIGTERM]
            callback(*args)  # type: ignore[operator]

            await asyncio.wait_for(task, timeout=5)

        env.register.assert_called_once_with(cfg=env.settings.strategy_portfolio)
        assert env.storage.initialized is True
        assert made[0].fetcher is env.fetcher
        assert made[0].stop_calls == 1
        assert made[0].shutdown_calls == 1
        assert env.storage.closed == 1

    async def test_an_orchestrator_that_returns_on_its_own_is_still_shut_down(self):
        build, made, _built = _orchestrators(run_returns=True)

        with _patched(build) as env:
            await asyncio.wait_for(trader._run(), timeout=5)

        assert made[0].shutdown_calls == 1
        assert env.storage.closed == 1

    async def test_a_platform_without_signal_handlers_still_runs_the_loop(self):
        build, made, _built = _orchestrators(run_returns=True)
        loop = asyncio.get_running_loop()

        def refuse(*_args: object, **_kwargs: object) -> None:
            raise NotImplementedError

        with _patched(build) as env, patch.object(loop, "add_signal_handler", refuse):
            await asyncio.wait_for(trader._run(), timeout=5)

        assert made[0].shutdown_calls == 1
        assert env.storage.closed == 1


class TestTheWorkerCleansUpWhenTheLoopFails:
    async def test_an_orchestrator_crash_propagates_but_cleanup_runs_first(self):
        # The crash is re-raised by the wait in the finally block as well as by
        # the block that detects it; neither may cost the shutdown or the close.
        build, made, _built = _orchestrators(run_error=RuntimeError("orchestrator died"))

        with _patched(build) as env, pytest.raises(RuntimeError, match="orchestrator died"):
            await asyncio.wait_for(trader._run(), timeout=5)

        assert made[0].stop_calls == 1
        assert made[0].shutdown_calls == 1
        assert env.storage.closed == 1

    async def test_a_startup_failure_closes_the_storage_and_propagates(self):
        build, made, _built = _orchestrators(startup_error=RuntimeError("no boot"))

        with _patched(build) as env, pytest.raises(RuntimeError, match="no boot"):
            await asyncio.wait_for(trader._run(), timeout=5)

        # Nothing was started, so there is nothing to shut down -- but the
        # storage handle was already open and must not leak.
        assert env.storage.closed == 1
        assert made[0].shutdown_calls == 0


class TestTheEntrypoint:
    def test_main_configures_logging_before_it_runs_the_loop(self):
        settings = SimpleNamespace(log_level="INFO", log_as_json=False)
        order: list[str] = []

        async def fake_run() -> None:
            order.append("run")

        with (
            patch.object(trader, "get_settings", return_value=settings),
            patch.object(trader, "configure_logging", side_effect=lambda _s: order.append("log")),
            patch.object(trader, "_run", fake_run),
        ):
            trader.main()

        assert order == ["log", "run"]

    def test_main_exits_quietly_on_a_keyboard_interrupt(self):
        settings = SimpleNamespace(log_level="INFO", log_as_json=False)

        async def interrupted() -> None:
            raise KeyboardInterrupt

        with (
            patch.object(trader, "get_settings", return_value=settings),
            patch.object(trader, "configure_logging"),
            patch.object(trader, "_run", interrupted),
        ):
            trader.main()  # must not raise -- cleanup already ran

    def test_the_package_runs_main_when_executed_as_a_script(self):
        # runpy re-executes __main__, so it binds whatever src.workers.trader
        # exposes at that moment.
        with patch("src.workers.trader.main") as main:
            runpy.run_module("src.workers", run_name="__main__")

        assert main.call_count == 1
