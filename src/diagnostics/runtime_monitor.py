"""
Runtime Monitor — continuous async health diagnostics (alert-only, no auto-restart).

Responsibilities:
  - Poll all subsystem health probes every POLL_INTERVAL_S seconds
  - Detect anomalies: memory leaks, task death, NaN equity, stalled ticks
  - Emit structured WARNING/CRITICAL logs for every failure
  - Alert only — no auto-restart (manual operator intervention required for crashed tasks)
  - Expose get_snapshot() for the /debug/health API endpoint

Authority:
  - Chan (2013) Algorithmic Trading Ch.8 — live system monitoring
  - Tulchinsky (2019) Finding Alphas — signal health checks
  - López de Prado (2018) AFML Ch.16 — strategy diagnostics

Registry: REL-004, REL-005, REL-007, RES-001 (config/quality_registry.json).
"""

from __future__ import annotations

import asyncio
import contextlib
import gc
import math
import time
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Final

import structlog

from src.eventbus import get_event_bus

log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

POLL_INTERVAL_S: Final[float] = 30.0  # health probe cadence
STALL_THRESHOLD_S: Final[float] = 300.0  # minimum tick stall threshold
TICK_SCHEDULE_GRACE_S: Final[float] = 60.0  # scheduler/network delay after each bar
MEMORY_WARN_MB: Final[float] = 512.0  # RSS warn threshold
MEMORY_CRITICAL_MB: Final[float] = 1024.0  # RSS critical threshold
MAX_CONSECUTIVE_FAILURES: Final[int] = 3  # auto-escalate after N failures


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
    overall: str  # "ok" | "degraded" | "critical"
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
        self._tick_registered_at: dict[str, float] = {}
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
        self._tick_registered_at[timeframe] = time.monotonic()

    def mark_tick_source_active(self, timeframe: str) -> None:
        """Start first-tick grace when the timeframe loop becomes runnable."""
        if timeframe not in self._tick_sources:
            raise KeyError(f"tick source is not registered: {timeframe}")
        # Initial model fitting can take several minutes. A tick source is not
        # expected to advance until its loop starts after startup training.
        self._tick_registered_at[timeframe] = time.monotonic()

    @staticmethod
    def _tick_stall_limit_s(timeframe: str) -> float:
        """Use the larger of the base stall window or timeframe cadence plus grace."""
        unit = timeframe[-1:].lower()
        try:
            amount = int(timeframe[:-1])
        except (TypeError, ValueError):
            return STALL_THRESHOLD_S
        multipliers = {"s": 1, "m": 60, "h": 3600, "d": 86400}
        if amount <= 0 or unit not in multipliers:
            return STALL_THRESHOLD_S
        return max(
            STALL_THRESHOLD_S,
            amount * multipliers[unit] + TICK_SCHEDULE_GRACE_S,
        )

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
                log.error("runtime_monitor.loop_error", error=str(exc), exc_info=True)
            await asyncio.sleep(POLL_INTERVAL_S)

    def _on_task_done(self, t: asyncio.Task) -> None:
        if not t.cancelled() and t.exception() is not None:
            log.critical(
                "runtime_monitor.task_crashed",
                error=str(t.exception()),
                action="monitor_offline — alert only, manual restart required",
            )

    async def _run_probe(self, name: str) -> ProbeResult:
        """Run one registered probe, converting any failure into a result.

        Returns rather than raises so a gather over all probes cannot let one
        failure mask another's outcome.
        """
        prior = self._results.get(name, ProbeResult(name=name, passed=True))
        try:
            result = await asyncio.wait_for(self._probes[name](), timeout=10.0)
        except TimeoutError:
            return ProbeResult(
                name=name,
                passed=False,
                detail="timeout_10s",
                consecutive_failures=prior.consecutive_failures + 1,
                last_ok_ts=prior.last_ok_ts,
            )
        except Exception as exc:
            log.warning("health_probe.exception", name=name, error=str(exc), exc_info=True)
            return ProbeResult(
                name=name,
                passed=False,
                detail=str(exc)[:200],
                consecutive_failures=prior.consecutive_failures + 1,
                last_ok_ts=prior.last_ok_ts,
            )
        return ProbeResult(
            name=name,
            passed=True,
            value=result,
            consecutive_failures=0,
            last_ok_ts=time.monotonic(),
        )

    async def _run_all_probes(self) -> None:
        alerts: list[str] = []

        # 1. Registered async probes, concurrently.
        #
        # Run sequentially these compounded: every probe carries its own 10s
        # timeout, so n hung probes cost n * 10s for a single cycle while
        # POLL_INTERVAL_S assumes a cycle is short. The monitor's own latency
        # therefore degraded in proportion to how much was broken -- the
        # snapshot went stalest exactly during the incident it exists to
        # describe. Concurrently the cycle is bounded by the slowest probe.
        names = list(self._probes)
        gathered = await asyncio.gather(
            *(self._run_probe(name) for name in names),
            return_exceptions=True,
        )

        for name, pr in zip(names, gathered, strict=True):
            if isinstance(pr, BaseException):
                # _run_probe catches per-probe failures itself; reaching here
                # means the wrapper broke, which must not lose the probe.
                log.error(
                    "runtime_monitor.probe_wrapper_failed",
                    name=name,
                    error=str(pr),
                )
                prior = self._results.get(name, ProbeResult(name=name, passed=True))
                pr = ProbeResult(
                    name=name,
                    passed=False,
                    detail=f"probe_wrapper_failed: {str(pr)[:150]}",
                    consecutive_failures=prior.consecutive_failures + 1,
                    last_ok_ts=prior.last_ok_ts,
                )

            self._results[name] = pr
            if not pr.passed:
                level = (
                    "critical" if pr.consecutive_failures >= MAX_CONSECUTIVE_FAILURES else "warning"
                )
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
                last_ts = float(getter())
                pr_name = f"tick_stall_{tf}"
                stall_limit_s = self._tick_stall_limit_s(tf)

                if not math.isfinite(last_ts):
                    pr = ProbeResult(
                        name=pr_name,
                        passed=False,
                        value=None,
                        detail="invalid_tick_timestamp",
                        consecutive_failures=1,
                    )
                    log.error("health_probe.invalid_tick_timestamp", timeframe=tf)
                    alerts.append(f"tick_stall_{tf}: invalid_tick_timestamp")
                    self._results[pr_name] = pr
                    continue

                # Zero means no tick has happened. Measure from registration, not
                # from process start (monotonic zero is not a real first-tick time).
                if last_ts <= 0.0:
                    registered_at = self._tick_registered_at.get(tf, now)
                    waiting_s = max(0.0, now - registered_at)
                    if waiting_s > stall_limit_s:
                        pr = ProbeResult(
                            name=pr_name,
                            passed=False,
                            value=round(waiting_s, 1),
                            detail=f"first_tick_not_seen_for_{waiting_s:.0f}s",
                            consecutive_failures=1,
                            last_ok_ts=registered_at,
                        )
                        log.critical(
                            "health_probe.first_tick_missing",
                            timeframe=tf,
                            waiting_s=round(waiting_s, 1),
                            threshold_s=stall_limit_s,
                        )
                        alerts.append(f"tick_stall_{tf}: first_tick_not_seen_for_{waiting_s:.0f}s")
                    else:
                        pr = ProbeResult(
                            name=pr_name,
                            passed=True,
                            value=round(waiting_s, 1),
                            detail="awaiting_first_tick",
                            last_ok_ts=registered_at,
                        )
                    self._results[pr_name] = pr
                    continue

                stale_s = max(0.0, now - last_ts)
                if stale_s > stall_limit_s:
                    pr = ProbeResult(
                        name=pr_name,
                        passed=False,
                        value=round(stale_s, 1),
                        detail=f"no_tick_for_{stale_s:.0f}s",
                        consecutive_failures=1,
                        last_ok_ts=last_ts,
                    )
                    log.critical(
                        "health_probe.tick_stall",
                        timeframe=tf,
                        stale_s=round(stale_s, 1),
                        threshold_s=stall_limit_s,
                        action="check_exchange_connection_and_orchestrator",
                    )
                    alerts.append(f"tick_stall_{tf}: {stale_s:.0f}s")
                else:
                    pr = ProbeResult(name=pr_name, passed=True, value=round(stale_s, 1))
                self._results[pr_name] = pr
            except Exception as exc:
                log.warning("health_probe.tick_getter_error", tf=tf, error=str(exc), exc_info=True)

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
            t.get_name()
            for t in asyncio.all_tasks()
            if t.done() and not t.cancelled() and t.get_name() not in ("Task-1",)
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

        # Every completed cycle, not only the ones that changed something.
        #
        # This looks like it should be transition-only, and it is not: the
        # snapshot is the *entire* answer /debug/health gives, and the panel
        # that replaces that poll has no other source. Publishing on change
        # alone leaves an operator unable to distinguish "still ok" from
        # "the monitor died and the last thing it said was ok" -- which is
        # precisely the state this module exists to make visible. One frame
        # per POLL_INTERVAL_S is the cheapest possible liveness proof, and
        # it replaces a 15s poll that re-read this same object twice per
        # cycle to get the same bytes.
        get_event_bus().publish("health", self._snapshot.to_dict())

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
            with open("/proc/self/status", encoding="utf-8") as f:
                for line in f:
                    if line.startswith("VmRSS:"):
                        return float(line.split()[1]) / 1024.0
        except Exception as exc:
            # UI-013: a 0.0 RSS reading is indistinguishable from "healthy"
            # to the memory-leak probe that consumes this -- log so a read
            # failure (permissions, unexpected /proc format) doesn't look
            # like a clean bill of health.
            log.debug("runtime_monitor.rss_read_failed", error=str(exc), exc_info=True)
        return 0.0


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------


@lru_cache(maxsize=1)
def get_monitor() -> RuntimeMonitor:
    """Process-wide RuntimeMonitor.

    lru_cache rather than a module global under `global`: the read and the
    assignment were two steps, so two callers arriving together could both
    find it unset and both construct one, with the second silently replacing
    the first. Same idiom as get_settings() in src/config.py, and it gives
    tests cache_clear() instead of reaching for the global.
    """
    return RuntimeMonitor()
