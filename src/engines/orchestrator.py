"""
Engine Orchestrator — parallel execution of all 18 Crypto-Box engines.

All engines run concurrently via asyncio.gather with per-engine SLA timeouts.
Engines that time-out or error are removed from consensus (graceful degradation).
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog

from src.engines.consensus import ConsensusLayer, ConsensusResult
from src.engines.e01_statistical import E01Statistical
from src.engines.e02_microstructure import E02Microstructure
from src.engines.e03_information_theory import E03InformationTheory
from src.engines.e04_fourier import E04Fourier
from src.engines.e05_onchain import E05OnChain
from src.engines.e06_fractal import E06Fractal
from src.engines.e07_linear_algebra import E07LinearAlgebra
from src.engines.e08_topology import E08Topology
from src.engines.e09_ml_meta import E09MlMeta
from src.engines.e10_supply import E10Supply
from src.engines.e11_stochastic import E11Stochastic
from src.engines.e12_options import E12Options
from src.engines.e13_contagion import E13Contagion
from src.engines.e14_sentiment import E14Sentiment
from src.engines.e15_rl import E15RL
from src.engines.e16_adversarial import E16Adversarial
from src.engines.e17_liquidity import E17Liquidity
from src.engines.e18_network import E18Network
from src.engines.risk_quantifier import RiskQuantifier
from src.engines.schema import EngineOutput
from src.engines.signal_gate import TradeSignal, consensus_to_signal

log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

# Per-engine SLA (Gap G-08 fix)
_ENGINE_SLAS: dict[str, float] = {
    "E-01": 2.0,
    "E-02": 3.0,
    "E-03": 3.0,
    "E-04": 5.0,
    "E-05": 5.0,
    "E-06": 3.0,
    "E-07": 2.0,
    "E-08": 5.0,
    "E-09": 10.0,
    "E-10": 2.0,
    "E-11": 8.0,
    "E-12": 5.0,
    "E-13": 5.0,
    "E-14": 5.0,
    "E-15": 5.0,
    "E-16": 5.0,
    "E-17": 5.0,
    "E-18": 5.0,
}


@dataclass
class OrchestratorResult:
    symbol: str
    timestamp_utc: datetime
    engine_outputs: list[EngineOutput]
    consensus: ConsensusResult
    trade_signal: TradeSignal
    risk: dict[str, Any]
    failed_engines: list[str] = field(default_factory=list)


class EngineOrchestrator:
    def __init__(self, data_root: Path | None = None) -> None:
        self._data_root = data_root or Path("data")
        self._engines: list[Any] = [
            E01Statistical(),
            E02Microstructure(),
            E03InformationTheory(),
            E04Fourier(),
            E05OnChain(),
            E06Fractal(),
            E07LinearAlgebra(),
            E08Topology(),
            E09MlMeta(),
            E10Supply(),
            E11Stochastic(),
            E12Options(),
            E13Contagion(),
            E14Sentiment(),
            E15RL(),
            E16Adversarial(),
            E17Liquidity(),
            E18Network(),
        ]
        self._consensus = ConsensusLayer()
        self._risk = RiskQuantifier()

    async def run(self, symbol: str, data: dict[str, Any]) -> OrchestratorResult:
        """Run all engines in parallel, aggregate consensus, return trade signal."""
        # Wall clock for the cycle's reported timestamp (a point in history);
        # monotonic clock for the elapsed-time measurement below.
        t_start = datetime.now(UTC)
        t_monotonic_start = time.monotonic()

        # Pass prior engine outputs into data for meta-engines (E-08, E-09, E-15)
        data = dict(data)  # shallow copy to avoid mutating caller's dict

        tasks = [
            asyncio.wait_for(engine.run(symbol, data), timeout=_ENGINE_SLAS.get(eid, 5.0))
            for engine, eid in zip(self._engines, [f"E-{i:02d}" for i in range(1, 19)], strict=True)
        ]
        raw_results = await asyncio.gather(*tasks, return_exceptions=True)

        outputs: list[EngineOutput] = []
        failed: list[str] = []
        engine_map: dict[str, EngineOutput] = {}

        for i, result in enumerate(raw_results):
            eid = f"E-{i + 1:02d}"
            if isinstance(result, EngineOutput):
                outputs.append(result)
                engine_map[eid] = result
            else:
                failed.append(eid)
                log.warning("engine_failed", engine=eid, error=str(result))

        # Feed engine outputs back for meta-engines on next cycle
        data["engine_outputs"] = engine_map

        # Regime from data (falls back to Trending)
        regime = str(data.get("regime", "Trending"))

        # Entropy for TTL computation
        e03 = engine_map.get("E-03")
        entropy = float(e03.metadata.get("entropy_score", 0.5)) if e03 else 0.5

        consensus_result = self._consensus.compute(outputs, regime, entropy)

        # Risk quantification from E-11 and E-17
        e11 = engine_map.get("E-11")
        e17 = engine_map.get("E-17")
        jump_prob = float(e11.metadata.get("jump_prob", 0.0)) if e11 else 0.0
        liq_score = float(e17.metadata.get("liquidity_score", 1.0)) if e17 else 1.0
        yz_vol = float(e11.metadata.get("yz_vol", 0.5)) if e11 else 0.5

        risk_result = self._risk.quantify(
            ci_low=consensus_result.ci_low,
            ci_high=consensus_result.ci_high,
            consensus=consensus_result.consensus_price,
            jump_prob=jump_prob,
            liquidity_score=liq_score,
            yz_vol=yz_vol,
            horizon_hours=4,
        )

        spot = float(data.get("spot", consensus_result.consensus_price))
        e16 = engine_map.get("E-16")
        e16_flag = bool(e16.metadata.get("manipulation_flag", False)) if e16 else False

        # Average confidence across contributing engines
        avg_conf = float(sum(o.confidence for o in outputs) / len(outputs)) if outputs else 0.0

        trade_signal = consensus_to_signal(
            consensus=consensus_result.consensus_price,
            spot=spot,
            uncertainty_label=risk_result["uncertainty_label"],
            agreement=consensus_result.agreement_score,
            tail_risk=risk_result["tail_risk_score"],
            e16_flag=e16_flag or consensus_result.circuit_breaker_triggered,
            regime=regime,
            ttl_hours=consensus_result.ttl_hours,
            symbol=symbol,
            raw_confidence=avg_conf,
        )

        log.info(
            "orchestrator_cycle_complete",
            symbol=symbol,
            n_engines=len(outputs),
            n_failed=len(failed),
            direction=trade_signal.direction,
            elapsed_ms=int((time.monotonic() - t_monotonic_start) * 1000),
        )

        self._log_engine_outputs(symbol, engine_map)

        return OrchestratorResult(
            symbol=symbol,
            timestamp_utc=t_start,
            engine_outputs=outputs,
            consensus=consensus_result,
            trade_signal=trade_signal,
            risk=risk_result,
            failed_engines=failed,
        )

    def _log_engine_outputs(self, symbol: str, engine_map: dict[str, EngineOutput]) -> None:
        """Persist engine outputs to parquet audit log (Gap G-13 fix)."""
        try:
            import pandas as pd

            rows = [
                {
                    "timestamp_utc": datetime.now(UTC),
                    "symbol": symbol,
                    "engine_id": eid,
                    "predicted_price": o.predicted_price,
                    "confidence": o.confidence,
                    "direction": o.direction,
                }
                for eid, o in engine_map.items()
            ]
            if not rows:
                return
            date_str = datetime.now(UTC).strftime("%Y-%m-%d")
            path = self._data_root / "engine_outputs" / f"{date_str}.parquet"
            path.parent.mkdir(parents=True, exist_ok=True)
            df = pd.DataFrame(rows)
            if path.exists():
                existing = pd.read_parquet(path)
                df = pd.concat([existing, df], ignore_index=True)
            df.to_parquet(path, index=False)
        except Exception as exc:
            log.warning("engine_output_log_error", exc=str(exc))
