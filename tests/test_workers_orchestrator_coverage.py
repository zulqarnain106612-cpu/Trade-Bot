"""Tests for the uncovered parts of src/workers/orchestrator.py.

Covers _model_worker's queue loop and _run_horizon_inference's deterministic
pseudo-inference. No real process is spawned: _model_worker is driven with
plain in-process queue stand-ins, and signal.signal is patched because the
worker installs a SIGTERM handler that would otherwise leak into the test
session (and fails outright off the main thread).
"""

from __future__ import annotations

import queue
from unittest.mock import patch

from src.workers.orchestrator import WorkerResult, WorkerTask, _model_worker, _run_horizon_inference


def _task(horizon_id: int = 0, task_id: str = "t1") -> WorkerTask:
    return WorkerTask(
        task_id=task_id,
        horizon_id=horizon_id,
        symbol="BTC/USDT",
        features={"price": 100.0, "volume": 5.0},
    )


def test_run_horizon_inference_is_deterministic_for_identical_features():
    a = _run_horizon_inference(_task())
    b = _run_horizon_inference(_task())
    assert a.confidence == b.confidence
    assert a.direction == b.direction
    assert a.magnitude_mu == b.magnitude_mu


def test_run_horizon_inference_differs_for_different_features():
    t1 = _task()
    t2 = WorkerTask(task_id="t2", horizon_id=0, symbol="BTC/USDT", features={"price": 999.0})
    assert _run_horizon_inference(t1).confidence != _run_horizon_inference(t2).confidence


def test_run_horizon_inference_bounds_and_shape():
    result = _run_horizon_inference(_task())
    assert isinstance(result, WorkerResult)
    assert 0.0 <= result.confidence <= 1.0
    assert result.direction in (-1, 0, 1)
    assert 0.0 <= result.timing <= 1.0
    assert result.magnitude_sigma == abs(result.magnitude_mu) * 0.5
    assert result.error is None


def test_run_horizon_inference_algo_ioc_for_fast_horizons():
    assert _run_horizon_inference(_task(horizon_id=0)).algo == "IOC"
    assert _run_horizon_inference(_task(horizon_id=1)).algo == "IOC"


def test_run_horizon_inference_algo_iceberg_for_mid_horizons():
    assert _run_horizon_inference(_task(horizon_id=2)).algo == "iceberg"
    assert _run_horizon_inference(_task(horizon_id=4)).algo == "iceberg"


def test_run_horizon_inference_algo_twap_for_slow_horizons():
    assert _run_horizon_inference(_task(horizon_id=5)).algo == "TWAP"
    assert _run_horizon_inference(_task(horizon_id=9)).algo == "TWAP"


def test_run_horizon_inference_zeroes_direction_below_confidence_threshold():
    # Scan feature values until one lands under the 0.65 confidence gate --
    # the module's own rule is "direction only survives above 0.65".
    for i in range(200):
        task = WorkerTask(task_id="t", horizon_id=0, symbol="S", features={"p": float(i)})
        result = _run_horizon_inference(task)
        if result.confidence <= 0.65:
            assert result.direction == 0
            return
    raise AssertionError("no sub-threshold confidence found in 200 samples")


def test_model_worker_processes_task_then_exits_on_sentinel():
    q_in: queue.Queue = queue.Queue()
    q_out: queue.Queue = queue.Queue()
    q_in.put(_task())
    q_in.put(None)

    with patch("src.workers.orchestrator.signal.signal"):
        _model_worker(worker_id=0, queue_in=q_in, queue_out=q_out)

    result = q_out.get_nowait()
    assert isinstance(result, WorkerResult)
    assert result.task_id == "t1"
    assert q_out.empty()


def test_model_worker_exits_immediately_on_sentinel():
    q_in: queue.Queue = queue.Queue()
    q_out: queue.Queue = queue.Queue()
    q_in.put(None)

    with patch("src.workers.orchestrator.signal.signal"):
        _model_worker(worker_id=1, queue_in=q_in, queue_out=q_out)

    assert q_out.empty()


def test_model_worker_emits_error_result_when_inference_raises():
    q_in: queue.Queue = queue.Queue()
    q_out: queue.Queue = queue.Queue()
    q_in.put(_task(horizon_id=3, task_id="bad"))
    q_in.put(None)

    with (
        patch("src.workers.orchestrator.signal.signal"),
        patch(
            "src.workers.orchestrator._run_horizon_inference",
            side_effect=RuntimeError("model blew up"),
        ),
    ):
        _model_worker(worker_id=2, queue_in=q_in, queue_out=q_out)

    result = q_out.get_nowait()
    assert result.error == "model blew up"
    assert result.task_id == "bad"
    assert result.horizon_id == 3
    assert result.direction == 0
    assert result.confidence == 0.0
    assert result.algo == "TWAP"


def test_model_worker_installs_sigterm_handler():
    q_in: queue.Queue = queue.Queue()
    q_in.put(None)
    with patch("src.workers.orchestrator.signal.signal") as mock_signal:
        _model_worker(worker_id=3, queue_in=q_in, queue_out=queue.Queue())
    assert mock_signal.call_count == 1


def test_model_worker_continues_after_a_failed_task():
    q_in: queue.Queue = queue.Queue()
    q_out: queue.Queue = queue.Queue()
    q_in.put(_task(task_id="fails"))
    q_in.put(_task(task_id="succeeds"))
    q_in.put(None)

    real = _run_horizon_inference
    calls = {"n": 0}

    def _flaky(task):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient")
        return real(task)

    with (
        patch("src.workers.orchestrator.signal.signal"),
        patch("src.workers.orchestrator._run_horizon_inference", side_effect=_flaky),
    ):
        _model_worker(worker_id=4, queue_in=q_in, queue_out=q_out)

    first = q_out.get_nowait()
    second = q_out.get_nowait()
    assert first.error == "transient"
    assert second.error is None
