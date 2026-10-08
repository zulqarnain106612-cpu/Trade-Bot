"""RES-023: the continuous loop survives every failure of its own parts, and
the MLflow listing never blocks it or forgets what it last saw.

* A pass reports a component reported twice instead of registering it twice,
  and logs a pass slower than its budget.
* ``run`` keeps going when the I/O refresh, a pass or the flush raises: each
  is logged and the next pass still runs.
* ``UpgradeListing`` runs on its own thread; a listing that fails or is
  still in flight keeps the last rows; without crypto-intel there is nothing
  to list; ``close`` lets go of the thread.
* The loop refuses a meaningless interval.

Decides: RES-023"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Sequence
from typing import Any

import pytest
from structlog.testing import capture_logs

from src.runtime.adapters import Discovery
from src.runtime.platform import RuntimePlatform, build_runtime_platform
from src.runtime.production import (
    ProductionSources,
    RuntimeLoop,
    UpgradeListing,
    production_discoveries,
)

from ._production import World, world


def _platform(w: World) -> RuntimePlatform:
    return build_runtime_platform(bus=w.bus, discoveries=production_discoveries(w.sources()))


@pytest.mark.parametrize("kwargs", [{"interval_s": 0.0}, {"slow_refresh_every": 0}])
def test_a_meaningless_interval_is_refused(
    monkeypatch: pytest.MonkeyPatch, kwargs: dict[str, Any]
) -> None:
    w = world(monkeypatch)
    with pytest.raises(ValueError, match="interval_s"):
        RuntimeLoop(_platform(w), lambda: [], **kwargs)


def test_a_component_reported_twice_is_a_problem_not_a_duplicate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    w = world(monkeypatch)
    sources = w.sources()
    platform = _platform(w)
    rows = production_discoveries(sources)
    loop = RuntimeLoop(platform, lambda: [*rows, rows[0]])
    report = loop.sync_once()
    assert report.problems == (f"{rows[0].spec.component_id}: reported twice in one pass",)
    assert len(platform.registry.components()) == len(rows)


def test_a_slow_pass_is_logged(monkeypatch: pytest.MonkeyPatch) -> None:
    w = world(monkeypatch)
    sources = w.sources()
    loop = RuntimeLoop(_platform(w), lambda: production_discoveries(sources), slow_step_ms=-1.0)
    with capture_logs() as logs:
        loop.step()
    assert [e["event"] for e in logs if e["event"] == "runtime.loop_step_slow"]


async def test_run_survives_a_failing_refresh_pass_and_flush(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    w = world(monkeypatch)
    sources = w.sources()
    platform = _platform(w)
    stop = asyncio.Event()
    passes: list[int] = []

    def discover() -> Sequence[Discovery]:
        passes.append(1)
        if len(passes) == 2:
            stop.set()
        return production_discoveries(sources)

    async def refresh() -> None:
        raise ConnectionError("mlflow down")

    async def flush() -> int:
        raise OSError("disk full")

    def reconcile(*, suppress_repeats: bool = False) -> list[object]:
        raise RuntimeError("reconciler bug")

    monkeypatch.setattr(platform, "flush", flush)
    monkeypatch.setattr(platform.reconciler, "reconcile_once", reconcile)
    loop = RuntimeLoop(platform, discover, interval_s=0.001, slow_refresh=refresh)
    with capture_logs() as logs:
        await asyncio.wait_for(loop.run(stop), timeout=5.0)
    events = [e["event"] for e in logs]
    assert len(passes) == 2  # the second pass ran after every failure of the first
    assert events.count("runtime.loop_step_failed") == 2
    assert events.count("runtime.loop_flush_failed") == 2
    assert events.count("runtime.slow_refresh_failed") == 1  # once, then every 60 passes


class _Registry:
    """The MLflow registry facade's one call, without a tracking server."""

    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = [{"name": "horizon_4h", "latest_version": 3}]
        self.fail = False
        self.release = threading.Event()
        self.release.set()
        self.threads: set[str] = set()

    def list_registered(self) -> list[dict[str, Any]]:
        self.threads.add(threading.current_thread().name)
        self.release.wait(timeout=5.0)
        if self.fail:
            raise ConnectionError("tracking server unreachable")
        return self.rows


class _Intel:
    def __init__(self) -> None:
        self.upgrade_registry = _Registry()


def _sources(w: World, intel: _Intel | None) -> ProductionSources:
    sources = w.sources()
    sources.intel = lambda: intel
    return sources


async def test_the_listing_runs_off_the_loop_and_keeps_the_last_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    w = world(monkeypatch)
    intel = _Intel()
    sources = _sources(w, intel)
    listing = UpgradeListing(sources, timeout_s=5.0)
    try:
        rows = await listing.refresh()
        assert rows == [{"name": "horizon_4h", "latest_version": "3"}]
        threads = intel.upgrade_registry.threads
        assert threads and all(t.startswith("runtime-upgrades") for t in threads)
        intel.upgrade_registry.fail = True
        with capture_logs() as logs:
            assert await listing.refresh() == rows  # a failure keeps what was seen
        assert logs[-1]["event"] == "runtime.upgrade_listing_failed"
    finally:
        listing.close()


async def test_a_listing_still_in_flight_is_not_started_twice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    w = world(monkeypatch)
    intel = _Intel()
    sources = _sources(w, intel)
    sources.upgrade_rows = [{"name": "kept", "latest_version": "1"}]
    intel.upgrade_registry.release.clear()  # the tracking server hangs
    listing = UpgradeListing(sources, timeout_s=0.01)
    try:
        assert await listing.refresh() == [{"name": "kept", "latest_version": "1"}]  # timed out
        with capture_logs() as logs:
            assert await listing.refresh() == [{"name": "kept", "latest_version": "1"}]
        assert logs[-1]["event"] == "runtime.upgrade_listing_still_running"
    finally:
        intel.upgrade_registry.release.set()
        listing.close()


async def test_without_crypto_intel_there_is_nothing_to_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    w = world(monkeypatch)
    sources = _sources(w, None)
    sources.upgrade_rows = [{"name": "stale", "latest_version": "1"}]
    listing = UpgradeListing(sources)
    assert await listing.refresh() == [] and sources.upgrade_rows == []
    listing.close()  # nothing was started: nothing to shut down
