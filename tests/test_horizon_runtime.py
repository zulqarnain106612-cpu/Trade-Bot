"""Horizon artifact lifecycle tests.

Decides:
  - MODL-001 — model artifacts retain provenance and activation is reversible
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.models.horizon_mlp import HorizonMLPExpert
from src.runtime.contracts import (
    ComponentType,
    ComponentVersion,
    HealthState,
    LifecycleAction,
    LifecycleState,
)
from src.runtime.horizon_models import HorizonModelRuntime
from src.runtime.supervisor import ActionRefusedError

SYMBOL = "BTC/USDT"
INSTRUMENT = hashlib.sha256(SYMBOL.encode()).hexdigest()[:16]
CANDIDATE = "candidate-sha"
ACTIVE = "active-sha"


class LearningStore:
    def __init__(self):
        self.evaluation = {
            "eligible": True,
            "candidate_model_version": CANDIDATE,
            "evaluation_id": "paired",
        }
        self.latest = {"eligible": True, "evaluation_id": "paired"}
        self.calls = []
        self.fail_activate = False
        self.fail_rollback = False

    def evaluate_candidate(self, **kwargs):
        self.calls.append(("evaluate_candidate", kwargs))
        return self.evaluation

    def latest_candidate_evaluation(self, **kwargs):
        self.calls.append(("latest_candidate_evaluation", kwargs))
        return self.latest

    def activate_candidate(self, **kwargs):
        self.calls.append(("activate_candidate", kwargs))
        if self.fail_activate:
            raise RuntimeError("activation failed")

    def rollback_promotion(self, **kwargs):
        self.calls.append(("rollback_promotion", kwargs))
        if self.fail_rollback:
            raise RuntimeError("rollback failed")

    def complete_promotion(self, **kwargs):
        self.calls.append(("complete_promotion", kwargs))


class Intelligence:
    def __init__(self, root: Path):
        self.checkpoint_dir = root
        self.last_horizon_symbol = SYMBOL
        self._cfg = {"candidate_minimum_oos_samples": 7}
        self._learning_store = LearningStore()
        self.rows = [
            {
                "id": 1,
                "source_timeframe": "1m",
                "last_prediction": {"candidate_model_version": CANDIDATE},
                "candidate_evaluation": {"eligible": True, "candidate_model_version": CANDIDATE},
                "shadow_artifact_available": True,
            }
        ]

    def horizon_status(self):
        return self.rows


def make_component(config=None):
    cfg = config or {
        "horizon_id": 1,
        "symbol": SYMBOL,
        "instrument_hash": INSTRUMENT,
        "candidate_version": CANDIDATE,
        "source_timeframe": "1m",
    }
    return SimpleNamespace(
        component_id="model:horizon/1/candidate",
        version=ComponentVersion.create(CANDIDATE, "model", cfg),
    )


@pytest.fixture
def intelligence(tmp_path, monkeypatch):
    intel = Intelligence(tmp_path / "models")

    def load(path, **kwargs):
        return SimpleNamespace(artifact_sha256=CANDIDATE)

    monkeypatch.setattr(HorizonMLPExpert, "load", load)
    monkeypatch.setattr(HorizonMLPExpert, "activation_pointer", lambda path: None)
    return intel


def test_discoveries_expose_only_current_passing_candidate_and_active_pointer(
    tmp_path, monkeypatch
):
    intel = Intelligence(tmp_path / "models")
    assert HorizonModelRuntime(lambda: None).discoveries() == []
    runtime = HorizonModelRuntime(lambda: intel)
    intel.last_horizon_symbol = SYMBOL
    monkeypatch.setattr(HorizonMLPExpert, "activation_pointer", lambda path: None)
    rows = runtime.discoveries()
    assert len(rows) == 1
    assert rows[0].state is LifecycleState.STANDBY
    assert rows[0].spec.component_type is ComponentType.MODEL
    assert LifecycleAction.ACTIVATE in rows[0].spec.capabilities.actions

    monkeypatch.setattr(
        HorizonMLPExpert,
        "activation_pointer",
        lambda path: {"sha256": ACTIVE, "lifecycle_status": "PROMOTED"},
    )
    intel.rows[0]["last_prediction"]["candidate_model_version"] = ACTIVE
    rows = runtime.discoveries()
    assert len(rows) == 1
    assert rows[0].state is LifecycleState.ACTIVE
    assert rows[0].health.state is HealthState.HEALTHY
    assert LifecycleAction.DEACTIVATE in rows[0].spec.capabilities.actions


def test_discovery_rejects_missing_symbol_and_stale_or_ineligible_candidate(tmp_path, monkeypatch):
    intel = Intelligence(tmp_path / "models")
    runtime = HorizonModelRuntime(lambda: intel)
    intel.last_horizon_symbol = ""
    assert runtime.discoveries() == []
    intel.last_horizon_symbol = SYMBOL
    intel.rows[0]["candidate_evaluation"]["candidate_model_version"] = "old"
    assert runtime.discoveries() == []
    intel.rows[0]["candidate_evaluation"]["candidate_model_version"] = CANDIDATE
    intel.rows[0]["candidate_evaluation"]["eligible"] = False
    assert runtime.discoveries() == []
    monkeypatch.setattr(
        HorizonMLPExpert,
        "activation_pointer",
        lambda path: {"sha256": ACTIVE, "lifecycle_status": "PROBATION"},
    )
    rows = runtime.discoveries()
    assert len(rows) == 1
    assert rows[0].health.state is HealthState.DEGRADED


def test_context_and_candidate_evaluation_fail_closed(tmp_path, monkeypatch, intelligence):
    runtime = HorizonModelRuntime(lambda: None)
    with pytest.raises(ActionRefusedError, match="unavailable"):
        runtime._context(make_component().version.configuration)
    runtime = HorizonModelRuntime(lambda: intelligence)
    bad = dict(make_component().version.configuration, instrument_hash="wrong")
    with pytest.raises(ActionRefusedError, match="identity changed"):
        runtime._context(bad)
    assert runtime._evaluation_passes(make_component().version.configuration)
    monkeypatch.setattr(
        HorizonMLPExpert, "load", lambda path, **kwargs: SimpleNamespace(artifact_sha256="other")
    )
    assert not runtime._evaluation_passes(make_component().version.configuration)
    monkeypatch.setattr(
        HorizonMLPExpert, "load", lambda path, **kwargs: SimpleNamespace(artifact_sha256=CANDIDATE)
    )
    intelligence._learning_store.evaluation = {
        "eligible": False,
        "candidate_model_version": CANDIDATE,
    }
    assert not runtime._evaluation_passes(make_component().version.configuration)


def test_record_stage_requires_fresh_pass_and_serializes_evidence(tmp_path, intelligence):
    runtime = HorizonModelRuntime(lambda: intelligence)
    component = make_component()
    assert json.loads(runtime.record_shadow_stage(component))["evaluation_id"] == "paired"
    intelligence._learning_store.evaluation["eligible"] = False
    with pytest.raises(ActionRefusedError, match="no longer current"):
        runtime.record_shadow_stage(component)


def test_activate_and_deactivate_are_gated_and_reversible(tmp_path, intelligence):
    runtime = HorizonModelRuntime(lambda: intelligence)
    component = make_component()
    with pytest.raises(ActionRefusedError, match="replacement version"):
        runtime.perform(
            component, LifecycleAction.ACTIVATE, ComponentVersion.create("other", "model", {})
        )
    with pytest.raises(ActionRefusedError, match="failed or became stale"):
        intelligence._learning_store.evaluation["eligible"] = False
        runtime.perform(component, LifecycleAction.ACTIVATE, None)
    intelligence._learning_store.evaluation["eligible"] = True
    runtime.perform(component, LifecycleAction.ACTIVATE, None)
    assert any(name == "activate_candidate" for name, _ in intelligence._learning_store.calls)
    runtime.perform(component, LifecycleAction.DEACTIVATE, None)
    assert any(name == "rollback_promotion" for name, _ in intelligence._learning_store.calls)
    with pytest.raises(ActionRefusedError, match="unsupported"):
        runtime.perform(component, LifecycleAction.STOP, None)


def test_activation_failure_attempts_rollback_and_probe_reports_state(
    tmp_path, intelligence, monkeypatch
):
    runtime = HorizonModelRuntime(lambda: intelligence)
    component = make_component()
    intelligence._learning_store.fail_activate = True
    with pytest.raises(RuntimeError, match="activation failed"):
        runtime.perform(component, LifecycleAction.ACTIVATE, None)
    assert any(name == "rollback_promotion" for name, _ in intelligence._learning_store.calls)

    monkeypatch.setattr(
        HorizonMLPExpert,
        "activation_pointer",
        lambda path: {"sha256": CANDIDATE, "lifecycle_status": "PROBATION"},
    )
    assert runtime.probe(component).state is HealthState.HEALTHY
    monkeypatch.setattr(
        HorizonMLPExpert,
        "activation_pointer",
        lambda path: {"sha256": "different", "lifecycle_status": "PROMOTED"},
    )
    assert runtime.probe(component).state is HealthState.UNKNOWN
    broken = HorizonModelRuntime(lambda: None)
    assert broken.probe(component).state is HealthState.UNHEALTHY


def test_complete_promotion_requires_live_intelligence_and_records_completion(
    tmp_path, intelligence
):
    runtime = HorizonModelRuntime(lambda: intelligence)
    runtime.complete_promotion(make_component())
    assert any(name == "complete_promotion" for name, _ in intelligence._learning_store.calls)
    with pytest.raises(ActionRefusedError, match="unavailable"):
        HorizonModelRuntime(lambda: None).complete_promotion(make_component())
