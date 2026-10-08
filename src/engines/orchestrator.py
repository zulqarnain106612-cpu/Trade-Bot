"""
Engine Orchestrator — parallel execution of all 18 Crypto-Box engines.

All engines run concurrently via asyncio.gather with per-engine SLA timeouts.
Engines that time-out or error are removed from consensus (graceful degradation).

Registry: SIG-004 (config/quality_registry.json).
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
from src.eventbus import get_event_bus

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


# Positional engine identities (SIG-004): the i-th engine in the list is
# E-{i:02d}. The three stages after the fan-out are named, not numbered.
ENGINE_IDS: tuple[str, ...] = tuple(f"E-{i:02d}" for i in range(1, 19))
STAGE_IDS: tuple[str, ...] = ("consensus", "risk_quantifier", "signal_gate")

# The provider-cache fields (ProviderCache.snapshot) each engine reads, for
# the runtime platform's dependency graph. Engines not listed read only what
# the caller computes itself (bars, spot). Every engine degrades without its
# field, so these are optional dependencies. Checked against the engine
# sources by tests/runtime/test_production_runtime.py.
ENGINE_CACHE_INPUTS: dict[str, tuple[str, ...]] = {
    "E-02": ("orderbook",),
    "E-04": ("block_height",),
    "E-05": ("onchain",),
    "E-10": ("block_height",),
    "E-12": ("options",),
    "E-13": ("macro",),
    "E-14": ("sentiment",),
    "E-18": ("exchange_flows",),
}

# What each post-fan-out stage in run() consumes: (engine or stage id,
# required). Consensus takes whichever engines answered, so none is required
# by it; the risk quantifier and the gate cannot run without consensus.
STAGE_INPUTS: dict[str, tuple[tuple[str, bool], ...]] = {
    "consensus": tuple((eid, False) for eid in ENGINE_IDS),
    "risk_quantifier": (("consensus", True), ("E-11", False), ("E-17", False)),
    "signal_gate": (("consensus", True), ("risk_quantifier", True), ("E-16", False)),
}

_ERROR_TEXT_LIMIT = 200


def _describe_failure(result: object) -> str:
    if isinstance(result, TimeoutError):
        return "timeout"
    if isinstance(result, BaseException):
        return f"{type(result).__name__}: {result}"[:_ERROR_TEXT_LIMIT]
    return f"returned {type(result).__name__}, not EngineOutput"


def _engine_row(
    engine_id: str, result: object, latency_ms: float | None, error: str | None
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "engine_id": engine_id,
        "ok": error is None,
        "latency_ms": None if latency_ms is None else round(latency_ms, 3),
        "error": error,
    }
    if isinstance(result, EngineOutput):
        row["direction"] = int(result.direction)
        row["confidence"] = float(result.confidence)
    return row


def _publish_cycle(
    symbol: str,
    engine_rows: list[dict[str, Any]],
    consensus: ConsensusResult,
    risk: dict[str, Any],
    signal: TradeSignal,
    elapsed_ms: int,
) -> None:
    """
    One ``engine`` event per cycle: every engine's outcome, then consensus,
    the risk quantifier and the signal gate. Published inside the caller's
    tick, so it carries the tick's trace_id and joins its decision trace.

    The bus never blocks or raises; building the payload could (a metadata
    value that is not a number), so that is guarded too -- a cycle's evidence
    is worth losing before a cycle's signal is.
    """
    try:
        payload = {
            "symbol": symbol,
            "engines": engine_rows,
            "failed": [r["engine_id"] for r in engine_rows if not r["ok"]],
            "consensus": {
                "price": float(consensus.consensus_price),
                "agreement": float(consensus.agreement_score),
                "outliers": list(consensus.outlier_ids),
                "circuit_breaker": bool(consensus.circuit_breaker_triggered),
            },
            "risk": {
                "uncertainty_label": str(risk.get("uncertainty_label")),
                "tail_risk_score": float(risk.get("tail_risk_score", 0.0)),
            },
            "signal": {
                "direction": int(signal.direction),
                "confidence": float(signal.confidence),
                "kelly_multiplier": float(signal.kelly_multiplier),
                "warnings": list(signal.warnings),
            },
            "elapsed_ms": elapsed_ms,
        }
    except Exception as exc:  # evidence lost, signal intact
        log.warning("engine_cycle_event_unbuildable", symbol=symbol, error=str(exc))
        return
    get_event_bus().publish("engine", payload)


@dataclass
class EngineRunStats:
    """
    The observed record of one engine (or stage) across cycles: what the
    runtime platform reports as its health. Updated only by ``run``, read as
    a copy through ``EngineOrchestrator.engine_health``.
    """

    runs: int = 0
    failures: int = 0
    consecutive_failures: int = 0
    last_ok: bool | None = None
    last_error: str | None = None
    last_latency_ms: float | None = None
    last_run_ms: int | None = None

    def record(self, ok: bool, latency_ms: float | None, error: str | None) -> None:
        self.runs += 1
        self.last_ok = ok
        self.last_error = error
        self.last_latency_ms = latency_ms
        self.last_run_ms = int(time.time() * 1000)
        if ok:
            self.consecutive_failures = 0
        else:
            self.failures += 1
            self.consecutive_failures += 1

    def snapshot(self) -> dict[str, Any]:
        return {
            "runs": self.runs,
            "failures": self.failures,
            "consecutive_failures": self.consecutive_failures,
            "last_ok": self.last_ok,
            "last_error": self.last_error,
            "last_latency_ms": self.last_latency_ms,
            "last_run_ms": self.last_run_ms,
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
        self._stats: dict[str, EngineRunStats] = {
            cid: EngineRunStats() for cid in (*ENGINE_IDS, *STAGE_IDS)
        }

    def engine_roster(self) -> tuple[tuple[str, str], ...]:
        """(engine id, implementation) in run()'s positional E-01..E-18 order."""
        return tuple(
            (eid, f"{type(engine).__module__}.{type(engine).__qualname__}")
            for eid, engine in zip(ENGINE_IDS, self._engines, strict=True)
        )

    def stage_roster(self) -> tuple[tuple[str, str], ...]:
        """(stage id, implementation) for the steps after the fan-out."""
        def impl(obj: Any) -> str:
            return f"{obj.__module__}.{obj.__qualname__}"

        return (
            ("consensus", impl(type(self._consensus))),
            ("risk_quantifier", impl(type(self._risk))),
            ("signal_gate", impl(consensus_to_signal)),
        )

    @staticmethod
    def cache_inputs() -> dict[str, tuple[str, ...]]:
        """ENGINE_CACHE_INPUTS: the provider-cache fields each engine reads."""
        return dict(ENGINE_CACHE_INPUTS)

    @staticmethod
    def stage_inputs() -> dict[str, tuple[tuple[str, bool], ...]]:
        """STAGE_INPUTS: what each post-fan-out stage consumes."""
        return dict(STAGE_INPUTS)

    def engine_health(self) -> dict[str, dict[str, Any]]:
        """Per-engine and per-stage run record, as copies (E-01..E-18, then stages)."""
        return {cid: stats.snapshot() for cid, stats in self._stats.items()}

    @staticmethod
    async def _timed(coro: Any, sla_s: float) -> tuple[object, float]:
        """The engine's result (or its failure, as data) and how long it took."""
        start = time.monotonic()
        try:
            result: object = await asyncio.wait_for(coro, timeout=sla_s)
        except Exception as exc:  # an engine failure degrades consensus; it never escapes
            result = exc
        return result, (time.monotonic() - start) * 1000.0

    def _stage(self, stage_id: str, fn: Any) -> Any:
        """Run one post-fan-out stage, recording its outcome; failures still raise."""
        start = time.monotonic()
        try:
            value = fn()
        except Exception as exc:
            self._stats[stage_id].record(
                False, (time.monotonic() - start) * 1000.0, _describe_failure(exc)
            )
            raise
        self._stats[stage_id].record(True, (time.monotonic() - start) * 1000.0, None)
        return value

    async def run(self, symbol: str, data: dict[str, Any]) -> OrchestratorResult:
        """Run all engines in parallel, aggregate consensus, return trade signal."""
        # Wall clock for the cycle's reported timestamp (a point in history);
        # monotonic clock for the elapsed-time measurement below.
        t_start = datetime.now(UTC)
        t_monotonic_start = time.monotonic()

        # Pass prior engine outputs into data for meta-engines (E-08, E-09, E-15)
        data = dict(data)  # shallow copy to avoid mutating caller's dict

        tasks = [
            self._timed(engine.run(symbol, data), _ENGINE_SLAS.get(eid, 5.0))
            for engine, eid in zip(self._engines, [f"E-{i:02d}" for i in range(1, 19)], strict=True)
        ]
        raw_results = await asyncio.gather(*tasks, return_exceptions=True)

        outputs: list[EngineOutput] = []
        failed: list[str] = []
        engine_map: dict[str, EngineOutput] = {}
        engine_rows: list[dict[str, Any]] = []

        for i, item in enumerate(raw_results):
            eid = f"E-{i + 1:02d}"
            # A cancelled child comes back bare from gather, not as a tuple.
            result, latency_ms = item if isinstance(item, tuple) else (item, None)
            ok = isinstance(result, EngineOutput)
            error = None if ok else _describe_failure(result)
            self._stats[eid].record(ok, latency_ms, error)
            engine_rows.append(_engine_row(eid, result, latency_ms, error))
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

        consensus_result = self._stage(
            "consensus", lambda: self._consensus.compute(outputs, regime, entropy)
        )

        # Risk quantification from E-11 and E-17
        e11 = engine_map.get("E-11")
        e17 = engine_map.get("E-17")
        jump_prob = float(e11.metadata.get("jump_prob", 0.0)) if e11 else 0.0
        liq_score = float(e17.metadata.get("liquidity_score", 1.0)) if e17 else 1.0
        yz_vol = float(e11.metadata.get("yz_vol", 0.5)) if e11 else 0.5

        risk_result = self._stage(
            "risk_quantifier",
            lambda: self._risk.quantify(
                ci_low=consensus_result.ci_low,
                ci_high=consensus_result.ci_high,
                consensus=consensus_result.consensus_price,
                jump_prob=jump_prob,
                liquidity_score=liq_score,
                yz_vol=yz_vol,
                horizon_hours=4,
            ),
        )

        spot = float(data.get("spot", consensus_result.consensus_price))
        e16 = engine_map.get("E-16")
        e16_flag = bool(e16.metadata.get("manipulation_flag", False)) if e16 else False

        # Average confidence across contributing engines
        avg_conf = float(sum(o.confidence for o in outputs) / len(outputs)) if outputs else 0.0

        trade_signal = self._stage(
            "signal_gate",
            lambda: consensus_to_signal(
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
            ),
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
        _publish_cycle(
            symbol,
            engine_rows,
            consensus_result,
            risk_result,
            trade_signal,
            int((time.monotonic() - t_monotonic_start) * 1000),
        )

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
