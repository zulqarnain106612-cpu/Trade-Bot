"""Runtime change-manager bridge for shadow-evaluated horizon artifacts."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import structlog

from src.models.horizon_mlp import HorizonMLPExpert
from src.runtime.adapters import Discovery
from src.runtime.contracts import (
    CapabilitySet,
    ComponentSpec,
    ComponentType,
    ComponentVersion,
    HealthReport,
    HealthState,
    LifecycleAction,
    LifecycleState,
    make_component_id,
)
from src.runtime.supervisor import ActionRefusedError

log = structlog.get_logger(__name__)


class HorizonModelRuntime:
    """Expose only passing shadow candidates to the existing human change flow."""

    def __init__(self, intelligence: Callable[[], Any | None]) -> None:
        self._intelligence = intelligence

    @staticmethod
    def _component_id(horizon_id: int, instrument_hash: str, version: str) -> str:
        return make_component_id(
            ComponentType.MODEL,
            f"horizon/{horizon_id}/{instrument_hash}/candidate/{version}",
        )

    def _context(self, config: dict[str, Any]) -> tuple[Any, Path, Path, str]:
        intelligence = self._intelligence()
        if intelligence is None:
            raise ActionRefusedError("horizon intelligence is unavailable")
        symbol = str(config["symbol"])
        horizon_id = int(config["horizon_id"])
        instrument = hashlib.sha256(symbol.encode()).hexdigest()[:16]
        if instrument != str(config["instrument_hash"]):
            raise ActionRefusedError("horizon candidate instrument identity changed")
        root = Path(intelligence.checkpoint_dir)
        active_path = root / f"h{horizon_id + 1}" / instrument / "mlp.joblib"
        shadow_path = active_path.parent / "shadow" / active_path.name
        return intelligence, active_path, shadow_path, symbol

    def discoveries(self) -> list[Discovery]:
        intelligence = self._intelligence()
        if intelligence is None:
            return []
        rows: list[Discovery] = []
        for horizon in intelligence.horizon_status():
            prediction = horizon.get("last_prediction") or {}
            evaluation = horizon.get("candidate_evaluation") or {}
            symbol = intelligence.last_horizon_symbol
            if not symbol:
                continue
            instrument_hash = hashlib.sha256(symbol.encode()).hexdigest()[:16]
            horizon_id = int(horizon["id"])
            active_path = (
                Path(intelligence.checkpoint_dir)
                / f"h{horizon_id + 1}"
                / instrument_hash
                / "mlp.joblib"
            )
            active_pointer = HorizonMLPExpert.activation_pointer(active_path)
            active_version = None if active_pointer is None else active_pointer.get("sha256")
            source_timeframe = str(horizon.get("source_timeframe") or "")

            def add_component(version: str, *, active: bool, eligible: bool) -> None:
                config = {
                    "horizon_id": horizon_id,
                    "symbol": symbol,
                    "instrument_hash": instrument_hash,
                    "candidate_version": version,
                    "source_timeframe": source_timeframe,
                }
                cid = self._component_id(horizon_id, instrument_hash, version)
                component_version = ComponentVersion.create(
                    version, "src.models.horizon_mlp.HorizonMLPExpert", config
                )
                actions = (
                    frozenset({LifecycleAction.DEACTIVATE})
                    if active
                    else frozenset({LifecycleAction.ACTIVATE})
                    if eligible
                    else frozenset()
                )
                spec = ComponentSpec(
                    component_id=cid,
                    component_type=ComponentType.MODEL,
                    version=component_version,
                    owner="src.intelligence.horizon_models",
                    capabilities=CapabilitySet(actions=actions),
                )
                if active:
                    lifecycle = str(active_pointer.get("lifecycle_status", "UNPROMOTED"))
                    health = HealthReport(
                        HealthState.HEALTHY if lifecycle == "PROMOTED" else HealthState.DEGRADED,
                        f"active pointer lifecycle is {lifecycle.lower()}",
                    )
                else:
                    health = HealthReport(
                        HealthState.HEALTHY if eligible else HealthState.DEGRADED,
                        "paired forward evaluation passed; human approval required"
                        if eligible else str(evaluation.get("reason") or "candidate shadow evidence unavailable"),
                    )
                rows.append(
                    Discovery(
                        spec,
                        LifecycleState.ACTIVE if active else LifecycleState.STANDBY,
                        health,
                    )
                )

            if active_version:
                add_component(str(active_version), active=True, eligible=False)
            candidate_version = prediction.get("candidate_model_version")
            candidate_is_current = bool(
                horizon.get("shadow_artifact_available")
                and candidate_version
                and evaluation.get("eligible")
                and evaluation.get("candidate_model_version") == candidate_version
            )
            if candidate_is_current and candidate_version != active_version:
                add_component(str(candidate_version), active=False, eligible=True)
        return rows

    def _evaluation_passes(self, config: dict[str, Any]) -> bool:
        intelligence, _, shadow_path, symbol = self._context(config)
        candidate = HorizonMLPExpert.load(
            shadow_path,
            expected_horizon_id=int(config["horizon_id"]),
            expected_instrument_hash=str(config["instrument_hash"]),
        )
        if candidate.artifact_sha256 != config["candidate_version"]:
            return False
        evaluation = intelligence._learning_store.evaluate_candidate(
            symbol=symbol,
            horizon_id=int(config["horizon_id"]),
            source_timeframe=str(config["source_timeframe"]),
            candidate_version=str(config["candidate_version"]),
            minimum_samples=int(intelligence._cfg.get("candidate_minimum_oos_samples", 100)),
        )
        return bool(
            evaluation.get("eligible")
            and evaluation.get("candidate_model_version") == config["candidate_version"]
        )

    def record_shadow_stage(self, component: Any) -> str:
        config = dict(component.version.configuration)
        if not self._evaluation_passes(config):
            raise ActionRefusedError("candidate paired evaluation is no longer current and passing")
        intelligence, _, _, _ = self._context(config)
        return json.dumps(
            intelligence._learning_store.latest_candidate_evaluation(
                symbol=str(config["symbol"]),
                horizon_id=int(config["horizon_id"]),
                source_timeframe=str(config["source_timeframe"]),
            ),
            sort_keys=True,
        )

    def perform(
        self,
        record: Any,
        action: LifecycleAction,
        target_version: ComponentVersion | None,
    ) -> None:
        config = dict(record.version.configuration)
        intelligence, _, shadow_path, symbol = self._context(config)
        if action is LifecycleAction.ACTIVATE:
            if target_version is not None:
                raise ActionRefusedError("horizon candidate activation does not accept a replacement version")
            if not self._evaluation_passes(config):
                raise ActionRefusedError("candidate paired evaluation failed or became stale")
            try:
                intelligence._learning_store.activate_candidate(
                    component_id=record.component_id,
                    symbol=symbol,
                    horizon_id=int(config["horizon_id"]),
                    candidate_version=str(config["candidate_version"]),
                    model_root=Path(intelligence.checkpoint_dir),
                    source_timeframe=str(config["source_timeframe"]),
                )
            except Exception:
                try:
                    intelligence._learning_store.rollback_promotion(
                        component_id=record.component_id,
                        model_root=Path(intelligence.checkpoint_dir),
                    )
                except Exception as rollback_exc:
                    log.error(
                        "horizon_activation_failure_recovery_failed",
                        component_id=record.component_id,
                        error=str(rollback_exc),
                    )
                raise
            return
        if action is LifecycleAction.DEACTIVATE:
            intelligence._learning_store.rollback_promotion(
                component_id=record.component_id,
                model_root=Path(intelligence.checkpoint_dir),
            )
            return
        raise ActionRefusedError(f"horizon candidate action {action.value} is unsupported")

    def probe(self, record: Any) -> HealthReport:
        config = dict(record.version.configuration)
        try:
            intelligence, active_path, _, _ = self._context(config)
            pointer = HorizonMLPExpert.activation_pointer(active_path)
            if pointer is not None and pointer.get("sha256") == config["candidate_version"]:
                state = str(pointer.get("lifecycle_status", "UNPROMOTED"))
                if state in {"PROBATION", "PROMOTED"}:
                    return HealthReport(HealthState.HEALTHY, f"horizon model {state.lower()}")
            return HealthReport(HealthState.UNKNOWN, "candidate is not the active horizon artifact")
        except Exception as exc:
            return HealthReport(HealthState.UNHEALTHY, str(exc))

    def complete_promotion(self, component: Any) -> None:
        intelligence = self._intelligence()
        if intelligence is None:
            raise ActionRefusedError("horizon intelligence is unavailable")
        intelligence._learning_store.complete_promotion(
            component_id=component.component_id,
            model_root=Path(intelligence.checkpoint_dir),
        )
