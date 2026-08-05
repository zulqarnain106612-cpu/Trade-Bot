"""
WorkerOrchestrator — parallel horizon model workers + dedicated ECC thread.

Architecture:
  - N-1 multiprocessing workers for horizon model inference
  - 1 dedicated threading.Thread for ECC pipeline (GIL released via coincurve C bindings)
  - NUMA locality pinning via os.sched_setaffinity (Linux only)
  - Graceful shutdown via poison-pill None messages

Queue protocol:
  queue_in:  task dict → {task_id, horizon_id, symbol, features}
  queue_out: result dict → {task_id, horizon_id, direction, confidence, ...}

ECC results are always keyed with {'type': 'ecc', 'result': {...}}.
"""

from __future__ import annotations

import multiprocessing as mp
import os
import signal
import threading
import time
from dataclasses import dataclass, field
from typing import Any

import structlog


log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

_MIN_WORKERS = 2
_MAX_WORKERS = 24


@dataclass
class WorkerTask:
    task_id: str
    horizon_id: int
    symbol: str
    features: dict[str, Any]
    ecc_features: dict[str, float] = field(default_factory=dict)


@dataclass
class WorkerResult:
    task_id: str
    horizon_id: int
    direction: int  # 1=long, -1=short, 0=flat
    confidence: float  # [0, 1]
    magnitude_mu: float  # predicted price move
    magnitude_sigma: float  # uncertainty
    timing: float  # entry delay probability [0, 1]
    algo: str  # IOC / iceberg / TWAP
    error: str | None = None


def _model_worker(
    worker_id: int,
    queue_in: mp.Queue[WorkerTask | None],
    queue_out: mp.Queue[WorkerResult | dict],
) -> None:
    """
    Horizon model worker process.

    Listens on queue_in for WorkerTask objects; processes and puts
    WorkerResult on queue_out.  Exits cleanly on None sentinel.
    """
    signal.signal(signal.SIGTERM, lambda *_: exit(0))
    log.info("model_worker_started", worker_id=worker_id, pid=os.getpid())

    while True:
        task = queue_in.get()
        if task is None:
            log.info("model_worker_stopping", worker_id=worker_id)
            break

        try:
            result = _run_horizon_inference(task)
            queue_out.put(result)
        except Exception as exc:
            log.warning("model_worker_error", task_id=task.task_id, exc=str(exc))
            queue_out.put(
                WorkerResult(
                    task_id=task.task_id,
                    horizon_id=task.horizon_id,
                    direction=0,
                    confidence=0.0,
                    magnitude_mu=0.0,
                    magnitude_sigma=1.0,
                    timing=0.5,
                    algo="TWAP",
                    error=str(exc),
                )
            )


def _run_horizon_inference(task: WorkerTask) -> WorkerResult:
    """
    Run the model head(s) for a single horizon and return a result.

    In the full implementation, this would load the per-horizon checkpoint
    and run the appropriate model heads (CNN+TCN for h1, TFT+GRU for h4, etc.)
    through the cross-attention fusion.  Currently routes to a placeholder
    that returns a random-walk prediction until models are trained.
    """
    import hashlib

    import numpy as np

    # Deterministic pseudo-inference based on features until training completes
    feature_hash = int(
        hashlib.md5(str(sorted(task.features.items())).encode(), usedforsecurity=False).hexdigest(),
        16,
    )  # nosec B324
    rng = np.random.default_rng(feature_hash % 2**32)

    confidence = float(np.clip(rng.beta(2, 2), 0.0, 1.0))
    direction = int(rng.choice([-1, 0, 1], p=[0.3, 0.4, 0.3]))
    magnitude = float(rng.normal(0, 0.01))
    timing = float(rng.uniform(0, 1))

    # Algo selection based on horizon index and confidence
    if task.horizon_id <= 1:
        algo = "IOC"
    elif task.horizon_id <= 4:
        algo = "iceberg"
    else:
        algo = "TWAP"

    return WorkerResult(
        task_id=task.task_id,
        horizon_id=task.horizon_id,
        direction=direction if confidence > 0.65 else 0,
        confidence=confidence,
        magnitude_mu=magnitude,
        magnitude_sigma=abs(magnitude) * 0.5,
        timing=timing,
        algo=algo,
    )


def _ecc_worker(
    queue_out: mp.Queue[WorkerResult | dict],
    rpc_url: str,
    rpc_user: str,
    rpc_pass: str,
    interval_seconds: float = 60.0,
) -> None:
    """
    Dedicated ECC worker thread.

    Runs secp256k1 clustering + ECDSA scan + Schnorr/Taproot + UTXO + zkSNARK
    in a tight loop, sleeping `interval_seconds` between runs.

    Uses coincurve C bindings which release the GIL, so this runs in a thread
    without blocking the asyncio event loop.
    """
    log.info("ecc_worker_started", rpc_url=rpc_url)

    from src.ecc.secp256k1_cluster import Secp256k1ClusterWorker
    from src.ecc.zksnark_detect import ZkSnarkDetector

    clusterer = Secp256k1ClusterWorker(rpc_url=rpc_url, rpc_user=rpc_user, rpc_pass=rpc_pass)
    zk_detector = ZkSnarkDetector()

    while True:
        try:
            # secp256k1 clustering
            cluster_result = clusterer.run()

            # zkSNARK dark pool
            zk_result = zk_detector.detect_mixing_flows(block_lookback=3)

            ecc_result = {
                "cluster_flow_score": cluster_result.flow_score,
                "whale_count": cluster_result.whale_count,
                "total_whale_btc": cluster_result.total_whale_btc,
                "dark_pool_pressure": zk_result.dark_pool_pressure,
                "tornado_deposits": zk_result.tornado_deposits_detected,
            }

            queue_out.put({"type": "ecc", "result": ecc_result})
            log.info("ecc_pipeline_complete", flow_score=cluster_result.flow_score)

        except Exception as exc:
            log.warning("ecc_worker_error", exc=str(exc))
            queue_out.put(
                {
                    "type": "ecc",
                    "result": {
                        "cluster_flow_score": 0.0,
                        "whale_count": 0,
                        "total_whale_btc": 0.0,
                        "dark_pool_pressure": 0.0,
                        "tornado_deposits": 0,
                    },
                }
            )

        time.sleep(interval_seconds)


class WorkerOrchestrator:
    """
    Manages a pool of multiprocessing model workers + 1 ECC thread.

    - MIN_WORKERS=2, MAX_WORKERS=24
    - NUMA locality pinning via os.sched_setaffinity (Linux only)
    - Graceful scale-up and scale-down
    - ECC thread on separate Python thread (GIL released via C extension)
    """

    MIN_WORKERS: int = _MIN_WORKERS
    MAX_WORKERS: int = _MAX_WORKERS

    def __init__(
        self,
        n_workers: int = 8,
        btc_rpc_url: str = "http://127.0.0.1:8332",
        btc_rpc_user: str = "crypto",
        btc_rpc_pass: str = "crypto",
        ecc_interval: float = 60.0,
    ) -> None:
        n_workers = max(self.MIN_WORKERS, min(n_workers, self.MAX_WORKERS))
        self._n_workers = n_workers
        self._btc_rpc_url = btc_rpc_url
        self._btc_rpc_user = btc_rpc_user
        self._btc_rpc_pass = btc_rpc_pass
        self._ecc_interval = ecc_interval

        self._queue_in: mp.Queue = mp.Queue(maxsize=1000)
        self._queue_out: mp.Queue = mp.Queue(maxsize=1000)
        self._workers: list[mp.Process] = []
        self._ecc_thread: threading.Thread | None = None
        self._started = False

    def start(self) -> None:
        """Spawn worker processes and ECC thread."""
        if self._started:
            return
        self._spawn(self._n_workers)
        self._started = True
        log.info("worker_orchestrator_started", n_workers=len(self._workers))

    def _spawn(self, n: int) -> None:
        model_workers = max(0, n - 1)
        for i in range(model_workers):
            p = mp.Process(
                target=_model_worker,
                args=(i, self._queue_in, self._queue_out),
                daemon=True,
            )
            p.start()
            self._pin_cpu(p.pid, i)
            self._workers.append(p)

        if self._ecc_thread is None or not self._ecc_thread.is_alive():
            self._ecc_thread = threading.Thread(
                target=_ecc_worker,
                args=(
                    self._queue_out,
                    self._btc_rpc_url,
                    self._btc_rpc_user,
                    self._btc_rpc_pass,
                    self._ecc_interval,
                ),
                daemon=True,
            )
            self._ecc_thread.start()

    def _pin_cpu(self, pid: int | None, worker_idx: int) -> None:
        """Pin a worker process to a specific CPU core for NUMA locality."""
        if pid is None:
            return
        try:
            cpu = worker_idx % os.cpu_count()  # type: ignore[operator]
            os.sched_setaffinity(pid, {cpu})
        except (AttributeError, OSError):
            pass  # Windows / macOS don't support sched_setaffinity

    def submit(self, task: WorkerTask) -> None:
        """Submit a task for processing by a model worker."""
        if not self._started:
            self.start()
        self._queue_in.put_nowait(task)

    def collect(self, timeout: float = 5.0) -> WorkerResult | dict | None:
        """Collect one result from the output queue. Returns None on timeout."""
        try:
            return self._queue_out.get(timeout=timeout)  # type: ignore[return-value]
        except Exception:
            return None

    def scale(self, n: int) -> None:
        """Scale worker pool up or down to exactly `n` total workers."""
        n = max(self.MIN_WORKERS, min(n, self.MAX_WORKERS))
        delta = n - len(self._workers)
        if delta > 0:
            self._spawn(delta)
            log.info("workers_scaled_up", new_total=len(self._workers))
        elif delta < 0:
            for _ in range(-delta):
                self._queue_in.put(None)  # graceful SIGTERM
                proc = self._workers.pop()
                proc.join(timeout=5)
            log.info("workers_scaled_down", new_total=len(self._workers))

    def shutdown(self) -> None:
        """Drain queues and join all workers."""
        for _ in self._workers:
            self._queue_in.put(None)
        for w in self._workers:
            w.join(timeout=10)
        if self._ecc_thread and self._ecc_thread.is_alive():
            self._ecc_thread.join(timeout=5)
        self._workers.clear()
        self._started = False
        log.info("worker_orchestrator_shutdown")

    @property
    def worker_count(self) -> int:
        return len(self._workers)
