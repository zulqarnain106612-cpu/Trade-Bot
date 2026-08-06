"""
CryptoIntelligence — central orchestrator for crypto-intel-v6.

Coordinates:
  - 10-horizon WorkerOrchestrator (model inference workers + ECC thread)
  - Feature bus (microstructure, on-chain, derivatives, NLP, mempool)
  - ECC pipeline (secp256k1, ECDSA, Schnorr/Taproot, UTXO, zkSNARK)
  - Causal layer (DoWhy SCM, AssetGNN, Granger)
  - CrossAttentionFusion + MetaNetwork
  - RiskGate v2 (CVaR-Kelly + ADWIN) + HorizonConflictResolver
  - SmartOrderRouter, RLExecutionAgent, PostTradeAnalytics
  - Self-upgrade loop (MAML, Optuna, ShadowDeploy, ModelRegistry)
  - DuckDBStore + RedpandaFeeds (observability bus)
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import structlog
import yaml


log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

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
    ecc_anomaly: float  # [0, 1]
    conflict: bool  # True if horizons disagreed
    regime_id: int
    ts: float = field(default_factory=time.time)
    meta: dict[str, Any] = field(default_factory=dict)


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

        # Risk + conflict
        from src.risk.conflict_resolver import HorizonConflictResolver
        from src.risk.gate import RiskGate

        self._risk_gate = RiskGate.from_config(self._risk_cfg)
        self._resolver = HorizonConflictResolver(
            conflict_threshold=self._risk_cfg.get("conflict_threshold", 0.6)
        )

        # Worker orchestrator (model workers + ECC thread)
        from src.workers.orchestrator import WorkerOrchestrator

        btc_rpc = self._cfg.get("btc_node_rpc", {})
        self._orchestrator = WorkerOrchestrator(
            n_workers=self._cfg.get("n_workers", 8),
            btc_rpc_url=btc_rpc.get("url", "http://127.0.0.1:8332"),
            btc_rpc_user=btc_rpc.get("user", "crypto"),
            btc_rpc_pass=btc_rpc.get("pass", "crypto"),
            ecc_interval=self._cfg.get("ecc_interval_seconds", 60.0),
        )

        # Execution
        from src.execution.post_trade import PostTradeAnalytics
        from src.execution.rl_agent import RLExecutionAgent
        from src.execution.router import SmartOrderRouter

        self._router = SmartOrderRouter(
            exchanges=self._cfg.get("exchanges", ["binance", "bybit", "okx"])
        )
        self._rl_agent = RLExecutionAgent()
        self._post_trade = PostTradeAnalytics()

        # Observability
        from src.data.duckdb_store import DuckDBStore

        self._duckdb = DuckDBStore(
            path=Path(self._cfg.get("duckdb_path", "./models/crypto_intel.duckdb"))
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

        # State
        self._ecc_state: dict[str, float] = {}
        self._regime_id: int = 0
        self._started = False

    def _load_config(self, path: Path) -> dict:
        try:
            with open(path) as fh:
                return yaml.safe_load(fh) or {}
        except FileNotFoundError:
            log.warning("config_not_found", path=str(path))
            return {}

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
        regime_id: int = 0,
        regime_confidences: list[float] | None = None,
        derivatives_data: dict | None = None,
        alt_prices: dict[str, list[float]] | None = None,
    ) -> IntelSignal | None:
        """
        Process a new OHLCV bar and return a trading signal.

        Returns None if RiskGate suppresses all horizons.
        """
        if not self._started:
            self.start()

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

        n_horizons = self._cfg.get("n_horizons", 10)
        batch_id = str(uuid.uuid4())[:8]
        for h_idx in range(n_horizons):
            task = WorkerTask(
                task_id=f"{batch_id}_{h_idx}",
                horizon_id=h_idx,
                symbol=symbol,
                features=features,
                ecc_features=ecc_result,
            )
            self._orchestrator.submit(task)

        # --- Collect results ---
        results = []
        deadline = time.time() + 5.0
        while len(results) < n_horizons and time.time() < deadline:
            r = self._orchestrator.collect(timeout=1.0)
            if r is not None and not isinstance(r, dict):
                results.append(r)

        if not results:
            log.warning("no_horizon_results", symbol=symbol)
            return None

        # --- Conflict resolution ---
        import numpy as np

        signals = [
            {"direction": r.direction, "confidence": r.confidence, "horizon_idx": r.horizon_idx}
            for r in results
        ]
        rw = np.ones(len(results))
        if regime_confidences:
            rw = np.array(regime_confidences[: len(results)])

        ecc_anomaly = float(ecc_result.get("cluster_flow_score", 0.0))
        resolution = self._resolver.resolve_with_ecc(signals, rw, ecc_anomaly)

        if resolution.direction == 0:
            return None

        # --- Pick representative horizon result ---
        best = max(results, key=lambda r: r.confidence)

        # --- Risk gate ---
        vol = float(ohlcv.get("atr", max(abs(price * 0.01), 1e-9)))
        cvar = 0.02  # conservative default; real CVaR from portfolio manager
        size_result = self._risk_gate.size(
            signal={
                "confidence": best.confidence,
                "sharpe_est": resolution.weight * 2.0,
                "edge": 0.01,
                "odds": 1.0,
            },
            vol=vol,
            cvar=cvar,
            horizon_idx=best.horizon_idx,
        )

        # Circuit breaker
        if self._risk_gate.circuit_breaker(drawdown=0.0, daily_loss=0.0):
            return None

        if size_result.suppressed:
            log.debug("risk_gate_suppressed", reason=size_result.reason)
            return None

        # --- ADWIN drift check per horizon ---
        self._risk_gate.check_drift(best.horizon_idx, best.confidence)

        # --- Persist to DuckDB ---
        self._duckdb.write_horizon_metric(
            horizon_id=best.horizon_idx,
            label=resolution.direction,
            sharpe=resolution.weight * 2.0,
            confidence=best.confidence,
            direction=resolution.direction,
            drift_detected=False,
        )

        return IntelSignal(
            symbol=symbol,
            direction=resolution.direction,
            size_pct=size_result.size_pct,
            confidence=best.confidence,
            horizon_idx=best.horizon_idx,
            algo=best.algo,
            ecc_anomaly=ecc_anomaly,
            conflict=resolution.conflict,
            regime_id=regime_id,
            meta={
                "agreement_ratio": resolution.agreement_ratio,
                "kyle_lambda": kyle_lambda,
                "ofi": ofi,
                "vpin": vpin,
            },
        )

    def _collect_ecc(self) -> dict[str, float]:
        """Non-blocking drain of the ECC results from the orchestrator output queue."""
        latest = dict(self._ecc_state)
        try:
            r = self._orchestrator.collect(timeout=0.0)
            if isinstance(r, dict) and r.get("type") == "ecc":
                latest = dict(r.get("result", {}))
                self._ecc_state = latest
        except Exception:
            pass
        return latest

    async def route_signal(self, signal: IntelSignal, price: float, capital_usd: float) -> None:
        """Execute a signal via SmartOrderRouter and record fill analytics."""
        if signal.direction == 0 or signal.size_pct <= 0:
            return

        size_usd = capital_usd * signal.size_pct
        side = "buy" if signal.direction > 0 else "sell"

        route_signal = {
            "symbol": signal.symbol,
            "side": side,
            "price": price,
            "horizon": signal.horizon_idx,
            "horizon_seconds": [30, 60, 300, 600, 1800, 3600, 14400, 86400, 86400 * 7, 86400 * 30][
                min(signal.horizon_idx, 9)
            ],
            "confidence": signal.confidence,
        }

        result = await self._router.route(
            route_signal, signal.meta.get("kyle_lambda", 0.001), size_usd
        )
        qty = size_usd / max(price, 1e-9)
        self._post_trade.record(result, signal.symbol, side, signal.horizon_idx, price, qty)

    def close(self) -> None:
        """Shut down all subsystems cleanly."""
        self._orchestrator.shutdown()
        self._started = False
        log.info("crypto_intelligence_closed")
