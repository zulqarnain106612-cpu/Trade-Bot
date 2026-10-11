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

import hashlib
import math
import multiprocessing as mp
import os
import signal
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import structlog

log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

_MIN_WORKERS = 2
# The runtime memory critical threshold is 1 GiB. Torch is imported in each
# model process, so a 24-process default can exceed that limit before fitting.
# Keep the pool small; bounded queues preserve backpressure for the ten heads.
_MAX_WORKERS = 4


@dataclass
class WorkerTask:
    task_id: str
    horizon_id: int
    symbol: str
    features: dict[str, Any]
    ecc_features: dict[str, Any] = field(default_factory=dict)
    horizon_seconds: int | None = None
    source_event_ts: float | None = None
    architecture_names: tuple[str, ...] = ()
    model_dir: str | None = None
    submitted_monotonic: float | None = None
    regime_id: int | None = None
    source_cadence_seconds: float | None = None


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
    status: str = "UNTRAINED"
    model_id: str | None = None
    model_version: str | None = None
    feature_schema_hash: str | None = None
    prediction_ts: float | None = None
    horizon_seconds: int | None = None
    target_expiry_ts: float | None = None
    target_definition: str | None = None
    training_samples: int = 0
    inference_latency_ms: float | None = None
    oos_score: float | None = None
    queue_age_ms: float | None = None
    confidence_calibrated: bool = False
    uncertainty_calibrated: bool = False
    oos_sample_count: int = 0
    source_cadence_seconds: float | None = None
    candidate_magnitude_mu: float | None = None
    candidate_model_version: str | None = None
    candidate_schema_hash: str | None = None
    candidate_error: str | None = None
    calibration_method: str | None = None


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
                    status="FAILED",
                    prediction_ts=time.time(),
                )
            )


def _run_horizon_inference(task: WorkerTask) -> WorkerResult:
    """
    Run a compatible, trained horizon artifact or report it unavailable.

    An architecture declaration is not a trained model. Until a horizon has
    a compatible artifact, this worker returns UNTRAINED and never synthesizes
    a neutral-looking prediction.
    """
    schema = tuple(sorted(task.features))
    schema_hash = hashlib.sha256("\0".join(schema).encode()).hexdigest()
    started = time.perf_counter()
    try:
        source_event_ts = None if task.source_event_ts is None else float(task.source_event_ts)
    except (TypeError, ValueError, OverflowError):
        source_event_ts = None
    horizon_seconds = task.horizon_seconds
    execution_algo = (
        "IOC"
        if horizon_seconds is not None and horizon_seconds <= 120
        else "iceberg"
        if horizon_seconds is not None and horizon_seconds <= 3600
        else "TWAP"
        if horizon_seconds is not None
        else ""
    )
    queue_age_ms = (
        max(0.0, (time.monotonic() - task.submitted_monotonic) * 1000.0)
        if task.submitted_monotonic is not None
        else 0.0
    )
    if (
        source_event_ts is None
        or not math.isfinite(source_event_ts)
        or source_event_ts <= 0
        or source_event_ts > time.time() + 1.0
    ):
        return WorkerResult(
            task_id=task.task_id,
            horizon_id=task.horizon_id,
            direction=0,
            confidence=0.0,
            magnitude_mu=0.0,
            magnitude_sigma=0.0,
            timing=0.0,
            algo=execution_algo,
            error="source event timestamp is unavailable or invalid",
            status="UNAVAILABLE_SOURCE_TIME",
            prediction_ts=time.time(),
            horizon_seconds=task.horizon_seconds,
            source_cadence_seconds=task.source_cadence_seconds,
            inference_latency_ms=(time.perf_counter() - started) * 1000.0,
            queue_age_ms=queue_age_ms,
        )
    if (
        task.horizon_seconds is not None
        and task.horizon_seconds < 60
        and (
            task.source_cadence_seconds is None
            or task.source_cadence_seconds >= task.horizon_seconds
        )
    ):
        return WorkerResult(
            task_id=task.task_id,
            horizon_id=task.horizon_id,
            direction=0,
            confidence=0.0,
            magnitude_mu=0.0,
            magnitude_sigma=0.0,
            timing=0.0,
            algo=execution_algo,
            error="source cadence is unavailable or too slow for this prediction horizon",
            status="UNAVAILABLE_CADENCE",
            prediction_ts=time.time(),
            horizon_seconds=task.horizon_seconds,
            source_cadence_seconds=task.source_cadence_seconds,
            inference_latency_ms=(time.perf_counter() - started) * 1000.0,
            queue_age_ms=queue_age_ms,
        )
    artifact_path = Path(task.model_dir or "") / "mlp.joblib"
    active_pointer = artifact_path.with_suffix(artifact_path.suffix + ".active.json")
    if (
        task.model_dir
        and "mlp" in {name.strip().lower() for name in task.architecture_names}
        and (artifact_path.is_file() or active_pointer.is_file())
    ):
        try:
            from src.models.horizon_mlp import HorizonMLPExpert

            instrument_hash = hashlib.sha256(task.symbol.encode()).hexdigest()[:16]
            expert = HorizonMLPExpert.load(
                artifact_path,
                expected_horizon_id=task.horizon_id,
                expected_instrument_hash=instrument_hash,
            )
            if not set(expert.feature_names).issubset(task.features):
                raise ValueError("inference event is missing fields from the model feature schema")
            model_features = {name: task.features[name] for name in expert.feature_names}
            magnitude = expert.predict(model_features)
            activation_state = HorizonMLPExpert.activation_state(artifact_path)
            empirical_oos_ready = expert.oos_samples >= 100
            empirical_accuracy = expert.oos_directional_accuracy
            empirical_sigma = expert.predictive_sigma
            candidate_magnitude: float | None = None
            candidate_version: str | None = None
            candidate_schema: str | None = None
            candidate_error: str | None = None
            candidate_path = artifact_path.parent / "shadow" / artifact_path.name
            if HorizonMLPExpert.artifact_available(candidate_path):
                try:
                    challenger = HorizonMLPExpert.load(
                        candidate_path,
                        expected_schema_hash=expert.schema_hash,
                        expected_horizon_id=task.horizon_id,
                        expected_instrument_hash=instrument_hash,
                    )
                    candidate_features = {
                        name: task.features[name] for name in challenger.feature_names
                    }
                    candidate_magnitude = challenger.predict(candidate_features)
                    candidate_version = str(challenger.artifact_sha256 or "")
                    candidate_schema = challenger.schema_hash
                except Exception as exc:
                    candidate_error = f"incompatible candidate artifact: {exc}"
            return WorkerResult(
                task_id=task.task_id,
                horizon_id=task.horizon_id,
                direction=1 if magnitude > 0 else -1 if magnitude < 0 else 0,
                confidence=float(empirical_accuracy or 0.0),
                magnitude_mu=magnitude,
                magnitude_sigma=empirical_sigma,
                timing=0.0,
                algo=execution_algo,
                status=(
                    "PROMOTED"
                    if activation_state == "PROMOTED"
                    else "PROBATION"
                    if activation_state == "PROBATION"
                    else "TRAINED"
                ),
                model_id=f"h{task.horizon_id}-mlp-{instrument_hash}",
                model_version=str(expert.artifact_sha256 or ""),
                feature_schema_hash=expert.schema_hash,
                prediction_ts=time.time(),
                horizon_seconds=task.horizon_seconds,
                target_expiry_ts=(
                    source_event_ts + task.horizon_seconds
                    if source_event_ts is not None and task.horizon_seconds is not None
                    else None
                ),
                target_definition="log return to first source event at or after expiry",
                training_samples=expert.n_samples,
                inference_latency_ms=(time.perf_counter() - started) * 1000.0,
                oos_score=expert.oos_directional_accuracy,
                queue_age_ms=queue_age_ms,
                candidate_magnitude_mu=candidate_magnitude,
                candidate_model_version=candidate_version,
                candidate_schema_hash=candidate_schema,
                candidate_error=candidate_error,
                confidence_calibrated=empirical_oos_ready and empirical_accuracy is not None,
                uncertainty_calibrated=empirical_oos_ready and empirical_sigma > 0,
                oos_sample_count=expert.oos_samples,
                calibration_method=("prequential_empirical" if empirical_oos_ready else None),
            )
        except Exception as exc:
            return WorkerResult(
                task_id=task.task_id,
                horizon_id=task.horizon_id,
                direction=0,
                confidence=0.0,
                magnitude_mu=0.0,
                magnitude_sigma=0.0,
                timing=0.0,
                algo=execution_algo,
                error=f"incompatible horizon artifact: {exc}",
                status="FAILED",
                feature_schema_hash=schema_hash,
                prediction_ts=time.time(),
                horizon_seconds=task.horizon_seconds,
                inference_latency_ms=(time.perf_counter() - started) * 1000.0,
                queue_age_ms=queue_age_ms,
            )
    shadow_path = artifact_path.parent / "shadow" / artifact_path.name
    shadow_available = False
    if task.model_dir and "mlp" in {name.strip().lower() for name in task.architecture_names}:
        from src.models.horizon_mlp import HorizonMLPExpert

        shadow_available = HorizonMLPExpert.artifact_available(shadow_path)
    if shadow_available:
        try:
            from src.models.horizon_mlp import HorizonMLPExpert

            instrument_hash = hashlib.sha256(task.symbol.encode()).hexdigest()[:16]
            challenger = HorizonMLPExpert.load(
                shadow_path,
                expected_horizon_id=task.horizon_id,
                expected_instrument_hash=instrument_hash,
            )
            shadow_features = {name: task.features[name] for name in challenger.feature_names}
            candidate_magnitude = challenger.predict(shadow_features)
            version = str(challenger.artifact_sha256 or "")
            return WorkerResult(
                task_id=task.task_id,
                horizon_id=task.horizon_id,
                direction=0,
                confidence=0.0,
                magnitude_mu=0.0,
                magnitude_sigma=0.0,
                timing=0.0,
                algo=execution_algo,
                status="CANDIDATE_SHADOW",
                model_id=f"h{task.horizon_id}-baseline-{instrument_hash}",
                model_version="zero-baseline-v1",
                feature_schema_hash=challenger.schema_hash,
                prediction_ts=time.time(),
                horizon_seconds=task.horizon_seconds,
                target_expiry_ts=(
                    source_event_ts + task.horizon_seconds
                    if source_event_ts is not None and task.horizon_seconds is not None
                    else None
                ),
                target_definition="log return to first source event at or after expiry",
                training_samples=challenger.n_samples,
                inference_latency_ms=(time.perf_counter() - started) * 1000.0,
                queue_age_ms=queue_age_ms,
                candidate_magnitude_mu=candidate_magnitude,
                candidate_model_version=version,
                candidate_schema_hash=challenger.schema_hash,
            )
        except Exception as exc:
            log.warning(
                "horizon_shadow_inference_failed", horizon_id=task.horizon_id, error=str(exc)
            )
    reason = (
        "no trained horizon artifact is configured"
        if not task.model_dir or not task.architecture_names
        else "no compatible trained horizon artifact is available"
    )
    return WorkerResult(
        task_id=task.task_id,
        horizon_id=task.horizon_id,
        direction=0,
        confidence=0.0,
        magnitude_mu=0.0,
        magnitude_sigma=0.0,
        timing=0.0,
        algo=execution_algo,
        error=reason,
        status="UNTRAINED",
        feature_schema_hash=schema_hash,
        prediction_ts=time.time(),
        horizon_seconds=task.horizon_seconds,
        target_expiry_ts=(
            source_event_ts + task.horizon_seconds
            if source_event_ts is not None and task.horizon_seconds is not None
            else None
        ),
        target_definition="log return to first source event at or after expiry",
        inference_latency_ms=(time.perf_counter() - started) * 1000.0,
        queue_age_ms=queue_age_ms,
    )


# The full ECC feature set, at values that assert nothing. Every iteration
# starts from these so a partially-failing pipeline emits a stable key set --
# consumers index these by name and a missing key is a KeyError downstream.
_ECC_NEUTRAL_FEATURES: dict[str, float] = {
    "cluster_flow_score": 0.0,
    "whale_count": 0,
    "total_whale_btc": 0.0,
    "dark_pool_pressure": 0.0,
    "tornado_deposits": 0,
    "hodler_index": 0.0,
    "supply_shock_risk": 0.0,
    "young_supply_pct": 0.0,
    "aged_supply_pct": 0.0,
    "mean_age_days": 0.0,
    "musig2_count": 0,
    "privacy_score": 0.0,
    "smart_money_divergence": 0.0,
    "p2tr_input_count": 0,
    "ecdsa_weaknesses": 0,
    "ecdsa_keys_recovered": 0,
    # The five keys RLStateBuilder and ECCHead consume. They are read by name,
    # so a producer that never emits them leaves those state slots pinned at
    # zero and the agent silently trains on a constant. Kept alongside the raw
    # diagnostics above rather than replacing them: the counts are useful in
    # logs, the normalised signals are what the models take.
    "ecdsa_weakness": 0.0,
    "schnorr_divergence": 0.0,
}

# The ECC inputs RLStateBuilder.build reads, in its state-vector order.
ECC_MODEL_FEATURES: tuple[str, ...] = (
    "cluster_flow_score",
    "ecdsa_weakness",
    "schnorr_divergence",
    "hodler_index",
    "dark_pool_pressure",
)


def _utxos_with_ages(utxos: list[dict]) -> list[dict]:
    """Give listunspent entries the ``timestamp`` the UTXO age curve needs.

    ``listunspent`` reports ``confirmations``, not a creation time, so feeding
    its output straight to ``compute_hodler_index`` would date every UTXO to
    now and report a hodler index of zero regardless of the real age
    distribution. Bitcoin targets 10-minute blocks, so confirmations are the
    age estimate. Entries that already carry a timestamp are left alone.
    """
    now = time.time()
    converted: list[dict] = []
    for utxo in utxos:
        if "timestamp" in utxo:
            converted.append(utxo)
            continue
        confirmations = float(utxo.get("confirmations", 0) or 0)
        converted.append({**utxo, "timestamp": now - confirmations * 600.0})
    return converted


def _fetch_recent_block_transactions(
    rpc_url: str,
    rpc_user: str,
    rpc_pass: str,
    timeout: float = 30.0,
) -> list[dict]:
    """Fetch the transactions of the current best block, with raw hex.

    Verbosity 2 returns decoded transactions including ``hex``, which the ECDSA
    scanner needs; the Taproot parser reads the decoded inputs from the same
    payload, so one round trip serves both.
    """
    import requests

    def _call(method: str, params: list) -> Any:
        resp = requests.post(
            rpc_url,
            json={"jsonrpc": "1.0", "id": "ecc", "method": method, "params": params},
            auth=(rpc_user, rpc_pass),
            timeout=timeout,
        )
        payload = resp.json()
        if payload.get("error"):
            raise RuntimeError(f"{method} failed: {payload['error']}")
        return payload["result"]

    best_hash = _call("getbestblockhash", [])
    block = _call("getblock", [best_hash, 2])
    return list(block.get("tx") or [])


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

    from src.ecc.ecdsa_scan import ECDSAScanner
    from src.ecc.secp256k1_cluster import Secp256k1ClusterWorker
    from src.ecc.zksnark_detect import ZkSnarkDetector

    clusterer = Secp256k1ClusterWorker(rpc_url=rpc_url, rpc_user=rpc_user, rpc_pass=rpc_pass)
    zk_detector = ZkSnarkDetector()
    # Stateful across iterations: nonce reuse is only detectable by comparing an
    # r-value against ones seen in earlier blocks.
    ecdsa_scanner = ECDSAScanner()

    while True:
        ecc_result = _run_ecc_cycle(
            clusterer=clusterer,
            zk_detector=zk_detector,
            ecdsa_scanner=ecdsa_scanner,
            rpc_url=rpc_url,
            rpc_user=rpc_user,
            rpc_pass=rpc_pass,
        )
        queue_out.put({"type": "ecc", "result": ecc_result})
        time.sleep(interval_seconds)


def _run_ecc_cycle(
    clusterer: Any,
    zk_detector: Any,
    ecdsa_scanner: Any,
    rpc_url: str,
    rpc_user: str,
    rpc_pass: str,
) -> dict[str, Any]:
    """Run one pass of every ECC analysis and return the merged feature set.

    Split out of the worker loop so the pipeline is testable without a thread,
    a queue, or a live node.
    """
    from src.ecc.schnorr_taproot import parse_taproot_block
    from src.ecc.utxo_curve import compute_hodler_index, utxo_curve_feature_vector

    ecc_result: dict[str, Any] = dict(_ECC_NEUTRAL_FEATURES)
    ecc_validity = {
        "cluster": False,
        "zksnark": False,
        "utxo": False,
        "taproot": False,
        "ecdsa": False,
    }

    # Each analysis is isolated: an unreachable Ethereum node must not
    # discard the Bitcoin clustering features that did resolve.
    try:
        cluster_result = clusterer.run()
        ecc_result.update(
            cluster_flow_score=cluster_result.flow_score,
            whale_count=cluster_result.whale_count,
            total_whale_btc=cluster_result.total_whale_btc,
        )
        ecc_validity["cluster"] = True
    except Exception as exc:
        log.warning("ecc_cluster_failed", exc=str(exc))
        cluster_result = None

    try:
        zk_result = zk_detector.detect_mixing_flows(block_lookback=3)
        ecc_result.update(
            dark_pool_pressure=zk_result.dark_pool_pressure,
            tornado_deposits=zk_result.tornado_deposits_detected,
        )
        ecc_validity["zksnark"] = True
    except Exception as exc:
        log.warning("ecc_zksnark_failed", exc=str(exc))

    # UTXO age curve reuses the listunspent result the clusterer fetched.
    try:
        if cluster_result is not None and cluster_result.utxos:
            ecc_result.update(
                utxo_curve_feature_vector(
                    compute_hodler_index(_utxos_with_ages(cluster_result.utxos))
                )
            )
            ecc_validity["utxo"] = True
    except Exception as exc:
        log.warning("ecc_utxo_curve_failed", exc=str(exc))

    try:
        transactions = _fetch_recent_block_transactions(rpc_url, rpc_user, rpc_pass)
    except Exception as exc:
        log.warning("ecc_block_fetch_failed", exc=str(exc))
        transactions = []

    try:
        if transactions:
            taproot = parse_taproot_block(transactions)
            ecc_result.update(
                musig2_count=taproot.musig2_count,
                privacy_score=taproot.privacy_score,
                smart_money_divergence=taproot.smart_money_divergence,
                p2tr_input_count=taproot.p2tr_input_count,
                schnorr_divergence=taproot.smart_money_divergence,
            )
            ecc_validity["taproot"] = True
    except Exception as exc:
        log.warning("ecc_taproot_failed", exc=str(exc))

    try:
        weaknesses = 0
        keys_recovered = 0
        # The models want severity, not volume: one recoverable key matters
        # more than many low-confidence r-value collisions.
        peak_risk = 0.0
        for tx in transactions:
            raw_hex = tx.get("hex")
            if not raw_hex:
                continue
            for weakness in ecdsa_scanner.scan_transaction(raw_hex):
                weaknesses += 1
                keys_recovered += int(weakness.privkey_extracted)
                peak_risk = max(peak_risk, float(weakness.risk_score))
        ecc_result.update(
            ecdsa_weaknesses=weaknesses,
            ecdsa_keys_recovered=keys_recovered,
            ecdsa_weakness=peak_risk,
        )
        ecc_validity["ecdsa"] = True
    except Exception as exc:
        log.warning("ecc_ecdsa_scan_failed", exc=str(exc))

    ecc_result["_validity"] = ecc_validity
    ecc_result["_observed_at"] = time.time()
    log.info(
        "ecc_pipeline_complete",
        flow_score=ecc_result["cluster_flow_score"],
        tx_scanned=len(transactions),
    )
    return ecc_result


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
        ecc_enabled: bool = True,
    ) -> None:
        n_workers = max(self.MIN_WORKERS, min(n_workers, self.MAX_WORKERS))
        self._n_workers = n_workers
        self._btc_rpc_url = btc_rpc_url
        self._btc_rpc_user = btc_rpc_user
        self._btc_rpc_pass = btc_rpc_pass
        self._ecc_interval = ecc_interval
        self._ecc_enabled = bool(ecc_enabled)

        self._queue_in: mp.Queue = mp.Queue(maxsize=1000)
        self._queue_out: mp.Queue = mp.Queue(maxsize=1000)
        self._dropped_tasks = 0
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

        if self._ecc_enabled and (self._ecc_thread is None or not self._ecc_thread.is_alive()):
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
        task.submitted_monotonic = time.monotonic()
        try:
            self._queue_in.put_nowait(task)
        except Exception as exc:
            # Queue.Full differs across multiprocessing implementations; keep
            # the error explicit and expose the loss counter to runtime health.
            self._dropped_tasks += 1
            log.warning(
                "horizon_task_dropped",
                task_id=task.task_id,
                horizon_id=task.horizon_id,
                dropped_total=self._dropped_tasks,
                error=str(exc),
            )
            raise

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

    @property
    def dropped_tasks(self) -> int:
        return self._dropped_tasks

    def health_snapshot(self) -> dict[str, Any]:
        """Bounded scheduler health for the operator-facing horizon status."""
        try:
            input_depth = self._queue_in.qsize()
        except (NotImplementedError, OSError, ValueError):
            input_depth = None
        try:
            output_depth = self._queue_out.qsize()
        except (NotImplementedError, OSError, ValueError):
            output_depth = None
        return {
            "configured_workers": self._n_workers,
            "alive_model_workers": sum(worker.is_alive() for worker in self._workers),
            "input_queue_depth": input_depth,
            "output_queue_depth": output_depth,
            "dropped_tasks": self._dropped_tasks,
            "started": self._started,
            "ecc_enabled": self._ecc_enabled,
        }
