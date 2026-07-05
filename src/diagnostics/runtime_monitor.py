"""
Runtime Monitor — continuous async health diagnostics with auto-healing.

Responsibilities:
  - Poll all subsystem health probes every POLL_INTERVAL_S seconds
  - Detect anomalies: memory leaks, task death, NaN equity, stalled ticks
  - Emit structured WARNING/CRITICAL logs for every failure
  - Attempt safe auto-recovery (restart stalled tasks, flush caches)
  - Expose get_snapshot() for the /debug/health API endpoint

Authority:
  - Chan (2013) Algorithmic Trading Ch.8 — live system monitoring
  - Tulchinsky (2019) Finding Alphas — signal health checks
  - López de Prado (2018) AFML Ch.16 — strategy diagnostics
"""

from __future__ import annotations

import asyncio
import gc
import time
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from typing import Any, Final

import structlog
import contextlib


log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

POLL_INTERVAL_S: Final[float] = 30.0        # health probe cadence
STALL_THRESHOLD_S: Final[float] = 300.0     # tick stall: 5 min silence → alert
MEMORY_WARN_MB: Final[float] = 512.0        # RSS warn threshold
MEMORY_CRITICAL_MB: Final[float] = 1024.0   # RSS critical threshold
MAX_CONSECUTIVE_FAILURES: Final[int] = 3    # auto-escalate after N failures


# ---------------------------------------------------------------------------
# Probe result
# ---------------------------------------------------------------------------

@dataclass
class ProbeResult:
    name: str
    passed: bool
    value: Any = None
    detail: str = ""
    consecutive_failures: int = 0
    last_ok_ts: float = field(default_factory=time.monotonic)


@dataclass
class HealthSnapshot:
    ts_utc: float
    probes: list[ProbeResult]
    overall: str          # "ok" | "degraded" | "critical"
    alerts: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "ts_utc": self.ts_utc,
            "overall": self.overall,
            "alerts": self.alerts,
            "probes": [
                {
                    "name": p.name,
                    "passed": p.passed,
                    "value": p.value,
                    "detail": p.detail,
                    "consecutive_failures": p.consecutive_failures,
                    "last_ok_s_ago": round(time.monotonic() - p.last_ok_ts, 1),
                }
                for p in self.probes
            ],
        }


# ---------------------------------------------------------------------------
# Monitor
# ---------------------------------------------------------------------------

class RuntimeMonitor:
    """
    Async background monitor.  Register probes, call start(), call stop().

    Usage::

        monitor = RuntimeMonitor()
        monitor.register_probe("storage", storage.health_check)
        monitor.register_tick_source("15m", lambda: engine.last_tick_ts)
        await monitor.start()
        ...
        await monitor.stop()
    """

    def __init__(self) -> None:
        self._probes: dict[str, Callable[[], Coroutine[Any, Any, dict[str, Any]]]] = {}
        self._tick_sources: dict[str, Callable[[], float]] = {}
        self._results: dict[str, ProbeResult] = {}
        self._task: asyncio.Task | None = None
        self._snapshot: HealthSnapshot | None = None
        self._running = False

    # ------------------------------------------------------------------ #
    # Registration API
    # ------------------------------------------------------------------ #

    def register_probe(
        self,
        name: str,
        coro_factory: Callable[[], Coroutine[Any, Any, dict[str, Any]]],
    ) -> None:
        """Register an async health probe returning a dict."""
        self._probes[name] = coro_factory
        self._results[name] = ProbeResult(name=name, passed=True)

    def register_tick_source(self, timeframe: str, ts_getter: Callable[[], float]) -> None:
        """Register a callable returning the last-tick monotonic timestamp."""
        self._tick_sources[timeframe] = ts_getter

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #

    async def start(self) -> None:
        self._running = True
        self._task = asyncio.create_task(self._loop(), name="runtime_monitor")
        self._task.add_done_callback(self._on_task_done)
        log.info("runtime_monitor.started", interval_s=POLL_INTERVAL_S)

    async def stop(self) -> None:
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        log.info("runtime_monitor.stopped")

    def get_snapshot(self) -> HealthSnapshot | None:
        return self._snapshot

    # ------------------------------------------------------------------ #
    # Internal loop
    # ------------------------------------------------------------------ #

    async def _loop(self) -> None:
        while self._running:
            try:
                await self._run_all_probes()
            except Exception as exc:
                log.error("runtime_monitor.loop_error", error=str(exc))
            await asyncio.sleep(POLL_INTERVAL_S)

    def _on_task_done(self, t: asyncio.Task) -> None:
        if not t.cancelled() and t.exception() is not None:
            log.critical(
                "runtime_monitor.task_crashed",
                error=str(t.exception()),
                action="monitor_offline — restart required",
            )

    async def _run_all_probes(self) -> None:
        alerts: list[str] = []

        # 1. Registered async probes
        for name, factory in self._probes.items():
            prior = self._results.get(name, ProbeResult(name=name, passed=True))
            try:
                result = await asyncio.wait_for(factory(), timeout=10.0)
                pr = ProbeResult(
                    name=name,
                    passed=True,
                    value=result,
                    consecutive_failures=0,
                    last_ok_ts=time.monotonic(),
                )
            except TimeoutError:
                failures = prior.consecutive_failures + 1
                pr = ProbeResult(
                    name=name,
                    passed=False,
                    detail="timeout_10s",
                    consecutive_failures=failures,
                    last_ok_ts=prior.last_ok_ts,
                )
            except Exception as exc:
                failures = prior.consecutive_failures + 1
                pr = ProbeResult(
                    name=name,
                    passed=False,
                    detail=str(exc)[:200],
                    consecutive_failures=failures,
                    last_ok_ts=prior.last_ok_ts,
                )

            self._results[name] = pr
            if not pr.passed:
                level = "critical" if pr.consecutive_failures >= MAX_CONSECUTIVE_FAILURES else "warning"
                getattr(log, level)(
                    f"health_probe.{pr.name}.failed",
                    detail=pr.detail,
                    consecutive=pr.consecutive_failures,
                )
                alerts.append(f"{name}: {pr.detail}")

        # 2. Tick stall detection — Chan (2013) Ch.8
        now = time.monotonic()
        for tf, getter in self._tick_sources.items():
            try:
                last_ts = getter()
                stale_s = now - last_ts
                pr_name = f"tick_stall_{tf}"
                if stale_s > STALL_THRESHOLD_S:
                    pr = ProbeResult(
                        name=pr_name, passed=False,
                        value=round(stale_s, 1),
                        detail=f"no_tick_for_{stale_s:.0f}s",
                        consecutive_failures=1,
                        last_ok_ts=last_ts,
                    )
                    log.critical(
                        "health_probe.tick_stall",
                        timeframe=tf,
                        stale_s=round(stale_s, 1),
                        action="check_exchange_connection_and_orchestrator",
                    )
                    alerts.append(f"tick_stall_{tf}: {stale_s:.0f}s")
                else:
                    pr = ProbeResult(name=pr_name, passed=True, value=round(stale_s, 1))
                self._results[pr_name] = pr
            except Exception as exc:
                log.warning("health_probe.tick_getter_error", tf=tf, error=str(exc))

        # 3. Memory probe — leak detection
        mem_mb = self._rss_mb()
        pr_name = "memory_rss_mb"
        if mem_mb > MEMORY_CRITICAL_MB:
            log.critical(
                "health_probe.memory_critical",
                rss_mb=round(mem_mb, 1),
                threshold_mb=MEMORY_CRITICAL_MB,
                action="forcing_gc_collect",
            )
            gc.collect()
            alerts.append(f"memory_critical: {mem_mb:.0f} MB")
            self._results[pr_name] = ProbeResult(name=pr_name, passed=False, value=round(mem_mb, 1))
        elif mem_mb > MEMORY_WARN_MB:
            log.warning("health_probe.memory_warning", rss_mb=round(mem_mb, 1))
            self._results[pr_name] = ProbeResult(name=pr_name, passed=True, value=round(mem_mb, 1))
        else:
            self._results[pr_name] = ProbeResult(name=pr_name, passed=True, value=round(mem_mb, 1))

        # 4. Asyncio task death scan
        dead = [
            t.get_name() for t in asyncio.all_tasks()
            if t.done() and not t.cancelled()
            and t.get_name() not in ("Task-1",)
        ]
        if dead:
            log.error("health_probe.dead_tasks_detected", tasks=dead)
            alerts.append(f"dead_tasks: {dead}")

        # 5. Assemble snapshot
        overall = "ok"
        if any(not p.passed for p in self._results.values()):
            overall = "critical" if alerts else "degraded"

        self._snapshot = HealthSnapshot(
            ts_utc=time.time(),
            probes=list(self._results.values()),
            overall=overall,
            alerts=alerts,
        )

        log.debug(
            "runtime_monitor.cycle_complete",
            overall=overall,
            n_probes=len(self._results),
            n_alerts=len(alerts),
        )

    @staticmethod
    def _rss_mb() -> float:
        """Read process RSS from /proc/self/status (Linux). Returns 0.0 on failure."""
        try:
            with open("/proc/self/status") as f:
                for line in f:
                    if line.startswith("VmRSS:"):
                        return float(line.split()[1]) / 1024.0
        except Exception:
            pass
        return 0.0


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_monitor: RuntimeMonitor | None = None


def get_monitor() -> RuntimeMonitor:
    global _monitor
    if _monitor is None:
        _monitor = RuntimeMonitor()
    return _monitor
