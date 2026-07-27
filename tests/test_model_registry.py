"""Tests for the v4 model registry with shadow-mode evaluation."""

from __future__ import annotations

import pytest

from src.models.model_registry import ModelRegistry, get_model_registry


def test_set_and_get_live_model() -> None:
    registry = ModelRegistry()
    registry.set_live_model("xgb_v1")
    assert registry.live_model_id == "xgb_v1"


def test_register_shadow() -> None:
    registry = ModelRegistry()
    registry.register_shadow("xgb_v2")
    assert "xgb_v2" in registry.shadow_ids()


def test_duplicate_shadow_registration_rejected() -> None:
    registry = ModelRegistry()
    registry.register_shadow("xgb_v2")
    with pytest.raises(ValueError, match="already registered"):
        registry.register_shadow("xgb_v2")


def test_record_prediction_on_unregistered_shadow_raises() -> None:
    registry = ModelRegistry()
    with pytest.raises(KeyError):
        registry.record_shadow_prediction("unknown", 0.6, 1)


def test_evaluate_shadow_insufficient_data() -> None:
    registry = ModelRegistry(min_evaluations=50)
    registry.register_shadow("xgb_v2")
    ready, reason = registry.evaluate_shadow("xgb_v2")
    assert not ready
    assert "insufficient" in reason


def test_evaluate_shadow_ready_when_better_than_live() -> None:
    registry = ModelRegistry(min_evaluations=10)
    registry.register_shadow("xgb_v2")
    for _ in range(10):
        registry.record_shadow_prediction("xgb_v2", 0.9, 1)  # always correct
        registry.record_live_prediction_for_comparison("xgb_v2", 0.4, 1)  # always wrong
    ready, reason = registry.evaluate_shadow("xgb_v2")
    assert ready
    assert "beats" in reason


def test_evaluate_shadow_not_ready_when_worse_than_live() -> None:
    registry = ModelRegistry(min_evaluations=10)
    registry.register_shadow("xgb_v2")
    for _ in range(10):
        registry.record_shadow_prediction("xgb_v2", 0.4, 1)  # always wrong
        registry.record_live_prediction_for_comparison("xgb_v2", 0.9, 1)  # always correct
    ready, reason = registry.evaluate_shadow("xgb_v2")
    assert not ready
    assert "does not beat" in reason


def test_promote_shadow_swaps_live_model() -> None:
    registry = ModelRegistry()
    registry.set_live_model("xgb_v1")
    registry.register_shadow("xgb_v2")
    registry.promote_shadow("xgb_v2")
    assert registry.live_model_id == "xgb_v2"
    assert "xgb_v2" not in registry.shadow_ids()


def test_promote_unregistered_shadow_raises() -> None:
    registry = ModelRegistry()
    with pytest.raises(KeyError):
        registry.promote_shadow("unknown")


def test_rejects_invalid_min_evaluations() -> None:
    with pytest.raises(ValueError, match="min_evaluations"):
        ModelRegistry(min_evaluations=0)


def test_get_model_registry_singleton() -> None:
    r1 = get_model_registry()
    r2 = get_model_registry()
    assert r1 is r2
