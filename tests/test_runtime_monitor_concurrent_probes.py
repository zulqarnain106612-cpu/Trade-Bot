"""
Health probes must run concurrently, or the monitor is slowest when it matters.

_run_all_probes awaited each registered probe in turn, and every probe
carries its own 10s timeout. Run sequentially those compound: n hung probes
cost n * 10s for one cycle, while POLL_INTERVAL_S assumes a cycle is short.
The monitor's latency therefore degraded in proportion to how much was
broken -- the snapshot went stalest during exactly the incident it exists to
describe.

Each probe still keeps its own timeout and its own consecutive-failure
count; only the waiting is shared.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from src.diagnostics.runtime_monitor import ProbeResult, RuntimeMonitor


def _monitor() -> RuntimeMonitor:
    return RuntimeMonitor()


@pytest.mark.asyncio
async def test_probes_do_not_wait_for_each_other() -> None:
    """Assert overlap directly rather than by wall clock.

    A duration bound has to separate ~0.2s (concurrent) from ~1.0s
    (sequential), but a loaded CI runner can stretch the concurrent case
    past a tight threshold and fail a correct implementation. Counting how
    many probes are in flight at once tests the property itself.
    """
    m = _monitor()
    in_flight = 0
    peak = 0

    async def slow():
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        try:
            await asyncio.sleep(0.05)
            return "ok"
        finally:
            in_flight -= 1

    for i in range(5):
        m.register_probe(f"p{i}", slow)

    started = time.monotonic()
    await m._run_all_probes()
    elapsed = time.monotonic() - started

    assert peak == 5  # all five overlapped
    assert elapsed < 0.25 * 5  # and nowhere near the sequential cost
    assert all(m._results[f"p{i}"].passed for i in range(5))


@pytest.mark.asyncio
async def test_one_failing_probe_does_not_hide_the_others() -> None:
    m = _monitor()

    async def ok():
        return "fine"

    async def boom():
        raise RuntimeError("probe exploded")

    m.register_probe("good", ok)
    m.register_probe("bad", boom)
    m.register_probe("also_good", ok)

    await m._run_all_probes()

    assert m._results["good"].passed is True
    assert m._results["also_good"].passed is True
    assert m._results["bad"].passed is False
    assert "probe exploded" in m._results["bad"].detail


@pytest.mark.asyncio
async def test_a_failure_increments_only_its_own_counter() -> None:
    m = _monitor()

    async def ok():
        return "fine"

    async def boom():
        raise RuntimeError("nope")

    m.register_probe("good", ok)
    m.register_probe("bad", boom)

    await m._run_all_probes()
    await m._run_all_probes()

    assert m._results["bad"].consecutive_failures == 2
    assert m._results["good"].consecutive_failures == 0


@pytest.mark.asyncio
async def test_recovery_resets_the_counter_and_the_ok_timestamp() -> None:
    m = _monitor()
    state = {"fail": True}

    async def flaky():
        if state["fail"]:
            raise RuntimeError("still down")
        return "back"

    m.register_probe("flaky", flaky)
    await m._run_all_probes()
    failed_at = m._results["flaky"].last_ok_ts

    state["fail"] = False
    await m._run_all_probes()

    assert m._results["flaky"].passed is True
    assert m._results["flaky"].consecutive_failures == 0
    assert m._results["flaky"].last_ok_ts >= failed_at


@pytest.mark.asyncio
async def test_a_failing_probe_preserves_its_last_ok_timestamp() -> None:
    m = _monitor()
    state = {"fail": False}

    async def flaky():
        if state["fail"]:
            raise RuntimeError("down")
        return "up"

    m.register_probe("flaky", flaky)
    await m._run_all_probes()
    ok_ts = m._results["flaky"].last_ok_ts

    state["fail"] = True
    await m._run_all_probes()

    assert m._results["flaky"].passed is False
    assert m._results["flaky"].last_ok_ts == ok_ts


@pytest.mark.asyncio
async def test_run_probe_returns_a_result_rather_than_raising() -> None:
    # gather(..., return_exceptions=True) would otherwise pair results to the
    # wrong names if a probe raised out of the wrapper.
    m = _monitor()

    async def boom():
        raise ValueError("bad")

    m.register_probe("bad", boom)
    pr = await m._run_probe("bad")

    assert isinstance(pr, ProbeResult)
    assert pr.passed is False


@pytest.mark.asyncio
async def test_no_probes_is_not_an_error() -> None:
    """A cycle with nothing registered must still complete.

    _run_all_probes always records the built-in memory probe (step 3), so
    the result set is never empty -- what matters is that the registered-probe
    gather over an empty set is a no-op rather than a failure.
    """
    m = _monitor()
    await m._run_all_probes()

    assert m._probes == {}
    assert set(m._results) == {"memory_rss_mb"}
