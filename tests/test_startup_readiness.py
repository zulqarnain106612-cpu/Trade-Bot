"""
RES-025 / REG-0026 -- the API listens while the models warm up, and says so.

The lifespan used to await Orchestrator.startup() -- a 90-day history
download and a model fit per timeframe -- before yielding, and uvicorn opens
the port only after the lifespan yields: a cold PAPER start on 2026-10-11
kept port 8000 closed for 27.7 minutes with nothing able to say why. Now the
warm-up runs in the orchestrator's run task and GET /readyz reports every
layer (src/api/readiness.py). Decides:

* readiness_report: each phase, and that ready is the conjunction;
* the orchestrator: a deferred warm-up runs before the timeframe loops, stop()
  ends it, a failure starts no loop and is reported, and readiness() tracks
  each timeframe;
* the bounded history refresh for a warm store;
* the runtime monitor: no stall before a loop's first bar, a cadence-aware
  limit, and the memory release after training;
* the API: /livez, /readyz, the deferred startup and the cleanup after a
  failed one.

  - REG-0027 — regression coverage for the recorded behavior.
  - REG-0028 — regression coverage for the recorded behavior.
  - REG-0029 — regression coverage for the recorded behavior."""

from __future__ import annotations

import asyncio
import contextlib
import os
import sqlite3
import time
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from test_orchestrator_coverage import _make_orch, _make_storage, _orch_patches

from src.api.readiness import readiness_report
from src.config import Timeframe
from src.diagnostics.runtime_monitor import (
    STALL_THRESHOLD_S,
    RuntimeMonitor,
    release_freed_memory,
    tick_stall_limit_s,
)

_API_KEY = "x" * 32
_OP_SECRET = "y" * 32


def _tf(model: str, *, ticks: int = 0, detail: str | None = None, loop: bool | None = None) -> dict:
    return {
        "model": model,
        "detail": detail,
        "loop": ticks > 0 if loop is None else loop,
        "ticks": ticks,
        "last_tick_age_s": 1.0 if ticks else None,
        "bar_seconds": 60,
    }


def _orch(warm_up: str, loops: bool = False, error: str | None = None, **tfs: dict) -> dict:
    return {"warm_up": warm_up, "error": error, "loops_started": loops, "timeframes": tfs}


def test_preflight_distinguishes_api_runtime_and_model_readiness() -> None:
    from scripts.startup_preflight import _readiness_signals

    warming = _report(_orch("running", m1=_tf("training")))
    assert warming["phase"] == "warming_up"
    assert _readiness_signals(warming) == {
        "api_ready": True,
        "runtime_ready": True,
        "models_ready": False,
        "ready": False,
    }

    models_ready_before_first_bar = _report(
        _orch("complete", loops=True, m1=_tf("ready", ticks=0, loop=True))
    )
    assert _readiness_signals(models_ready_before_first_bar) == {
        "api_ready": True,
        "runtime_ready": True,
        "models_ready": True,
        "ready": True,
    }
    # A first tick is a separate preflight milestone, not an invented property
    # of the model-readiness endpoint (particularly for a 4h timeframe).
    assert (
        models_ready_before_first_bar["components"]["trading_loops"]["timeframes"]["m1"][
            "first_tick"
        ]
        is False
    )


def test_preflight_does_not_accept_a_legacy_status_only_report() -> None:
    from scripts.startup_preflight import _readiness_signals

    assert _readiness_signals({"ready": True, "phase": "ready"}) == {
        "api_ready": False,
        "runtime_ready": False,
        "models_ready": False,
        "ready": False,
    }


def test_preflight_snapshots_database_and_models_into_disposable_storage(
    tmp_path, monkeypatch
) -> None:
    from scripts import startup_preflight

    repo = tmp_path / "repo"
    source_db = repo / "data" / "trade_bot.db"
    source_db.parent.mkdir(parents=True)
    with sqlite3.connect(source_db) as connection:
        connection.execute("CREATE TABLE sample (value INTEGER NOT NULL)")
        connection.execute("INSERT INTO sample VALUES (7)")
    source_models = repo / "models" / "artifacts"
    source_models.mkdir(parents=True)
    (source_models / "fixture.bin").write_bytes(b"model-fixture")
    monkeypatch.setattr(startup_preflight, "REPO_ROOT", repo)
    monkeypatch.setattr(
        "src.config.get_settings",
        lambda: SimpleNamespace(
            storage=SimpleNamespace(
                db_path=Path("data/trade_bot.db"), model_dir=Path("models/artifacts")
            )
        ),
    )
    env: dict[str, str] = {}
    evidence = startup_preflight._isolate_storage(env, tmp_path / "sandbox")

    assert evidence == {
        "database_snapshot": True,
        "model_artifacts_snapshot": True,
        "storage_backend": "sqlite",
        "isolated": True,
    }
    assert env["STORAGE_BACKEND"] == "sqlite"
    assert Path(env["STORAGE_DB_PATH"]) != source_db
    with sqlite3.connect(env["STORAGE_DB_PATH"]) as connection:
        assert connection.execute("SELECT value FROM sample").fetchone() == (7,)
        connection.execute("UPDATE sample SET value = 99")
    with sqlite3.connect(source_db) as connection:
        assert connection.execute("SELECT value FROM sample").fetchone() == (7,)
    assert (Path(env["STORAGE_MODEL_DIR"]) / "fixture.bin").read_bytes() == b"model-fixture"
    assert Path(env["STORAGE_LOG_DIR"]).is_dir()
    assert env["INTEL_ENABLED"] == "false"
    assert env["BINANCE_ORDERBOOK_STREAM_ENABLED"] == "false"


def _report(orchestrator: dict | None, **overrides: object) -> dict:
    kwargs: dict = {
        "api_ready": True,
        "stopping": False,
        "runtime_ready": True,
        "reconcile_passes": 1,
        "orchestrator": orchestrator,
        "trading_mode": "paper",
    }
    kwargs.update(overrides)
    return readiness_report(**kwargs)


# ---------------------------------------------------------------------------
# readiness_report
# ---------------------------------------------------------------------------


def test_ready_needs_every_layer_and_does_not_need_a_first_tick() -> None:
    report = _report(_orch("complete", loops=True, m1=_tf("ready", ticks=1), h4=_tf("ready")))
    assert report["ready"] is True
    assert report["phase"] == "ready"
    assert report["reasons"] == []
    loops = report["components"]["trading_loops"]
    assert loops["state"] == "running"
    assert loops["timeframes"]["m1"]["first_tick"] is True
    assert loops["timeframes"]["h4"]["first_tick"] is False
    assert report["components"]["runtime_platform"] == {"state": "ready", "reconcile_passes": 1}


def test_ready_report_preserves_optional_component_warnings_without_failing_core_readiness() -> (
    None
):
    report = _report(
        _orch(
            "complete",
            loops=True,
            m1=_tf(
                "ready",
                ticks=1,
                detail="core direction/meta models ready; optional component degradation: ensemble members not fitted: lstm",
            ),
        )
    )
    assert report["ready"] is True
    assert report["phase"] == "ready"
    assert report["reasons"] == []
    assert report["warnings"] == [
        "m1: core direction/meta models ready; optional component degradation: ensemble members not fitted: lstm"
    ]


def test_a_running_warm_up_names_each_unfinished_timeframe() -> None:
    report = _report(_orch("running", m1=_tf("training"), m15=_tf("pending")))
    assert report["ready"] is False
    assert report["phase"] == "warming_up"
    assert report["reasons"] == ["model warm-up in progress: m1 training, m15 pending"]
    assert report["components"]["models"]["timeframes"]["m1"] == {
        "state": "training",
        "detail": None,
    }


def test_before_the_orchestrator_reports_anything_the_warm_up_is_queued() -> None:
    report = _report(None)
    assert report["phase"] == "warming_up"
    assert report["reasons"] == ["model warm-up in progress: queued"]
    assert report["components"]["trading_loops"]["state"] == "waiting"


def test_built_models_without_running_loops_are_still_warming_up() -> None:
    report = _report(_orch("complete", m1=_tf("ready")))
    assert report["phase"] == "warming_up"
    assert report["reasons"] == ["the timeframe loops are starting"]


def test_a_timeframe_without_an_engine_is_degraded_with_its_reason() -> None:
    detail = "no model trained: the regime (HMM) fit failed"
    report = _report(
        _orch("complete", loops=True, m1=_tf("ready"), m15=_tf("unavailable", detail=detail))
    )
    assert report["ready"] is False
    assert report["phase"] == "degraded"
    assert report["reasons"] == [f"m15: {detail}"]
    report = _report(_orch("complete", loops=True, m15=_tf("unavailable")))
    assert report["reasons"] == ["m15: no engine"]


def test_a_missing_runtime_platform_is_never_ready() -> None:
    report = _report(_orch("complete", loops=True, m1=_tf("ready")), runtime_ready=False)
    assert report["phase"] == "degraded"
    assert report["components"]["runtime_platform"]["state"] == "unavailable"
    assert "operator intents are not enforced" in report["reasons"][0]


@pytest.mark.parametrize(
    ("warm_up", "error", "reason"),
    [
        ("failed", "Bootstrap failed for timeframe 1m: down", "model warm-up failed: Bootstrap"),
        ("cancelled", None, "model warm-up was cancelled"),
    ],
)
def test_a_warm_up_that_ended_early_fails_readiness(warm_up, error, reason) -> None:
    report = _report(_orch(warm_up, error=error, m1=_tf("failed")))
    assert report["phase"] == "failed"
    assert report["ready"] is False
    assert report["reasons"][0].startswith(reason)
    assert report["components"]["models"]["error"] == error


def test_starting_and_stopping_take_precedence() -> None:
    starting = _report(None, api_ready=False, runtime_ready=False)
    assert starting["phase"] == "starting"
    assert starting["components"]["api"] == {"state": "starting"}
    assert starting["components"]["runtime_platform"] == {"state": "starting"}
    assert "the API is starting" in starting["reasons"]

    done = _orch("complete", loops=True, m1=_tf("ready"))
    stopping = _report(done, stopping=True)
    assert stopping["phase"] == "stopping"
    assert stopping["ready"] is False
    assert stopping["reasons"] == ["the API is shutting down"]


# ---------------------------------------------------------------------------
# Orchestrator: the deferred warm-up
# ---------------------------------------------------------------------------


async def _idle() -> None:
    with contextlib.suppress(asyncio.CancelledError):
        await asyncio.Event().wait()


def _quiet_run_loops(orch, timeframe_loop=None) -> None:
    """Replace every loop run() starts with one that idles until cancelled."""
    orch._cfg.binance.orderbook_stream_enabled = False
    orch._midnight_reset_loop = _idle
    orch._position_monitor_loop = _idle
    orch._allocation_rebalance_loop = _idle
    orch._timeframe_loop = timeframe_loop or (lambda tf: _idle())


async def test_a_deferred_startup_builds_the_executor_and_trains_nothing() -> None:
    fetcher_storage = _make_storage()
    orch = _make_orch(storage=fetcher_storage)
    with _orch_patches() as (executor, _, __):
        await orch.startup(defer_warm_up=True)
    assert orch._executor is executor
    assert orch._engines == {}
    orch._fetcher.bootstrap_history.assert_not_awaited()
    report = orch.readiness()
    assert report["warm_up"] == "deferred"
    assert report["loops_started"] is False
    assert report["timeframes"]["15m"] == {
        "model": "pending",
        "detail": None,
        "loop": False,
        "ticks": 0,
        "last_tick_age_s": None,
        "bar_seconds": 900,
    }


async def test_run_warms_up_first_then_starts_the_timeframe_loops() -> None:
    orch = _make_orch()
    loop_started = asyncio.Event()

    async def _timeframe_loop(tf) -> None:
        loop_started.set()
        await _idle()

    with _orch_patches():
        await orch.startup(defer_warm_up=True)
        _quiet_run_loops(orch, _timeframe_loop)
        run = asyncio.create_task(orch.run())
        await asyncio.wait_for(loop_started.wait(), 30)
        report = orch.readiness()
        orch.stop()
        await run

    assert report["warm_up"] == "complete"
    assert report["loops_started"] is True
    assert report["timeframes"]["15m"]["model"] == "ready"
    assert report["timeframes"]["15m"]["loop"] is True
    assert "15m" in orch._engines
    assert orch._running is False


async def test_stop_during_the_warm_up_cancels_it_and_starts_no_loop() -> None:
    orch = _make_orch()
    bootstrapping = asyncio.Event()

    async def _slow_bootstrap(*args, **kwargs) -> int:
        bootstrapping.set()
        await asyncio.Event().wait()
        return 0

    orch._fetcher.bootstrap_history = AsyncMock(side_effect=_slow_bootstrap)
    with _orch_patches():
        await orch.startup(defer_warm_up=True)
        _quiet_run_loops(orch)
        run = asyncio.create_task(orch.run())
        await asyncio.wait_for(bootstrapping.wait(), 30)
        assert orch.readiness()["timeframes"]["15m"]["model"] == "bootstrapping"
        orch.stop()
        await run

    assert orch.readiness()["warm_up"] == "cancelled"
    assert orch._loops_started is False
    assert orch._run_tasks and all(t.done() for t in orch._run_tasks)


async def test_a_failed_warm_up_starts_no_loop_and_keeps_the_monitor_until_stop() -> None:
    orch = _make_orch()
    orch._fetcher.bootstrap_history = AsyncMock(side_effect=RuntimeError("exchange down"))
    monitor_started = asyncio.Event()
    warm_up_done = asyncio.Event()

    async def _monitor() -> None:
        monitor_started.set()
        await _idle()

    real_warm_up = orch.warm_up

    async def _tracked_warm_up() -> None:
        try:
            await real_warm_up()
        finally:
            warm_up_done.set()

    with _orch_patches():
        await orch.startup(defer_warm_up=True)
        _quiet_run_loops(orch)
        orch._position_monitor_loop = _monitor
        orch.warm_up = _tracked_warm_up
        run = asyncio.create_task(orch.run())
        await asyncio.wait_for(warm_up_done.wait(), 30)
        await asyncio.wait_for(monitor_started.wait(), 30)
        orch.stop()
        await run

    report = orch.readiness()
    assert report["warm_up"] == "failed"
    assert "Bootstrap failed for timeframe 15m" in report["error"]
    assert report["timeframes"]["15m"]["model"] == "failed"
    assert report["timeframes"]["15m"]["detail"] == "history bootstrap failed: exchange down"
    assert report["loops_started"] is False
    assert {t.get_name() for t in orch._run_tasks} >= {"position_monitor"}


async def test_a_plain_startup_still_finishes_everything_before_returning() -> None:
    orch = _make_orch()
    with _orch_patches():
        await orch.startup()
    assert "15m" in orch._engines
    assert orch.readiness()["warm_up"] == "complete"


async def test_unavailable_engines_carry_the_reason() -> None:
    no_history = _make_storage()
    no_history.fetch_bars = AsyncMock(return_value=[])
    orch = _make_orch(storage=no_history)
    with _orch_patches():
        await orch.startup()
    entry = orch.readiness()["timeframes"]["15m"]
    assert entry["model"] == "unavailable"
    assert entry["detail"] == "no model trained: too little history or the feature build failed"

    orch = _make_orch()
    failing_hmm = MagicMock()
    failing_hmm.fit = MagicMock(side_effect=ValueError("degenerate covariance"))
    with (
        _orch_patches(),
        patch("src.engine.orchestrator.RegimeDetector", return_value=failing_hmm),
    ):
        await orch.startup()
    detail = orch.readiness()["timeframes"]["15m"]["detail"]
    assert detail == "no model trained: the regime (HMM) fit failed"

    orch = _make_orch()
    with (
        _orch_patches(),
        patch(
            "src.engine.orchestrator.ModelTrainer.load_direction",
            side_effect=FileNotFoundError("gone"),
        ),
    ):
        await orch.startup()
    entry = orch.readiness()["timeframes"]["15m"]
    assert entry["model"] == "unavailable"
    assert entry["detail"] == "trained model files not found in /tmp/models"


def test_readiness_reports_the_age_of_the_last_tick() -> None:
    orch = _make_orch()
    orch._tick_counts["15m"] = 3
    orch._last_tick_ts["15m"] = time.monotonic() - 5.0
    entry = orch.readiness()["timeframes"]["15m"]
    assert entry["ticks"] == 3
    assert entry["last_tick_age_s"] >= 5.0


async def test_each_timeframe_loop_registers_its_own_tick_source() -> None:
    orch = _make_orch()
    monitor = MagicMock()
    orch._running = False  # the loop registers, then exits at once
    with patch("src.engine.orchestrator.get_monitor", return_value=monitor):
        await orch._timeframe_loop(Timeframe.INTRADAY)
    (timeframe, getter), _ = monitor.register_tick_source.call_args
    assert timeframe == "15m"
    assert getter() == 0.0
    orch._last_tick_ts["15m"] = 42.0
    assert getter() == 42.0


# ---------------------------------------------------------------------------
# The history refresh for a warm store
# ---------------------------------------------------------------------------


def _now_ms() -> int:
    return int(datetime.now(tz=UTC).timestamp() * 1000)


@pytest.mark.parametrize(
    ("timeframe", "latest_age_ms"),
    [
        (Timeframe.SCALPING, 60_000),
        (Timeframe.INTRADAY, 10 * 60_000),
        (Timeframe.SWING, 60_000),
    ],
)
async def test_history_reuses_sufficient_fresh_cache_after_gap_fill(
    timeframe, latest_age_ms
) -> None:
    storage = _make_storage()
    storage.latest_bar_ts = AsyncMock(return_value=_now_ms() - latest_age_ms)
    orch = _make_orch(storage=storage)
    orch._fetcher.gap_fill = AsyncMock(return_value=7)

    assert await orch._prepare_history(timeframe) == 7
    orch._fetcher.gap_fill.assert_awaited_once_with("BTC/USDT", timeframe)
    orch._fetcher.bootstrap_history.assert_not_awaited()


@pytest.mark.asyncio
async def test_history_falls_back_to_full_bootstrap_when_refresh_fails_on_stale_cache() -> None:
    storage = _make_storage()
    stale_ts = _now_ms() - 2 * 86_400_000
    storage.latest_bar_ts = AsyncMock(return_value=stale_ts)
    orch = _make_orch(storage=storage)
    orch._fetcher.gap_fill = AsyncMock(side_effect=RuntimeError("exchange unavailable"))

    assert await orch._prepare_history(Timeframe.INTRADAY) == 100
    orch._fetcher.gap_fill.assert_awaited_once_with("BTC/USDT", Timeframe.INTRADAY)
    orch._fetcher.bootstrap_history.assert_awaited_once_with("BTC/USDT", Timeframe.INTRADAY)


@pytest.mark.asyncio
async def test_history_can_use_fresh_cache_when_incremental_refresh_temporarily_fails() -> None:
    storage = _make_storage()
    storage.latest_bar_ts = AsyncMock(return_value=_now_ms() - 60_000)
    orch = _make_orch(storage=storage)
    orch._fetcher.gap_fill = AsyncMock(side_effect=RuntimeError("exchange unavailable"))

    assert await orch._prepare_history(Timeframe.SCALPING) == 0
    orch._fetcher.bootstrap_history.assert_not_awaited()


# ---------------------------------------------------------------------------
# Runtime monitor
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("timeframe", "limit"),
    [
        ("1m", STALL_THRESHOLD_S),
        ("15m", 960.0),
        ("4h", 14_460.0),
        ("1d", 86_460.0),
        ("0m", STALL_THRESHOLD_S),
        ("m", STALL_THRESHOLD_S),
        ("5x", STALL_THRESHOLD_S),
    ],
)
def test_the_stall_limit_is_at_least_one_bar(timeframe, limit) -> None:
    assert tick_stall_limit_s(timeframe) == limit


async def test_the_first_tick_is_awaited_from_registration_not_from_boot() -> None:
    monitor = RuntimeMonitor()
    monitor.register_tick_source("1m", lambda: 0.0)
    await monitor._run_all_probes()
    probe = monitor._results["tick_stall_1m"]
    assert probe.passed is True
    assert probe.detail == "awaiting_first_tick"

    monitor._tick_registered_at["1m"] = time.monotonic() - STALL_THRESHOLD_S - 30
    await monitor._run_all_probes()
    probe = monitor._results["tick_stall_1m"]
    assert probe.passed is False
    assert probe.detail.startswith("first_tick_not_seen_for_")
    assert any(alert.startswith("tick_stall_1m") for alert in monitor.get_snapshot().alerts)


async def test_a_quarter_hour_loop_between_bars_is_not_a_stall() -> None:
    monitor = RuntimeMonitor()
    last_tick = time.monotonic() - 600
    monitor.register_tick_source("15m", lambda: last_tick)
    await monitor._run_all_probes()
    assert monitor._results["tick_stall_15m"].passed is True

    last_tick = time.monotonic() - 1000
    monitor.register_tick_source("15m", lambda: last_tick)
    await monitor._run_all_probes()
    assert monitor._results["tick_stall_15m"].passed is False


def test_freed_memory_is_returned_to_the_os_where_glibc_allows() -> None:
    assert isinstance(release_freed_memory(), float)
    with patch("src.diagnostics.runtime_monitor.ctypes.CDLL", side_effect=OSError("no libc")):
        assert release_freed_memory() is None
    no_trim = MagicMock(spec=[])
    with patch("src.diagnostics.runtime_monitor.ctypes.CDLL", return_value=no_trim):
        assert release_freed_memory() is None


# ---------------------------------------------------------------------------
# The API: /livez, /readyz and the lifespan
# ---------------------------------------------------------------------------


@pytest.fixture
def state():
    from src.api.main import AppState

    st = AppState()
    st.storage = AsyncMock()
    with (
        patch("src.api.main._state", st),
        patch.dict(os.environ, {"API_SECRET_KEY": _API_KEY, "OPERATOR_SECRET": _OP_SECRET}),
        patch("src.api.auth._get_configured_key", return_value=_API_KEY),
    ):
        yield st


@pytest.fixture
def client():
    from src.api.main import app

    return TestClient(app, raise_server_exceptions=False)


def test_the_dashboard_dev_server_is_an_allowed_origin_under_both_names(monkeypatch) -> None:
    # Vite binds 127.0.0.1 and the desktop app loads http://127.0.0.1:5173;
    # with only "localhost" allowed the browser dropped every API answer.
    from src.config import APISettings

    monkeypatch.delenv("API_CORS_ORIGINS", raising=False)
    origins = set(APISettings(_env_file=None).cors_origins)
    assert {"http://localhost:5173", "http://127.0.0.1:5173"} <= origins


def test_livez_is_constant_and_authenticated(state, client) -> None:
    response = client.get("/livez", headers={"x-api-key": _API_KEY})
    assert response.status_code == 200
    assert response.json() == {"status": "alive"}
    assert client.get("/livez").status_code in (401, 403)


def test_readyz_is_503_with_its_reasons_until_everything_is_up(state, client) -> None:
    orchestrator = MagicMock()
    orchestrator.readiness = MagicMock(return_value=_orch("running", m1=_tf("training")))
    state.ready = True
    state.orchestrator = orchestrator
    state.runtime = MagicMock()
    state.runtime_loop = SimpleNamespace(passes=4)

    response = client.get("/readyz", headers={"x-api-key": _API_KEY})
    assert response.status_code == 503
    body = response.json()
    assert body["phase"] == "warming_up"
    assert body["reasons"] == ["model warm-up in progress: m1 training"]
    assert body["components"]["runtime_platform"]["reconcile_passes"] == 4

    orchestrator.readiness.return_value = _orch("complete", loops=True, m1=_tf("ready"))
    response = client.get("/readyz", headers={"x-api-key": _API_KEY})
    assert response.status_code == 200
    assert response.json()["ready"] is True


def test_readyz_before_the_lifespan_has_built_anything(state, client) -> None:
    response = client.get("/readyz", headers={"x-api-key": _API_KEY})
    assert response.status_code == 503
    body = response.json()
    assert body["phase"] == "starting"
    assert body["components"]["runtime_platform"] == {"state": "starting"}


class _FetcherCtx:
    def __init__(self, fetcher) -> None:
        self._fetcher = fetcher

    async def __aenter__(self):
        return self._fetcher

    async def __aexit__(self, *exc):
        return False


def _lifespan_patches(orch, storage):
    fetcher = AsyncMock()
    fetcher.close = AsyncMock()
    return [
        patch.dict(os.environ, {"API_SECRET_KEY": _API_KEY, "OPERATOR_SECRET": _OP_SECRET}),
        patch("src.api.main.create_storage_backend", return_value=storage),
        patch("src.api.main.open_fetcher", return_value=_FetcherCtx(fetcher)),
        patch("src.api.main.Orchestrator", return_value=orch),
    ]


def _fake_orchestrator() -> MagicMock:
    orch = MagicMock()
    orch.startup = AsyncMock()
    orch.run = AsyncMock(return_value=None)
    orch.stop = MagicMock()
    orch.shutdown = AsyncMock()
    return orch


async def test_the_lifespan_defers_the_warm_up_and_reports_stopping() -> None:
    from src.api import main

    orch = _fake_orchestrator()
    with contextlib.ExitStack() as stack:
        for ctx in _lifespan_patches(orch, AsyncMock()):
            stack.enter_context(ctx)
        async with main.lifespan(main.app):
            assert main._state.ready is True
            assert main._state.stopping is False
        assert main._state.stopping is True
    orch.startup.assert_awaited_once()
    startup_kwargs = orch.startup.await_args.kwargs
    assert startup_kwargs["defer_warm_up"] is True
    assert isinstance(startup_kwargs["fetcher_init_task"], asyncio.Task)


async def test_deferred_fetcher_initialization_does_not_block_api_lifespan() -> None:
    from src.api import main

    storage = AsyncMock()
    fetcher = AsyncMock()
    fetcher.initialize = AsyncMock(return_value=None)
    fetcher.close = AsyncMock()
    orch = _fake_orchestrator()
    state = main.AppState()
    state.storage = storage
    with contextlib.ExitStack() as stack:
        for ctx in _lifespan_patches(orch, storage):
            stack.enter_context(ctx)
        stack.enter_context(patch("src.api.main._state", state))
        stack.enter_context(patch("src.api.main.open_fetcher", return_value=_FetcherCtx(fetcher)))
        stack.enter_context(
            patch("src.api.main.start_runtime_platform", new=AsyncMock(return_value=None))
        )
        async with main.lifespan(main.app):
            assert state.ready is True
            init_task = orch.startup.await_args.kwargs["fetcher_init_task"]
            await init_task
            fetcher.initialize.assert_awaited_once()
    orch.shutdown.assert_awaited_once()
    storage.close.assert_awaited_once()


async def test_failed_deferred_fetcher_initialization_marks_readiness_failed_and_starts_no_timeframe_loop() -> (
    None
):
    orch = _make_orch()

    async def fail_fetcher() -> None:
        raise RuntimeError("no exchange venue could be initialized")

    fetcher_task = asyncio.create_task(fail_fetcher())
    with _orch_patches():
        await orch.startup(defer_warm_up=True, fetcher_init_task=fetcher_task)
        _quiet_run_loops(orch)
        run = asyncio.create_task(orch.run())

        with pytest.raises(RuntimeError, match="no exchange venue"):
            await asyncio.wait_for(fetcher_task, 2)
        # Yield to the prerequisite task until its failure is reflected in
        # readiness, without a wall-clock sleep or scheduler-dependent delay.
        for _ in range(10):
            report = orch.readiness()
            if report["error"] is not None:
                break
            await asyncio.sleep(0)
        assert "no exchange venue" in report["error"]
        assert report["loops_started"] is False
        assert not any(t.get_name().startswith("loop_") for t in orch._run_tasks)
        orch.stop()
        await run


async def test_an_error_while_serving_still_releases_the_orchestrator_and_storage() -> None:
    from src.api import main

    orch = _fake_orchestrator()
    storage = AsyncMock()
    with contextlib.ExitStack() as stack:
        for ctx in _lifespan_patches(orch, storage):
            stack.enter_context(ctx)
        with pytest.raises(ValueError, match="app failed"):
            async with main.lifespan(main.app):
                raise ValueError("app failed")
    orch.shutdown.assert_awaited_once()
    storage.close.assert_awaited_once()


@pytest.mark.parametrize("storage_close_fails", [False, True])
async def test_a_failed_startup_releases_the_orchestrator_and_storage(storage_close_fails) -> None:
    from src.api import main

    orch = _fake_orchestrator()
    orch.startup = AsyncMock(side_effect=RuntimeError("boom"))
    storage = AsyncMock()
    if storage_close_fails:
        storage.close = AsyncMock(side_effect=OSError("already closed"))
    with contextlib.ExitStack() as stack:
        for ctx in _lifespan_patches(orch, storage):
            stack.enter_context(ctx)
        with pytest.raises(RuntimeError, match="boom"):
            async with main.lifespan(main.app):
                pass
    orch.shutdown.assert_awaited_once()
    storage.close.assert_awaited_once()
