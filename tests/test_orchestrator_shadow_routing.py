"""
Tests for Orchestrator._route_retrained_bundle.

Retraining used to hot-swap unconditionally. Clearing the live gate is an
absolute test (OOS Sharpe / max drawdown / trade count), not a comparison
against the model already trading, so a worse model replaced a better one
on every scheduled cycle. These tests pin the three routes a fresh bundle
can take and, crucially, when its metrics row may become visible to
risk.gates.check_live_gate.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
import structlog

from src.config import Timeframe, get_settings
from src.data.storage import ModelMetricsRecord
from src.engine.orchestrator import Orchestrator


@pytest.fixture
def shadow_enabled():
    xgb = get_settings().xgboost
    original = xgb.shadow_mode_enabled
    xgb.shadow_mode_enabled = True
    yield xgb
    xgb.shadow_mode_enabled = original


def _metrics(live_gate_pass: bool = True) -> tuple[ModelMetricsRecord, ...]:
    return tuple(
        ModelMetricsRecord(
            model_name=name,
            timeframe="15m",
            version="v2",
            oos_sharpe=2.0,
            max_drawdown=5.0,
            n_trades=600,
            accuracy=0.6,
            precision_score=0.6,
            recall_score=0.6,
            f1_score=0.6,
            live_gate_pass=live_gate_pass,
        )
        for name in ("direction", "meta_label")
    )


def _orchestrator() -> Orchestrator:
    """Orchestrator without __init__ — only the routing path is under test."""
    orch = object.__new__(Orchestrator)
    orch._log = structlog.get_logger().bind(component="orchestrator_test")
    orch._cfg = get_settings()
    orch._storage = AsyncMock()
    engine = AsyncMock()
    orch._engines = {Timeframe.INTRADAY.value: engine}
    return orch


async def _route(orch: Orchestrator, *, live_gate_pass: bool, metrics=None) -> None:
    await orch._route_retrained_bundle(
        tf=Timeframe.INTRADAY,
        version="v2",
        direction_model=MagicMock(name="direction"),
        meta_model=MagicMock(name="meta"),
        detector=MagicMock(name="detector"),
        ensemble=MagicMock(name="ensemble"),
        metrics_records=_metrics() if metrics is None else metrics,
        live_gate_pass=live_gate_pass,
    )


@pytest.mark.asyncio
async def test_a_candidate_failing_the_live_gate_is_discarded(shadow_enabled):
    orch = _orchestrator()
    await _route(orch, live_gate_pass=False, metrics=_metrics(live_gate_pass=False))

    engine = orch._engines[Timeframe.INTRADAY.value]
    engine.swap_models.assert_not_called()
    engine.set_shadow_bundle.assert_not_called()
    # The live gate reads the *latest* metrics row. Writing a failing
    # candidate's row would halt live trading on the strength of a model
    # that never went live, while the passing incumbent keeps trading.
    orch._storage.insert_model_metrics.assert_not_called()


@pytest.mark.asyncio
async def test_a_passing_candidate_enters_shadow_without_touching_the_live_slot(shadow_enabled):
    orch = _orchestrator()
    await _route(orch, live_gate_pass=True)

    engine = orch._engines[Timeframe.INTRADAY.value]
    engine.swap_models.assert_not_called()
    engine.set_shadow_bundle.assert_awaited_once()
    bundle = engine.set_shadow_bundle.await_args.args[0]
    assert bundle.model_id == "v2"
    assert len(bundle.metrics) == 2
    # Deferred to promotion — see ShadowBundle.metrics.
    orch._storage.insert_model_metrics.assert_not_called()


@pytest.mark.asyncio
async def test_disabling_shadow_mode_restores_swap_on_retrain(shadow_enabled):
    shadow_enabled.shadow_mode_enabled = False
    orch = _orchestrator()
    await _route(orch, live_gate_pass=True)

    engine = orch._engines[Timeframe.INTRADAY.value]
    engine.set_shadow_bundle.assert_not_called()
    engine.swap_models.assert_awaited_once()
    assert engine.swap_models.await_args.kwargs["model_id"] == "v2"
    # The bundle is live now, so its metrics are the ones the live gate
    # should be reading.
    assert orch._storage.insert_model_metrics.await_count == 2
