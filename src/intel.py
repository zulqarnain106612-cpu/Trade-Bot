"""
CryptoIntelligence — central orchestrator for crypto-intel-v6.

Coordinates:
  - 10-horizon WorkerOrchestrator (model inference workers + ECC thread)
  - Feature bus (microstructure, on-chain, derivatives, NLP, mempool)
  - ECC pipeline (secp256k1, ECDSA, Schnorr/Taproot, UTXO, zkSNARK)
  - Causal layer (DoWhy SCM, AssetGNN, Granger)
  - CrossAttentionFusion + MetaNetwork
  - one OOS-weighted horizon fusion + RiskGate v2 (CVaR-Kelly + ADWIN)
  - SmartOrderRouter, RLExecutionAgent, PostTradeAnalytics
  - Self-upgrade loop (MAML, Optuna, ShadowDeploy, ModelRegistry)
  - DuckDBStore + RedpandaFeeds (observability bus)
"""

from __future__ import annotations

import asyncio
import hashlib
import math
import os
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import structlog
import yaml

from src.models.horizon_config import load_horizon_specs as _load_horizon_specs
from src.workers.orchestrator import WorkerResult


log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)


def _validated_risk_context(
    context: dict[str, float] | None,
    *,
    now: float,
    max_age_seconds: float,
) -> tuple[dict[str, float] | None, str | None]:
    required = {"atr", "cvar", "drawdown", "daily_loss", "odds", "as_of_ts"}
    if context is None or not required.issubset(context):
        return None, "validated portfolio risk context unavailable"
    try:
        values = {key: float(context[key]) for key in required}
    except (TypeError, ValueError, OverflowError):
        return None, "portfolio risk context contains malformed values"
    if not all(math.isfinite(value) for value in values.values()):
        return None, "portfolio risk context contains non-finite values"
    age = now - values["as_of_ts"]
    if (
        age < 0
        or age > max_age_seconds
        or values["atr"] <= 0
        or values["cvar"] <= 0
        or values["odds"] <= 0
        or values["drawdown"] < 0
        or values["daily_loss"] < 0
    ):
        return None, "portfolio risk context is stale or outside valid bounds"
    return values, None


_CONFIG_PATH = Path(os.environ.get("INTEL_CONFIG", "config/intelligence.yaml"))
_HORIZONS_CONFIG = Path(os.environ.get("HORIZONS_CONFIG", "config/horizons.yaml"))
_RISK_CONFIG = Path(os.environ.get("RISK_CONFIG", "config/risk.yaml"))


@dataclass
class IntelSignal:
    """Output signal produced by CryptoIntelligence for one bar."""

    symbol: str
    direction: int  # -1 / 0 / +1
    size_pct: float  # [0, 0.05] fraction of capital
    confidence: float
    horizon_idx: int  # winning horizon
    algo: str  # IOC / iceberg / TWAP
    ecc_anomaly: float | None  # None when provider validity is unavailable
    conflict: bool  # True if horizons disagreed
    regime_id: int
    ts: float = field(default_factory=time.time)
    meta: dict[str, Any] = field(default_factory=dict)


def load_horizon_specs() -> dict[str, Any]:
    """Public compatibility wrapper for the canonical horizon configuration loader."""
    return _load_horizon_specs(_HORIZONS_CONFIG)


def horizon_entry(key: str, spec: dict[str, Any] | None) -> dict[str, Any]:
    """One horizon's static fields, shared by the live and disabled paths."""
    spec = spec or {}
    return {
        "key": key,
        "id": int(spec.get("id", 0)),
        "label": str(spec.get("label", key)),
        "seconds": spec.get("seconds"),
        "models": list(spec.get("models") or []),
        "last_prediction": None,
        "age_seconds": None,
        "status": "CONFIGURED",
        "model_status": "UNTRAINED",
        "artifact_available": False,
        "lifecycle_state": "UNPROMOTED",
        "calibration_state": "UNCALIBRATED",
        "error": "no validated horizon model is active",
    }


def sorted_horizon_entries() -> list[dict[str, Any]]:
    """Every declared horizon as a static entry, shortest term first."""
    specs = load_horizon_specs()
    return [
        horizon_entry(key, spec)
        for key, spec in sorted(specs.items(), key=lambda kv: (kv[1] or {}).get("id", 0))
    ]


class CryptoIntelligence:
    """
    Top-level crypto-intel-v6 inference pipeline.

    Instantiate once; call on_bar() for each new OHLCV bar.
    Call start() before first use; call close() for clean shutdown.
    """

    def __init__(self, config_path: Path = _CONFIG_PATH) -> None:
        self._cfg = self._load_config(config_path)
        self._risk_cfg = self._load_config(_RISK_CONFIG)

        # Feature subsystems
        from src.causal.dowhy_scm import DoWhySCM
        from src.causal.granger import GrangerCausalityDetector
        from src.features.derivatives import DerivativesFeatureExtractor
        from src.features.mempool import fetch_mempool_features
        from src.features.microstructure import (
            KyleLambdaEstimator,
            VPINTracker,
        )
        from src.features.onchain import BitcoinRPCClient, OnChainFeatureExtractor

        self._vpin = VPINTracker()
        self._kyle = KyleLambdaEstimator()
        rpc_cfg = self._cfg.get("btc_node_rpc", {})
        self._onchain = OnChainFeatureExtractor(
            rpc=BitcoinRPCClient(
                url=rpc_cfg.get("url", "http://127.0.0.1:8332"),
                user=rpc_cfg.get("user", "crypto"),
                password=rpc_cfg.get("pass", "crypto"),
            )
        )
        self._derivatives = DerivativesFeatureExtractor()
        self._scm = DoWhySCM()
        self._granger = GrangerCausalityDetector()
        self._fetch_mempool = fetch_mempool_features

        # Risk gate; cross-horizon fusion is the explicit component below.
        from src.risk.gate import RiskGate

        self._risk_gate = RiskGate.from_config(self._risk_cfg)

        # Worker orchestrator (model workers + ECC thread)
        from src.workers.orchestrator import WorkerOrchestrator

        btc_rpc = self._cfg.get("btc_node_rpc", {})
        self._orchestrator = WorkerOrchestrator(
            n_workers=self._cfg.get("n_workers", 8),
            btc_rpc_url=btc_rpc.get("url", "http://127.0.0.1:8332"),
            btc_rpc_user=btc_rpc.get("user", "crypto"),
            btc_rpc_pass=btc_rpc.get("pass", "crypto"),
            ecc_interval=self._cfg.get("ecc_interval_seconds", 60.0),
            ecc_enabled=bool(self._cfg.get("ecc_enabled", True)),
        )

        # Observability
        from src.data.duckdb_store import DuckDBStore

        # DUCKDB_PATH is honoured before the built-in default so a test run or
        # a sandboxed deployment can redirect the database without a config
        # file; DuckDBStore reads the same variable for its own default.
        self._duckdb = DuckDBStore(
            path=Path(
                self._cfg.get("duckdb_path")
                or os.environ.get("DUCKDB_PATH", "./models/crypto_intel.duckdb")
            )
        )

        # Self-upgrade
        from src.upgrade.maml import HorizonMAMLAdapter
        from src.upgrade.registry import ModelRegistry

        self._maml = HorizonMAMLAdapter(
            checkpoint_dir=Path(self._cfg.get("checkpoint_dir", "./models/horizons")),
        )
        self._registry = ModelRegistry(
            tracking_uri=self._cfg.get("observability", {}).get(
                "mlflow_uri", "http://127.0.0.1:5000"
            ),
        )

        # Horizon architectures. Validated here so a bad model name in
        # horizons.yaml fails at startup rather than at the first retrain.
        from src.models.architectures import load_horizon_architectures

        self._horizon_models = load_horizon_architectures(_HORIZONS_CONFIG)
        from src.models.horizon_learning import HorizonLearningStore

        self._checkpoint_dir = Path(self._cfg.get("checkpoint_dir", "./models/horizons"))
        self._learning_store = HorizonLearningStore(self._checkpoint_dir)
        recovered = self._learning_store.recover_promotions(self._checkpoint_dir)
        if recovered:
            log.warning("horizon_promotions_recovered", components=recovered)
        self._training_executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="horizon-label-trainer"
        )
        self._training_future: Future[dict[str, int]] | None = None
        self._learning_status: dict[str, Any] = {
            "last_update": None,
            "last_error": "waiting for source timestamped market events",
            "matured": 0,
            "updated": 0,
            "rejected": 0,
        }

        # State
        self._ecc_state: dict[str, Any] = {}
        self._ecc_max_age = max(60.0, float(self._cfg.get("ecc_interval_seconds", 60.0)) * 3.0)
        self._regime_id: int | None = None
        self._started = False

        # Last result per horizon id, with the wall-clock time it was
        # recorded: horizon_status() reports staleness, and a horizon whose
        # worker has died must be distinguishable from one that is merely
        # predicting flat.
        self._last_horizon_results: dict[int, tuple[WorkerResult, float]] = {}
        self._last_horizon_ts: float | None = None
        self._last_horizon_symbol: str | None = None
        self._source_cadence_seconds: float | None = None
        self._source_timeframe: str | None = None
        self._last_decision: Any | None = None

    def _load_config(self, path: Path) -> dict:
        try:
            with open(path, encoding="utf-8") as fh:
                return yaml.safe_load(fh) or {}
        except FileNotFoundError:
            log.warning("config_not_found", path=str(path))
            return {}

    def horizon_status(self) -> list[dict[str, Any]]:
        """
        The full horizon term structure: every horizon declared in
        horizons.yaml, with its last prediction if one has been made.

        Returns one entry per *declared* horizon, not per horizon that has
        reported. A horizon whose worker never answered still appears, with
        ``last_prediction`` None — otherwise a dead worker would simply
        vanish from the view rather than showing up as a problem.

        Ordered by the horizon's own ``id`` so the result reads as a term
        structure (shortest first), which is the order a reader expects and
        the order the chart plots.
        """
        now = time.time()
        out: list[dict[str, Any]] = []
        learning_store = getattr(self, "_learning_store", None)
        learning_stats = (
            learning_store.stats(
                symbol=self._last_horizon_symbol,
                source_timeframe=self._source_timeframe,
            )
            if learning_store is not None and self._last_horizon_symbol is not None
            else {}
        )

        for entry in sorted_horizon_entries():
            entry.update(learning_stats.get(entry["id"], {}))
            entry["source_cadence_seconds"] = self._source_cadence_seconds
            entry["source_timeframe"] = self._source_timeframe
            if self._last_horizon_symbol and self._source_timeframe:
                entry["candidate_evaluation"] = (
                    learning_store.latest_candidate_evaluation(
                        symbol=self._last_horizon_symbol,
                        horizon_id=int(entry["id"]),
                        source_timeframe=self._source_timeframe,
                    )
                    if learning_store is not None
                    else None
                )
            instrument_hash = (
                hashlib.sha256(self._last_horizon_symbol.encode()).hexdigest()[:16]
                if self._last_horizon_symbol
                else None
            )
            if instrument_hash is not None:
                from src.models.horizon_mlp import HorizonMLPExpert

                artifact_path = (
                    self._checkpoint_dir / f"h{entry['id'] + 1}" / instrument_hash / "mlp.joblib"
                )
                entry["artifact_available"] = HorizonMLPExpert.artifact_available(artifact_path)
                entry["active_lifecycle_state"] = (
                    HorizonMLPExpert.activation_state(artifact_path)
                    if entry["artifact_available"]
                    else "UNPROMOTED"
                )
                candidate_path = artifact_path.parent / "candidate" / artifact_path.name
                entry["candidate_artifact_available"] = HorizonMLPExpert.artifact_available(
                    candidate_path
                )
                shadow_path = artifact_path.parent / "shadow" / artifact_path.name
                entry["shadow_artifact_available"] = HorizonMLPExpert.artifact_available(
                    shadow_path
                )
                if entry["candidate_artifact_available"] and not entry["artifact_available"]:
                    entry["lifecycle_state"] = "CANDIDATE_UNPROMOTED"
            recorded = self._last_horizon_results.get(entry["id"])
            if recorded is not None:
                r, ts = recorded
                entry["last_prediction"] = {
                    "direction": r.direction,
                    "confidence": r.confidence,
                    "magnitude_mu": r.magnitude_mu,
                    "magnitude_sigma": r.magnitude_sigma,
                    "timing": r.timing,
                    "algo": r.algo,
                    "error": r.error,
                    "status": r.status,
                    "model_id": r.model_id,
                    "model_version": r.model_version,
                    "prediction_ts": r.prediction_ts,
                    "horizon_seconds": r.horizon_seconds,
                    "target_expiry_ts": r.target_expiry_ts,
                    "target_definition": r.target_definition,
                    "feature_schema_hash": r.feature_schema_hash,
                    "training_samples": r.training_samples,
                    "inference_latency_ms": r.inference_latency_ms,
                    "queue_age_ms": r.queue_age_ms,
                    "oos_score": r.oos_score,
                    "oos_sample_count": r.oos_sample_count,
                    "confidence_calibrated": r.confidence_calibrated,
                    "uncertainty_calibrated": r.uncertainty_calibrated,
                    "candidate_magnitude_mu": r.candidate_magnitude_mu,
                    "candidate_model_version": r.candidate_model_version,
                    "candidate_schema_hash": r.candidate_schema_hash,
                    "candidate_error": r.candidate_error,
                    "calibration_method": r.calibration_method,
                }
                entry["calibration_state"] = (
                    "CALIBRATED"
                    if r.confidence_calibrated and r.uncertainty_calibrated
                    else "UNCALIBRATED"
                )
                entry["age_seconds"] = round(now - ts, 3)
                entry["model_status"] = r.status
                stale_after = float(self._cfg.get("max_prediction_age_seconds", 20.0))
                entry["status"] = (
                    "DEGRADED"
                    if r.error
                    else "STALE"
                    if entry["age_seconds"] > stale_after
                    else r.status
                )
                entry["error"] = r.error or (
                    "last horizon result exceeded the configured freshness budget"
                    if entry["status"] == "STALE"
                    else None
                )
            elif self._started:
                entry["status"] = "DATA_PENDING"
                entry["error"] = "waiting for first horizon result"

            out.append(entry)

        return out

    @property
    def last_decision(self) -> dict[str, Any] | None:
        """Most recent fused decision; never carries forward across a new bar."""
        return None if self._last_decision is None else self._last_decision.to_dict()

    @property
    def learning_status(self) -> dict[str, Any]:
        self._collect_training_completion()
        return {
            **self._learning_status,
            "training_active": self._training_future is not None,
        }

    def _collect_training_completion(self) -> None:
        future = self._training_future
        if future is None or not future.done():
            return
        self._training_future = None
        try:
            result = future.result()
        except Exception as exc:
            self._learning_status["last_error"] = str(exc)
            log.warning("horizon_background_training_failed", error=str(exc))
            return
        self._learning_status["updated"] += result["updated"]
        self._learning_status["rejected"] += result["rejected"]
        if result["updated"]:
            self._learning_status["last_update"] = time.time()

    def _schedule_training(self, symbol: str, specs: dict[str, Any]) -> None:
        self._collect_training_completion()
        if self._training_future is not None:
            return
        self._training_future = self._training_executor.submit(
            self._learning_store.train_pending,
            symbol=symbol,
            horizons=list(specs.values()),
            model_root=self._checkpoint_dir,
            limit=100,
            source_timeframe=self._source_timeframe,
        )

    @property
    def last_horizon_symbol(self) -> str | None:
        return self._last_horizon_symbol

    @property
    def last_horizon_ts(self) -> float | None:
        return self._last_horizon_ts

    @property
    def worker_pool(self) -> Any:
        """The horizon worker orchestrator (read-only view for the runtime platform)."""
        return self._orchestrator

    @property
    def checkpoint_dir(self) -> Path:
        """Root containing horizon-scoped active, candidate, and shadow models."""
        return self._checkpoint_dir

    @property
    def upgrade_registry(self) -> Any:
        """The MLflow model registry facade (read-only view for the runtime platform)."""
        return self._registry

    def start(self) -> None:
        """Start the worker orchestrator (spawns model processes + ECC thread)."""
        if not self._started:
            self._orchestrator.start()
            self._started = True
            log.info("crypto_intelligence_started")

    async def on_bar(
        self,
        symbol: str,
        ohlcv: dict,
        bids: list | None = None,
        asks: list | None = None,
        regime_id: int | None = None,
        regime_confidences: list[float] | None = None,
        derivatives_data: dict | None = None,
        alt_prices: dict[str, list[float]] | None = None,
        event_ts: float | None = None,
        event_id: str | None = None,
        risk_context: dict[str, float] | None = None,
        source_timeframe: str | None = None,
    ) -> IntelSignal | None:
        """
        Process a new OHLCV bar and return a trading signal.

        Returns None if RiskGate suppresses all horizons.
        """
        if not self._started:
            self.start()

        if self._last_horizon_symbol is not None and symbol != self._last_horizon_symbol:
            self._last_horizon_results.clear()
            self._source_cadence_seconds = None
        self._last_decision = None

        self._regime_id = regime_id

        # --- Feature extraction ---
        price = float(ohlcv.get("close", 0.0))
        volume = float(ohlcv.get("volume", 0.0))

        vpin = self._vpin.update(price, volume)
        kyle_lambda = self._kyle.update(price, volume)
        from src.features.microstructure import compute_ofi

        ofi = compute_ofi(bids or [], asks or [])

        mempool_ft = await self._fetch_mempool()
        onchain_ft = await self._onchain.compute(price, 0.0)
        deriv_ft = self._derivatives.extract(derivatives_data or {})

        # Check ECC output from dedicated thread
        ecc_result = self._collect_ecc()

        # Causal layer update
        if alt_prices and len(next(iter(alt_prices.values()), [])) >= 10:
            import numpy as np

            btc_ret = np.diff(np.log(np.array(ohlcv.get("close_history", [price] * 11))))
            self._granger.update(btc_ret, alt_prices)

        # --- Submit inference tasks ---
        import uuid

        from src.workers.orchestrator import WorkerTask

        specs = load_horizon_specs()
        features = {
            "price": price,
            "volume": volume,
            "ofi": ofi,
            "vpin": vpin,
            "kyle_lambda": kyle_lambda,
            "sopr": onchain_ft.sopr,
            "nvt": onchain_ft.nvt,
            "mvrv": onchain_ft.mvrv,
            "oi_usd": deriv_ft.open_interest_usd,
            "funding_rate": deriv_ft.funding_rate,
            "liquidation_pressure": deriv_ft.liquidation_pressure,
            "mempool_fee_p50": mempool_ft.fee_rate_p50_sat,
            "fee_pressure": mempool_ft.fee_pressure,
        }
        # The online learner requires one stable train/serve schema. Price
        # and volume are present on every persisted OHLCV event; optional
        # provider fields remain in the durable raw observation below rather
        # than appearing and disappearing from the model vector.
        model_features = {"price": price, "volume": volume}

        # Use provider event time only. When a caller cannot supply source
        # time, retain inference behavior but do not invent a label clock.
        source_ts = event_ts if event_ts is not None else ohlcv.get("timestamp")
        source_event_key: str | None = None
        nominal_cadence = {"1m": 60.0, "15m": 900.0, "4h": 14400.0}.get(
            str(source_timeframe or "").lower()
        )
        self._source_timeframe = source_timeframe
        observed_cadence = await asyncio.to_thread(
            self._learning_store.source_cadence_seconds, symbol, source_timeframe
        )
        cadence_candidates = [
            value for value in (nominal_cadence, observed_cadence) if value is not None
        ]
        source_cadence = max(cadence_candidates) if cadence_candidates else None
        self._source_cadence_seconds = source_cadence
        if source_ts is not None:
            source_ts = float(source_ts)
            if source_ts > 100_000_000_000:  # common millisecond epoch input
                source_ts /= 1000.0
            if math.isfinite(source_ts) and source_ts > 0:
                source_event_key = event_id or f"{symbol}:{source_ts:.6f}"
                try:
                    learning = await asyncio.to_thread(
                        self._learning_store.observe_event,
                        event_id=source_event_key,
                        symbol=symbol,
                        event_ts=source_ts,
                        close=price,
                        features=model_features,
                        horizons=list(specs.values()),
                        model_root=self._checkpoint_dir,
                        provider=str(ohlcv.get("provider", "persisted_market_bars")),
                        quality="persisted_bar",
                        event_payload={
                            **ohlcv,
                            "source_timeframe": source_timeframe,
                            "derived_features": features,
                            "ecc_observations": ecc_result,
                        },
                        regime_id=regime_id,
                        defer_training=True,
                        source_cadence_seconds=source_cadence,
                        source_timeframe=source_timeframe,
                    )
                    self._learning_status = {
                        "last_update": time.time()
                        if learning["updated"]
                        else self._learning_status["last_update"],
                        "last_error": None,
                        **{
                            key: self._learning_status.get(key, 0) + learning[key]
                            for key in ("matured", "updated", "rejected")
                        },
                    }
                    observed_cadence = await asyncio.to_thread(
                        self._learning_store.source_cadence_seconds, symbol, source_timeframe
                    )
                    if observed_cadence is not None:
                        self._source_cadence_seconds = max(
                            value
                            for value in (nominal_cadence, observed_cadence)
                            if value is not None
                        )
                except Exception as exc:
                    self._learning_status["last_error"] = str(exc)
                    log.warning("horizon_learning_event_rejected", symbol=symbol, error=str(exc))
            else:
                source_ts = None
                self._learning_status["last_error"] = (
                    "invalid source event timestamp; label not scheduled"
                )
        else:
            self._learning_status["last_error"] = (
                "source event timestamp unavailable; label not scheduled"
            )

        batch_id = str(uuid.uuid4())[:8]
        expected_task_ids: set[str] = set()
        results_by_task: dict[str, WorkerResult] = {}
        for _, spec in sorted(specs.items(), key=lambda item: int(item[1]["id"])):
            h_idx = int(spec["id"])
            task_id = f"{batch_id}_{h_idx}"
            expected_task_ids.add(task_id)
            task = WorkerTask(
                task_id=task_id,
                horizon_id=h_idx,
                symbol=symbol,
                features=model_features,
                ecc_features=ecc_result,
                horizon_seconds=int(spec["seconds"]),
                source_event_ts=source_ts,
                source_cadence_seconds=self._source_cadence_seconds,
                architecture_names=tuple(
                    name for name in spec.get("models", []) if name.strip().lower() != "maml"
                ),
                model_dir=str(
                    self._checkpoint_dir
                    / f"h{h_idx + 1}"
                    / hashlib.sha256(symbol.encode()).hexdigest()[:16]
                ),
                regime_id=regime_id,
            )
            try:
                self._orchestrator.submit(task)
            except Exception as exc:
                results_by_task[task_id] = WorkerResult(
                    task_id=task_id,
                    horizon_id=h_idx,
                    direction=0,
                    confidence=0.0,
                    magnitude_mu=0.0,
                    magnitude_sigma=0.0,
                    timing=0.0,
                    algo="",
                    error=f"horizon task submission failed: {exc}",
                    status="FAILED",
                    prediction_ts=time.time(),
                    horizon_seconds=int(spec["seconds"]),
                )

        # --- Collect results ---
        # monotonic, not wall clock: an NTP correction during this window
        # would either abandon the collection early or hold it open long past
        inference_timeout = max(0.05, float(self._cfg.get("inference_timeout_seconds", 1.0)))
        deadline = time.monotonic() + inference_timeout
        expected_horizons = len(specs)
        while len(results_by_task) < expected_horizons and time.monotonic() < deadline:
            r = self._drain_one(timeout=min(0.1, max(0.0, deadline - time.monotonic())))
            if r is not None and r.task_id in expected_task_ids:
                results_by_task[r.task_id] = r
            elif r is not None:
                log.warning("stale_or_unmatched_horizon_result", task_id=r.task_id)

        completed_horizons = {result.horizon_id for result in results_by_task.values()}
        for _, spec in specs.items():
            horizon_id = int(spec["id"])
            if horizon_id in completed_horizons:
                continue
            task_id = f"{batch_id}_{horizon_id}"
            results_by_task[task_id] = WorkerResult(
                task_id=task_id,
                horizon_id=horizon_id,
                direction=0,
                confidence=0.0,
                magnitude_mu=0.0,
                magnitude_sigma=0.0,
                timing=0.0,
                algo="",
                error="horizon worker did not respond before the inference deadline",
                status="FAILED",
                prediction_ts=time.time(),
                horizon_seconds=int(spec["seconds"]),
            )

        results = list(results_by_task.values())

        # Score only prequential outcomes collected under this event's actual
        # regime. A missing regime or a regime without enough OOS observations
        # remains unavailable and cannot be replaced with the aggregate score.
        if regime_id is not None:
            stats = await asyncio.to_thread(self._learning_store.stats, symbol, source_timeframe)
            for result in results:
                regime_score = (
                    stats.get(result.horizon_id, {}).get("oos_by_regime", {}).get(int(regime_id))
                )
                result.oos_score = (
                    None if regime_score is None else regime_score["directional_accuracy"]
                )
                result.oos_sample_count = 0 if regime_score is None else regime_score["samples"]
        else:
            for result in results:
                result.oos_score = None
                result.oos_sample_count = 0

        if source_event_key is not None:
            try:
                await asyncio.to_thread(
                    self._learning_store.record_predictions,
                    event_id=source_event_key,
                    symbol=symbol,
                    predictions=results,
                )
            except Exception as exc:
                self._learning_status["last_error"] = str(exc)
                log.warning("horizon_prediction_persistence_failed", symbol=symbol, error=str(exc))
            for spec in specs.values():
                try:
                    await asyncio.to_thread(
                        self._learning_store.evaluate_candidate,
                        symbol=symbol,
                        horizon_id=int(spec["id"]),
                        source_timeframe=str(source_timeframe or ""),
                        minimum_samples=int(self._cfg.get("candidate_minimum_oos_samples", 100)),
                    )
                except Exception as exc:
                    self._learning_status["last_error"] = str(exc)
                    log.warning(
                        "horizon_candidate_evaluation_failed",
                        symbol=symbol,
                        horizon_id=spec["id"],
                        error=str(exc),
                    )
            self._schedule_training(symbol, specs)

        # Keep each named horizon's latest worker status, including explicit
        # untrained/failed states, so the status API never hides a silent head.
        self._last_horizon_ts = time.time()
        self._last_horizon_symbol = symbol
        for r in results:
            self._last_horizon_results[r.horizon_id] = (r, self._last_horizon_ts)

        from src.fusion.horizon_decision import fuse_horizon_predictions

        decision = fuse_horizon_predictions(
            results,
            specs,
            regime_id=regime_id,
            max_age_seconds=float(self._cfg.get("max_prediction_age_seconds", 20.0)),
        )
        self._last_decision = decision
        if decision.action == "ABSTAIN":
            log.info("horizon_decision_abstained", reason=decision.abstain_reason, symbol=symbol)
            return None

        # A single representative horizon supplies the existing sizing API;
        # its selection follows the fused contribution, never arrival order.
        best_id = max(decision.contributions, key=decision.contributions.get)
        best = next(result for result in results if result.horizon_id == best_id)
        ecc_validity = ecc_result.get("_validity", {})
        ecc_anomaly = (
            float(ecc_result["cluster_flow_score"]) if ecc_validity.get("cluster") is True else None
        )
        score = decision.expected_return / max(decision.uncertainty or 0.0, 1e-12)

        # --- Risk gate ---
        # Risk statistics must come from the portfolio/risk caller. Missing
        # values are not measurements and cannot be replaced by safe-looking
        # constants at this boundary.
        required_risk = {"atr", "cvar", "drawdown", "daily_loss", "odds", "as_of_ts"}
        if risk_context is None or not required_risk.issubset(risk_context):
            log.info(
                "horizon_decision_suppressed_missing_risk_context",
                missing=sorted(required_risk - set(risk_context or {})),
                symbol=symbol,
            )
            self._last_decision = replace(
                decision,
                action="ABSTAIN",
                direction=0,
                abstain_reason="validated portfolio risk context unavailable",
            )
            return None
        risk_values, risk_error = _validated_risk_context(
            risk_context,
            now=time.time(),
            max_age_seconds=float(self._cfg.get("risk_context_max_age_seconds", 30.0)),
        )
        if risk_values is None:
            log.warning("horizon_decision_suppressed_invalid_risk_context", symbol=symbol)
            self._last_decision = replace(
                decision,
                action="ABSTAIN",
                direction=0,
                abstain_reason=risk_error,
            )
            return None
        size_result = self._risk_gate.size(
            signal={
                "confidence": best.confidence,
                "sharpe_est": score,
                "edge": decision.expected_return,
                "odds": risk_values["odds"],
            },
            vol=risk_values["atr"],
            cvar=risk_values["cvar"],
            horizon_idx=best.horizon_id,
        )

        # Circuit breaker
        if self._risk_gate.circuit_breaker(
            drawdown=risk_values["drawdown"], daily_loss=risk_values["daily_loss"]
        ):
            self._last_decision = replace(
                decision,
                action="ABSTAIN",
                direction=0,
                abstain_reason="horizon risk circuit breaker suppressed the decision",
            )
            return None

        if size_result.suppressed:
            log.debug("risk_gate_suppressed", reason=size_result.reason)
            self._last_decision = replace(
                decision,
                action="ABSTAIN",
                direction=0,
                abstain_reason=f"horizon risk sizing suppressed: {size_result.reason}",
            )
            return None

        # --- ADWIN drift check per horizon ---
        self._risk_gate.check_drift(best.horizon_id, best.confidence)

        # --- Persist to DuckDB ---
        self._duckdb.write_horizon_metric(
            horizon_id=best.horizon_id,
            label=decision.direction,
            sharpe=score,
            confidence=best.confidence,
            direction=decision.direction,
            drift_detected=False,
        )

        if self._regime_id is None:
            return None

        return IntelSignal(
            symbol=symbol,
            direction=decision.direction,
            size_pct=size_result.size_pct,
            confidence=best.confidence,
            horizon_idx=best.horizon_id,
            algo=best.algo,
            ecc_anomaly=ecc_anomaly,
            conflict=decision.conflict,
            regime_id=int(self._regime_id),
            meta={
                "agreement_ratio": max(decision.contributions.values(), default=0.0),
                "horizon_contributions": decision.contributions,
                "expected_return": decision.expected_return,
                "uncertainty": decision.uncertainty,
                "model_versions": decision.model_versions,
                "freshness_seconds": decision.freshness_seconds,
                "ecc_validity": ecc_validity,
                "ecc_observed_at": ecc_result.get("_observed_at"),
                "kyle_lambda": kyle_lambda,
                "ofi": ofi,
                "vpin": vpin,
            },
        )

    def _drain_one(self, timeout: float) -> WorkerResult | None:
        """
        Take one item off the shared output queue and keep whatever it is.

        Model workers and the ECC thread publish to the same queue, so either
        kind can be at the head. Both callers used to take an item and drop it
        if it was the other kind — the fusion loop then waited out its full
        deadline for an inference result it had already thrown away, and an
        ECC scan landing mid-window was lost for good. ECC payloads are folded
        into `_ecc_state` here; inference results are returned.
        """
        from src.workers.orchestrator import WorkerResult

        try:
            item = self._orchestrator.collect(timeout=timeout)
        except Exception as exc:
            log.debug("orchestrator_collect_failed", exc=str(exc))
            return None

        if isinstance(item, dict):
            if item.get("type") == "ecc":
                self._ecc_state = dict(item.get("result", {}))
            return None
        return item if isinstance(item, WorkerResult) else None

    def _collect_ecc(self) -> dict[str, Any]:
        """
        Non-blocking drain for the latest ECC features.

        This runs before the bar's own tasks are submitted, so any inference
        result still on the queue belongs to a previous bar that timed out.
        It is dropped rather than fused — a prediction made against a stale
        price is worse than one fewer horizon — but logged, because a queue
        that is consistently behind means the worker pool is undersized.
        """
        stale = self._drain_one(timeout=0.0)
        if stale is not None:
            log.warning("stale_worker_result_dropped", horizon_id=stale.horizon_id)
        result = dict(self._ecc_state)
        observed_at = result.get("_observed_at")
        max_age = float(getattr(self, "_ecc_max_age", 180.0))
        if "_validity" in result and (
            observed_at is None or time.time() - float(observed_at) > max_age
        ):
            validity = dict(result["_validity"])
            result["_validity"] = {name: False for name in validity}
        return result

    def close(self) -> None:
        """Shut down all subsystems cleanly."""
        self._training_executor.shutdown(wait=True, cancel_futures=False)
        self._collect_training_completion()
        self._orchestrator.shutdown()
        # DuckDB holds a file lock, so leaving this open blocks the next
        # process (or the next test) from opening the same database.
        try:
            self._duckdb.close()
        except Exception as exc:
            log.warning("duckdb_close_failed", exc=str(exc))
        self._started = False
        log.info("crypto_intelligence_closed")
