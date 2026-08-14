"""
v4 shadow-mode model promotion — src/models/model_registry.py wired into
the signal engine and the orchestrator's retrain path.

The property under test throughout: a retrained bundle changes nothing
about live trading until it has out-predicted the incumbent on real bars.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pandas as pd
import pytest

from src.config import Timeframe, XGBoostSettings, get_settings
from src.data.storage import ModelMetricsRecord
from src.engine.signal_engine import ShadowBundle, SignalEngine
from src.models.model_registry import ModelRegistry


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def shadow_window():
    """
    Shrinks the promotion window so a test can close it in a handful of
    bars, and restores the process-wide cached Settings afterwards.
    """
    cfg = get_settings()
    xgb = cfg.xgboost
    original = (xgb.shadow_mode_enabled, xgb.shadow_min_evaluations, xgb.shadow_max_evaluations)
    original_log_path = cfg.storage.decision_log_path
    xgb.shadow_mode_enabled = True
    xgb.shadow_min_evaluations = 3
    xgb.shadow_max_evaluations = 6
    yield xgb
    (
        xgb.shadow_mode_enabled,
        xgb.shadow_min_evaluations,
        xgb.shadow_max_evaluations,
    ) = original
    cfg.storage.decision_log_path = original_log_path


def _engine(tmp_path: Path) -> SignalEngine:
    storage = AsyncMock()
    trainer = MagicMock()
    engine = SignalEngine(
        symbol="BTC/USDT",
        timeframe=Timeframe.INTRADAY,
        storage=storage,
        fetcher=AsyncMock(),
        detector=MagicMock(name="live_detector"),
        direction_model=MagicMock(name="live_direction"),
        meta_model=MagicMock(name="live_meta"),
        trainer=trainer,
    )
    engine._registry = ModelRegistry(min_evaluations=3)
    engine._registry.set_live_model("initial")
    engine._cfg.storage.decision_log_path = tmp_path / "decision_log.md"
    return engine


def _bundle(model_id: str = "v2", metrics: tuple[ModelMetricsRecord, ...] = ()) -> ShadowBundle:
    return ShadowBundle(
        model_id=model_id,
        direction_model=MagicMock(name="cand_direction"),
        meta_model=MagicMock(name="cand_meta"),
        detector=MagicMock(name="cand_detector"),
        ensemble=MagicMock(name="cand_ensemble"),
        metrics=metrics,
    )


_VEC = pd.Series([0.0, 0.0], index=["f0", "f1"])


def _bars(ts: int, close: float) -> pd.DataFrame:
    return pd.DataFrame({"close": [close]}, index=[ts])


async def _drive(
    engine: SignalEngine,
    closes: list[float],
    shadow_p: float,
    live_p: float,
    start: int = 0,
) -> None:
    """Feeds one bar per close, with fixed shadow/live direction probabilities."""
    engine._trainer.predict_direction.return_value = (1, shadow_p)
    for i, close in enumerate(closes):
        await engine._evaluate_shadow_tick(_bars(1000 + start + i, close), _VEC, live_p)


# ---------------------------------------------------------------------------
# Registry additions
# ---------------------------------------------------------------------------


def test_discard_shadow_removes_it_without_touching_the_live_slot():
    reg = ModelRegistry(min_evaluations=1)
    reg.set_live_model("v1")
    reg.register_shadow("v2")
    reg.discard_shadow("v2")
    assert reg.shadow_ids() == []
    assert reg.live_model_id == "v1"


def test_registry_accessors_reject_unknown_model_ids():
    reg = ModelRegistry(min_evaluations=1)
    for call in (reg.discard_shadow, reg.evaluation_count, reg.accuracies):
        with pytest.raises(KeyError):
            call("nope")


def test_evaluation_count_and_accuracies_track_recorded_predictions():
    reg = ModelRegistry(min_evaluations=1)
    reg.register_shadow("v2")
    reg.record_shadow_prediction("v2", 0.9, 1)
    reg.record_live_prediction_for_comparison("v2", 0.1, 1)
    assert reg.evaluation_count("v2") == 1
    assert reg.accuracies("v2") == (1.0, 0.0)


# ---------------------------------------------------------------------------
# Config guard
# ---------------------------------------------------------------------------


def test_shadow_max_below_min_is_rejected():
    with pytest.raises(ValueError, match="shadow_max_evaluations"):
        XGBoostSettings(shadow_min_evaluations=100, shadow_max_evaluations=10)


# ---------------------------------------------------------------------------
# Outcome resolution
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_first_bar_only_opens_a_pair_and_scores_nothing(shadow_window, tmp_path):
    engine = _engine(tmp_path)
    await engine.set_shadow_bundle(_bundle())
    await _drive(engine, [100.0], shadow_p=0.9, live_p=0.1)

    # The outcome of a prediction made on this bar does not exist yet.
    assert engine._registry.evaluation_count("v2") == 0
    assert engine._pending_shadow is not None
    assert engine._pending_shadow.shadow_p_long == 0.9
    assert engine._pending_shadow.live_p_long == 0.1


@pytest.mark.asyncio
async def test_pair_is_scored_against_the_following_bar(shadow_window, tmp_path):
    engine = _engine(tmp_path)
    await engine.set_shadow_bundle(_bundle())
    await _drive(engine, [100.0, 101.0], shadow_p=0.9, live_p=0.1)

    assert engine._registry.evaluation_count("v2") == 1
    # Price rose: the shadow's P(long)=0.9 was right, the live model's 0.1 wrong.
    assert engine._registry.accuracies("v2") == (1.0, 0.0)


@pytest.mark.asyncio
async def test_repeated_tick_on_the_same_bar_does_not_double_count(shadow_window, tmp_path):
    engine = _engine(tmp_path)
    await engine.set_shadow_bundle(_bundle())
    engine._trainer.predict_direction.return_value = (1, 0.9)
    for _ in range(4):
        await engine._evaluate_shadow_tick(_bars(1000, 100.0), _VEC, 0.1)

    assert engine._registry.evaluation_count("v2") == 0


@pytest.mark.asyncio
async def test_unchanged_close_is_dropped_rather_than_scored(shadow_window, tmp_path):
    engine = _engine(tmp_path)
    await engine.set_shadow_bundle(_bundle())
    # Bar advanced but the close did not: there was no direction to predict,
    # so neither model may be credited or debited for it.
    await _drive(engine, [100.0, 100.0], shadow_p=0.9, live_p=0.1)

    assert engine._registry.evaluation_count("v2") == 0
    # ...and a fresh pair is opened for the new bar.
    assert engine._pending_shadow is not None
    assert engine._pending_shadow.bar_ts == 1001


# ---------------------------------------------------------------------------
# Promotion
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_shadow_is_promoted_once_it_beats_the_incumbent(shadow_window, tmp_path):
    engine = _engine(tmp_path)
    bundle = _bundle()
    live_direction = engine._direction_model
    await engine.set_shadow_bundle(bundle)

    await _drive(engine, [100.0, 101.0, 102.0, 103.0], shadow_p=0.9, live_p=0.1)

    assert engine._direction_model is bundle.direction_model
    assert engine._direction_model is not live_direction
    assert engine._meta_model is bundle.meta_model
    assert engine._detector is bundle.detector
    assert engine._ensemble is bundle.ensemble
    assert engine._registry.live_model_id == "v2"
    assert engine._shadow is None
    assert await engine.shadow_status() is None


@pytest.mark.asyncio
async def test_promotion_writes_an_append_only_decision_log_entry(shadow_window, tmp_path):
    engine = _engine(tmp_path)
    await engine.set_shadow_bundle(_bundle())
    await _drive(engine, [100.0, 101.0, 102.0, 103.0], shadow_p=0.9, live_p=0.1)

    log_path = tmp_path / "decision_log.md"
    text = log_path.read_text(encoding="utf-8")
    assert "model_promoted" in text
    assert "v2" in text
    assert "BTC/USDT" in text
    assert "**shadow_accuracy**: 1.0" in text


@pytest.mark.asyncio
async def test_metrics_are_published_only_on_promotion(shadow_window, tmp_path):
    metrics = (
        ModelMetricsRecord(
            model_name="direction",
            timeframe="15m",
            version="v2",
            oos_sharpe=2.0,
            max_drawdown=5.0,
            n_trades=600,
            accuracy=0.6,
            precision_score=0.6,
            recall_score=0.6,
            f1_score=0.6,
            live_gate_pass=True,
        ),
    )
    engine = _engine(tmp_path)
    await engine.set_shadow_bundle(_bundle(metrics=metrics))

    # Mid-window: the candidate is not trading, so the live gate must not be
    # able to read its metrics row.
    await _drive(engine, [100.0, 101.0], shadow_p=0.9, live_p=0.1)
    engine._storage.insert_model_metrics.assert_not_called()

    await _drive(engine, [102.0, 103.0], shadow_p=0.9, live_p=0.1, start=2)
    engine._storage.insert_model_metrics.assert_awaited_once_with(metrics[0])


@pytest.mark.asyncio
async def test_a_failed_metrics_insert_does_not_undo_the_promotion(shadow_window, tmp_path):
    metrics = (
        ModelMetricsRecord(
            model_name="direction",
            timeframe="15m",
            version="v2",
            oos_sharpe=2.0,
            max_drawdown=5.0,
            n_trades=600,
            accuracy=0.6,
            precision_score=0.6,
            recall_score=0.6,
            f1_score=0.6,
            live_gate_pass=True,
        ),
    )
    engine = _engine(tmp_path)
    bundle = _bundle(metrics=metrics)
    engine._storage.insert_model_metrics.side_effect = RuntimeError("db down")
    await engine.set_shadow_bundle(bundle)

    await _drive(engine, [100.0, 101.0, 102.0, 103.0], shadow_p=0.9, live_p=0.1)

    # The swap already happened under the lock; the stale metrics row it
    # leaves behind describes a model that did pass the gate, so the failure
    # is logged rather than rolled back into an inconsistent live slot.
    assert engine._direction_model is bundle.direction_model
    assert (tmp_path / "decision_log.md").exists()


@pytest.mark.asyncio
async def test_a_failed_decision_log_write_does_not_undo_the_promotion(shadow_window, tmp_path):
    engine = _engine(tmp_path)
    bundle = _bundle()
    await engine.set_shadow_bundle(bundle)

    with patch(
        "src.engine.signal_engine._append_decision_log", side_effect=OSError("read-only fs")
    ):
        await _drive(engine, [100.0, 101.0, 102.0, 103.0], shadow_p=0.9, live_p=0.1)

    assert engine._direction_model is bundle.direction_model


# ---------------------------------------------------------------------------
# Abandonment and non-interference
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_shadow_that_never_wins_is_abandoned_not_promoted(shadow_window, tmp_path):
    engine = _engine(tmp_path)
    live_direction = engine._direction_model
    await engine.set_shadow_bundle(_bundle())

    # Price rises every bar; the shadow calls short every time, the live
    # model calls long. 8 bars > shadow_max_evaluations (6).
    await _drive(engine, [100.0 + i for i in range(8)], shadow_p=0.1, live_p=0.9)

    assert engine._shadow is None
    assert engine._registry.shadow_ids() == []
    assert engine._direction_model is live_direction
    assert engine._registry.live_model_id == "initial"


@pytest.mark.asyncio
async def test_shadow_holds_the_live_slot_unchanged_while_it_is_still_being_evaluated(
    shadow_window, tmp_path
):
    engine = _engine(tmp_path)
    live_direction = engine._direction_model
    await engine.set_shadow_bundle(_bundle())

    await _drive(engine, [100.0, 101.0], shadow_p=0.9, live_p=0.1)

    assert engine._direction_model is live_direction
    status = await engine.shadow_status()
    assert status is not None
    assert status["ready_to_promote"] is False
    assert status["evaluations"] == 1


@pytest.mark.asyncio
async def test_a_candidate_that_cannot_score_live_features_is_dropped(shadow_window, tmp_path):
    engine = _engine(tmp_path)
    await engine.set_shadow_bundle(_bundle())
    engine._trainer.predict_direction.side_effect = ValueError("feature schema mismatch")

    await engine._evaluate_shadow_tick(_bars(1000, 100.0), _VEC, 0.5)

    assert engine._shadow is None
    assert engine._registry.shadow_ids() == []


@pytest.mark.asyncio
async def test_disabled_shadow_mode_records_nothing(shadow_window, tmp_path):
    engine = _engine(tmp_path)
    await engine.set_shadow_bundle(_bundle())
    shadow_window.shadow_mode_enabled = False

    await _drive(engine, [100.0, 101.0, 102.0, 103.0], shadow_p=0.9, live_p=0.1)

    assert engine._registry.evaluation_count("v2") == 0
    assert engine._registry.live_model_id == "initial"


# ---------------------------------------------------------------------------
# Shadow lifecycle vs. the live slot
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_newer_candidate_supersedes_an_older_one(shadow_window, tmp_path):
    engine = _engine(tmp_path)
    await engine.set_shadow_bundle(_bundle("v2"))
    await _drive(engine, [100.0, 101.0], shadow_p=0.9, live_p=0.1)
    assert engine._registry.evaluation_count("v2") == 1

    await engine.set_shadow_bundle(_bundle("v3"))

    assert engine._registry.shadow_ids() == ["v3"]
    assert engine._pending_shadow is None
    assert engine._registry.evaluation_count("v3") == 0


@pytest.mark.asyncio
async def test_swapping_the_live_model_invalidates_the_shadow_comparison(shadow_window, tmp_path):
    engine = _engine(tmp_path)
    await engine.set_shadow_bundle(_bundle("v2"))
    await _drive(engine, [100.0, 101.0], shadow_p=0.9, live_p=0.1)

    # The accumulated record answers "better than the model being replaced?",
    # which is no longer the question being asked.
    await engine.swap_models(MagicMock(), MagicMock(), MagicMock(), model_id="v9")

    assert engine._shadow is None
    assert engine._registry.shadow_ids() == []
    assert engine._registry.live_model_id == "v9"
