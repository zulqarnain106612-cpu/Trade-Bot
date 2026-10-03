"""
Headless trader worker entrypoint.

Runs the same `src.engine.orchestrator.Orchestrator` that
`src/api/main.py`'s FastAPI lifespan runs, minus the HTTP surface. Use this
when you want the trading loop as its own process — for example, a compose
`worker:` service running independently of the API replica.

Boot sequence mirrors the API lifespan (see `src/api/main.py`,
`_lifespan`):

  1. Load settings, configure logging.
  2. Populate the strategy registry (`register_default_strategies`).
  3. Build the storage backend, initialize it.
  4. Open the market-data fetcher via `open_fetcher`.
  5. Construct `Orchestrator(storage, fetcher)`, call `startup()`, then
     `run()` (blocks until `stop()` is invoked).
  6. On SIGINT / SIGTERM: `stop()`, then `shutdown()` for a clean exit and
     an equity snapshot on the way out.

Deliberately omitted, since neither is required to run the loop:

  * IntelligenceAdapter autostart (`INTEL_ENABLED=true` in the API path).
  * AutoTuningScheduler (`self_tuning.enabled`).

Either can be added here later without changing the entrypoint contract.

Run with::

    uv run python -m src.workers

Environment variables and defaults are documented in `.env.example`.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal

import structlog

from src.config import get_settings
from src.data.fetcher import open_fetcher
from src.data.storage import create_storage_backend
from src.engine.orchestrator import Orchestrator
from src.logging_setup import configure_logging
from src.strategies.bootstrap import register_default_strategies

log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)


async def _run() -> None:
    settings = get_settings()

    register_default_strategies(cfg=settings.strategy_portfolio)

    storage = create_storage_backend()
    await storage.initialize()

    async with open_fetcher(storage) as fetcher:
        orchestrator = Orchestrator(storage, fetcher)

        stop_event = asyncio.Event()

        def _handle_signal(signame: str) -> None:
            log.info("worker.signal_received", signal=signame)
            stop_event.set()

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            # Signal handlers are not supported on this platform (e.g.
            # Windows). Fall back to letting KeyboardInterrupt propagate
            # from asyncio.run().
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, _handle_signal, sig.name)

        try:
            await orchestrator.startup()
        except Exception:
            log.critical("worker.startup_failed", exc_info=True)
            await storage.close()
            raise

        log.info(
            "worker.startup_complete",
            trading_mode=settings.trading_mode.value,
        )

        orch_task = asyncio.create_task(orchestrator.run(), name="orchestrator")

        try:
            # Wait for either the orchestrator to exit on its own (crash /
            # unexpected termination) or a shutdown signal to arrive.
            stop_waiter = asyncio.create_task(stop_event.wait(), name="stop_waiter")
            done, _pending = await asyncio.wait(
                {orch_task, stop_waiter},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if orch_task in done and not stop_event.is_set():
                # The orchestrator returned without a stop request — surface
                # the reason (may be an exception) instead of silently
                # exiting 0.
                exc = orch_task.exception()
                if exc is not None:
                    log.critical("worker.orchestrator_crashed", exc_info=exc)
                    raise exc
                log.warning("worker.orchestrator_exited_unexpectedly")
        finally:
            orchestrator.stop()
            try:
                await asyncio.wait_for(orch_task, timeout=10.0)
            except TimeoutError:
                orch_task.cancel()
                # Nothing raised here is actionable: the task is already being
                # abandoned, and the shutdown below must run regardless.
                with contextlib.suppress(BaseException):
                    await orch_task
            await orchestrator.shutdown()
            await storage.close()
            log.info("worker.shutdown_complete")


def main() -> None:
    settings = get_settings()
    configure_logging(settings)
    # asyncio.run() re-raises KeyboardInterrupt after cancelling the
    # coroutine — the finally block above has already run cleanup by
    # then. Nothing more to do; exit 0.
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(_run())


if __name__ == "__main__":
    main()
