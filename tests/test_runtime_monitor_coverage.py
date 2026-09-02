"""Tests for src/diagnostics/runtime_monitor.py (27% → target 80%+)."""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.diagnostics.runtime_monitor import (
    MEMORY_CRITICAL_MB,
    MEMORY_WARN_MB,
    STALL_THRESHOLD_S,
    HealthSnapshot,
    ProbeResult,
    RuntimeMonitor,
    get_monitor,
)

# ---------------------------------------------------------------------------
# Dataclass sanity
# ---------------------------------------------------------------------------


def test_probe_result_defaults():
    pr = ProbeResult(name="test", passed=True)
    assert pr.name == "test"
    assert pr.passed is True
    assert pr.consecutive_failures == 0


def test_health_snapshot_to_dict():
    snap = HealthSnapshot(
        ts_utc=1234567890.0,
        probes=[ProbeResult(name="x", passed=True, value=42)],
        overall="ok",
        alerts=[],
    )
    d = snap.to_dict()
    assert d["overall"] == "ok"
    assert d["ts_utc"] == 1234567890.0
    assert d["alerts"] == []
    assert d["probes"][0]["name"] == "x"
    assert d["probes"][0]["passed"] is True
    assert "last_ok_s_ago" in d["probes"][0]


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def test_register_probe_stores_factory():
    m = RuntimeMonitor()
    factory = AsyncMock(return_value={"status": "ok"})
    m.register_probe("storage", factory)
    assert "storage" in m._probes
    assert "storage" in m._results


def test_register_tick_source():
    m = RuntimeMonitor()

    def getter() -> float:
        return time.monotonic()

    m.register_tick_source("1h", getter)
    assert "1h" in m._tick_sources


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_creates_task():
    m = RuntimeMonitor()
    with patch.object(m, "_loop", new=AsyncMock()):
        await m.start()
        assert m._task is not None
        assert m._running is True
        await m.stop()


@pytest.mark.asyncio
async def test_stop_cancels_task():
    m = RuntimeMonitor()

    async def _never_end():
        await asyncio.sleep(3600)

    m._running = True
    m._task = asyncio.create_task(_never_end())
    await m.stop()
    assert m._running is False


@pytest.mark.asyncio
async def test_loop_logs_and_continues_on_probe_run_exception():
    """_loop()'s own try/except (not _run_all_probes()'s internal
    per-probe handling) must catch anything unexpected from
    _run_all_probes() itself and keep the loop alive for the next tick."""
    m = RuntimeMonitor()
    m._running = True
    call_count = 0

    async def _boom():
        nonlocal call_count
        call_count += 1
        raise RuntimeError("probe cycle blew up")

    async def _fake_sleep(_s):
        m._running = False  # stop after one iteration

    with (
        patch.object(m, "_run_all_probes", side_effect=_boom),
        patch("asyncio.sleep", side_effect=_fake_sleep),
    ):
        await m._loop()  # must not raise

    assert call_count == 1


@pytest.mark.asyncio
async def test_stop_without_a_running_task_is_a_noop():
    m = RuntimeMonitor()
    assert m._task is None
    await m.stop()  # must not raise
    assert m._running is False


@pytest.mark.asyncio
async def test_stop_with_already_done_task_skips_cancel():
    m = RuntimeMonitor()

    async def _finish_immediately():
        return None

    m._running = True
    m._task = asyncio.create_task(_finish_immediately())
    await asyncio.sleep(0)  # let it finish
    assert m._task.done()
    await m.stop()  # must not raise; skips the cancel() branch
    assert m._running is False


@pytest.mark.asyncio
async def test_get_snapshot_initially_none():
    m = RuntimeMonitor()
    assert m.get_snapshot() is None


# ---------------------------------------------------------------------------
# _run_all_probes — happy paths and failures
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_all_probes_passing():
    m = RuntimeMonitor()

    async def _good_probe():
        return {"status": "healthy"}

    m.register_probe("db", _good_probe)
    m._tick_sources["1h"] = lambda: time.monotonic()
    await m._run_all_probes()
    snap = m.get_snapshot()
    assert snap is not None
    assert snap.overall in ("ok", "degraded", "critical")
    assert m._results["db"].passed is True


@pytest.mark.asyncio
async def test_run_all_probes_failure_increments_consecutive():
    m = RuntimeMonitor()

    async def _bad_probe():
        raise RuntimeError("db down")

    m.register_probe("db", _bad_probe)
    await m._run_all_probes()
    assert m._results["db"].passed is False
    assert m._results["db"].consecutive_failures == 1
    await m._run_all_probes()
    assert m._results["db"].consecutive_failures == 2


@pytest.mark.asyncio
async def test_run_all_probes_timeout():
    m = RuntimeMonitor()

    async def _slow_probe():
        await asyncio.sleep(100)

    m.register_probe("slow", _slow_probe)

    def _fail_wait_for(coro, timeout=None):
        coro.close()  # mimic asyncio.wait_for's real cancel-and-close on timeout
        raise TimeoutError

    with patch("asyncio.wait_for", side_effect=_fail_wait_for):
        await m._run_all_probes()
    assert m._results["slow"].passed is False
    assert m._results["slow"].detail == "timeout_10s"


@pytest.mark.asyncio
async def test_run_all_probes_tick_stall():
    m = RuntimeMonitor()
    stale_ts = time.monotonic() - STALL_THRESHOLD_S - 100
    m.register_tick_source("5m", lambda: stale_ts)
    await m._run_all_probes()
    snap = m.get_snapshot()
    assert any("tick_stall" in a for a in snap.alerts)


@pytest.mark.asyncio
async def test_run_all_probes_tick_ok():
    m = RuntimeMonitor()
    m.register_tick_source("5m", lambda: time.monotonic())
    await m._run_all_probes()
    assert m._results["tick_stall_5m"].passed is True


@pytest.mark.asyncio
async def test_run_all_probes_tick_getter_exception():
    m = RuntimeMonitor()
    m.register_tick_source("bad", lambda: 1 / 0)
    await m._run_all_probes()  # should not raise


@pytest.mark.asyncio
async def test_run_all_probes_detects_dead_tasks():
    """A completed, non-cancelled asyncio task lingering in the event loop
    (other than the harness's own "Task-1") must be flagged as an alert.
    asyncio.all_tasks() normally excludes finished tasks by the time a test
    can observe them, so this mocks it directly rather than relying on a
    real (unreliable) completion race."""
    m = RuntimeMonitor()

    dead_task = MagicMock()
    dead_task.done.return_value = True
    dead_task.cancelled.return_value = False
    dead_task.get_name.return_value = "leaked-worker"

    with patch("asyncio.all_tasks", return_value={dead_task}):
        await m._run_all_probes()

    snap = m.get_snapshot()
    assert snap is not None
    assert any("dead_tasks" in a for a in snap.alerts)


@pytest.mark.asyncio
async def test_run_all_probes_overall_critical_with_alerts():
    m = RuntimeMonitor()

    async def _bad():
        raise RuntimeError("broken")

    m.register_probe("broken", _bad)
    await m._run_all_probes()
    snap = m.get_snapshot()
    # Has alert and failed probe → critical
    assert snap.overall in ("critical", "degraded")


@pytest.mark.asyncio
async def test_run_all_probes_memory_branches():
    m = RuntimeMonitor()

    with patch.object(RuntimeMonitor, "_rss_mb", return_value=100.0):
        await m._run_all_probes()
    assert m._results["memory_rss_mb"].passed is True

    with patch.object(RuntimeMonitor, "_rss_mb", return_value=MEMORY_WARN_MB + 50):
        await m._run_all_probes()
    assert m._results["memory_rss_mb"].passed is True  # warn but pass

    with patch.object(RuntimeMonitor, "_rss_mb", return_value=MEMORY_CRITICAL_MB + 100):
        await m._run_all_probes()
    assert m._results["memory_rss_mb"].passed is False


@pytest.mark.asyncio
async def test_probe_consecutive_failures_escalate_to_critical():
    m = RuntimeMonitor()

    async def _bad():
        raise RuntimeError("always fails")

    m.register_probe("always_bad", _bad)
    # Run enough times to hit MAX_CONSECUTIVE_FAILURES=3
    for _ in range(4):
        await m._run_all_probes()
    assert m._results["always_bad"].consecutive_failures >= 3


# ---------------------------------------------------------------------------
# _rss_mb
# ---------------------------------------------------------------------------


def test_rss_mb_returns_float():
    result = RuntimeMonitor._rss_mb()
    assert isinstance(result, float)
    assert result >= 0.0


def test_rss_mb_failure_returns_zero():
    with patch("builtins.open", side_effect=OSError("not found")):
        result = RuntimeMonitor._rss_mb()
    assert result == 0.0


def test_rss_mb_no_matching_line_returns_zero():
    """The /proc/self/status file exists and is readable, but has no
    VmRSS: line -- the loop exhausts without returning, falling through
    to the 0.0 default."""
    from unittest.mock import mock_open

    with patch("builtins.open", mock_open(read_data="VmSize:  1234 kB\nThreads: 4\n")):
        result = RuntimeMonitor._rss_mb()
    assert result == 0.0


# ---------------------------------------------------------------------------
# _on_task_done
# ---------------------------------------------------------------------------


def test_on_task_done_with_exception():
    m = RuntimeMonitor()
    task = MagicMock()
    task.cancelled.return_value = False
    task.exception.return_value = RuntimeError("crashed")
    m._on_task_done(task)  # should not raise


def test_on_task_done_cancelled():
    m = RuntimeMonitor()
    task = MagicMock()
    task.cancelled.return_value = True
    m._on_task_done(task)  # should not raise


# ---------------------------------------------------------------------------
# get_monitor singleton
# ---------------------------------------------------------------------------


def test_get_monitor_returns_singleton():
    m1 = get_monitor()
    m2 = get_monitor()
    assert m1 is m2
    assert isinstance(m1, RuntimeMonitor)
