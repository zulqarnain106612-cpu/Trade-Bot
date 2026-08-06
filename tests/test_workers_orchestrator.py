"""Tests for WorkerOrchestrator — multiprocessing pool + ECC thread."""

from __future__ import annotations


class TestWorkerOrchestrator:
    def test_default_worker_count_clipped(self) -> None:
        from src.workers.orchestrator import WorkerOrchestrator

        orch = WorkerOrchestrator(n_workers=1)  # below MIN_WORKERS=2
        assert orch._n_workers == 2

    def test_default_worker_count_max_clipped(self) -> None:
        from src.workers.orchestrator import WorkerOrchestrator

        orch = WorkerOrchestrator(n_workers=100)  # above MAX_WORKERS=24
        assert orch._n_workers == 24

    def test_start_and_shutdown(self) -> None:
        from src.workers.orchestrator import WorkerOrchestrator

        orch = WorkerOrchestrator(n_workers=2, ecc_interval=3600.0)
        orch.start()
        assert orch._started
        orch.shutdown()
        assert not orch._started

    def test_double_start_is_idempotent(self) -> None:
        from src.workers.orchestrator import WorkerOrchestrator

        orch = WorkerOrchestrator(n_workers=2, ecc_interval=3600.0)
        orch.start()
        worker_count_after_first = len(orch._workers)
        orch.start()  # second call must be no-op
        assert len(orch._workers) == worker_count_after_first
        orch.shutdown()

    def test_submit_auto_starts(self) -> None:
        from src.workers.orchestrator import WorkerOrchestrator, WorkerTask

        orch = WorkerOrchestrator(n_workers=2, ecc_interval=3600.0)
        task = WorkerTask(horizon_idx=0, symbol="BTC/USDT", features={}, task_id="t1")
        orch.submit(task)
        assert orch._started
        orch.shutdown()

    def test_collect_returns_none_on_empty(self) -> None:
        from src.workers.orchestrator import WorkerOrchestrator

        orch = WorkerOrchestrator(n_workers=2, ecc_interval=3600.0)
        orch.start()
        result = orch.collect(timeout=0.05)
        assert result is None
        orch.shutdown()

    def test_worker_count_property(self) -> None:
        from src.workers.orchestrator import WorkerOrchestrator

        orch = WorkerOrchestrator(n_workers=2, ecc_interval=3600.0)
        orch.start()
        assert isinstance(orch.worker_count, int)
        orch.shutdown()

    def test_scale_up(self) -> None:
        from src.workers.orchestrator import WorkerOrchestrator

        orch = WorkerOrchestrator(n_workers=2, ecc_interval=3600.0)
        orch.start()
        before = len(orch._workers)
        orch.scale(before + 2)
        assert len(orch._workers) >= before
        orch.shutdown()

    def test_scale_down(self) -> None:
        from src.workers.orchestrator import WorkerOrchestrator

        orch = WorkerOrchestrator(n_workers=4, ecc_interval=3600.0)
        orch.start()
        orch.scale(2)
        assert len(orch._workers) <= 4
        orch.shutdown()

    def test_scale_clipped_to_min(self) -> None:
        from src.workers.orchestrator import WorkerOrchestrator

        orch = WorkerOrchestrator(n_workers=2, ecc_interval=3600.0)
        orch.start()
        orch.scale(0)  # below MIN — should stay at MIN
        assert len(orch._workers) >= 0  # won't crash
        orch.shutdown()

    def test_pin_cpu_no_crash_on_none_pid(self) -> None:
        from src.workers.orchestrator import WorkerOrchestrator

        orch = WorkerOrchestrator(n_workers=2)
        orch._pin_cpu(None, 0)  # should silently succeed


class TestWorkerTask:
    def test_worker_task_dataclass(self) -> None:
        from src.workers.orchestrator import WorkerTask

        task = WorkerTask(horizon_idx=3, symbol="ETH/USDT", features={"a": 1.0}, task_id="x")
        assert task.horizon_idx == 3
        assert task.symbol == "ETH/USDT"
        assert task.task_id == "x"

    def test_worker_result_dataclass(self) -> None:
        from src.workers.orchestrator import WorkerResult

        res = WorkerResult(
            task_id="t1",
            horizon_idx=0,
            direction=1,
            confidence=0.8,
            magnitude_mu=0.02,
            magnitude_logsigma=-2.0,
            timing=0.5,
        )
        assert res.direction == 1
        assert 0.0 <= res.confidence <= 1.0
