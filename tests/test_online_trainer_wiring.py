"""
Wiring tests for the TASK-008 online learner.

online_trainer.py had a full incremental-SGD implementation and no caller:
nothing ever fed it a resolved bar, so its warm-up was never reached and
its blend was never applied. These cover the learn gate, the feature-column
projection, and the two blend call sites.
"""

from __future__ import annotations

from collections import OrderedDict
from unittest.mock import MagicMock

import numpy as np
import pandas as pd
import pytest

from src.features.pipeline import FeatureMatrix


def _matrix(index, labels, columns=("f0", "f1")) -> FeatureMatrix:
    idx = pd.DatetimeIndex(index)
    features = pd.DataFrame(
        {c: np.linspace(1.0, 2.0, len(idx)) for c in columns},
        index=idx,
    )
    return FeatureMatrix(
        features=features,
        labels=pd.Series(labels, index=idx, dtype="float64"),
        meta=None,
        daily_vol=pd.Series(0.01, index=idx),
        log_returns=pd.Series(0.0, index=idx),
    )


def _engine(online_trainer):
    """SignalEngine with only the attributes the online path touches."""
    from src.engine.signal_engine import SignalEngine

    engine = object.__new__(SignalEngine)
    engine._online_trainer = online_trainer
    engine._last_online_learn_ts = None
    engine._online_feature_cols = None
    engine._p_long_by_bar = OrderedDict()
    engine._log = MagicMock()
    return engine


_TS = ["2026-01-01 00:00", "2026-01-01 00:15", "2026-01-01 00:30"]


def _ts_key(ts: str) -> int:
    return int(pd.Timestamp(ts).value)


class TestLearnOnline:
    def test_no_trainer_is_a_no_op(self) -> None:
        engine = _engine(None)
        engine._learn_online(_matrix(_TS, [1.0, -1.0, 1.0]))
        assert engine._online_feature_cols is None

    def test_learns_the_newest_labelled_bar(self) -> None:
        trainer = MagicMock()
        engine = _engine(trainer)
        engine._learn_online(_matrix(_TS, [1.0, -1.0, 1.0]))
        trainer.learn_direction.assert_called_once()
        assert trainer.learn_direction.call_args.kwargs["label"] == 1

    def test_negative_label_becomes_short(self) -> None:
        trainer = MagicMock()
        engine = _engine(trainer)
        engine._learn_online(_matrix(_TS, [1.0, 1.0, -1.0]))
        assert trainer.learn_direction.call_args.kwargs["label"] == 0

    def test_unlabelled_tail_rows_are_skipped(self) -> None:
        """
        The triple-barrier labeller cannot label the newest bars. Learning
        from the last feature row would pair a bar with another bar's label.
        """
        trainer = MagicMock()
        engine = _engine(trainer)
        fm = _matrix(_TS, [1.0, -1.0, float("nan")])
        engine._learn_online(fm)
        learned = trainer.learn_direction.call_args.args[0]
        # Second row's features, not the third's.
        assert learned == pytest.approx(fm.features.iloc[1].to_numpy())

    def test_zero_label_carries_no_direction_and_is_dropped(self) -> None:
        trainer = MagicMock()
        engine = _engine(trainer)
        engine._learn_online(_matrix(_TS, [1.0, -1.0, 0.0]))
        trainer.learn_direction.assert_not_called()

    def test_the_meta_model_is_fed_too(self) -> None:
        """
        blend() warms up on min(dir_samples, meta_samples) >= 50. Feeding only
        the direction model would leave the meta counter at zero forever and
        the blend would never activate -- inert with a producer attached.
        """
        trainer = MagicMock()
        engine = _engine(trainer)
        engine._p_long_by_bar[_ts_key(_TS[-1])] = 0.73
        engine._learn_online(_matrix(_TS, [1.0, -1.0, 1.0]))
        trainer.learn_meta.assert_called_once()
        assert trainer.learn_meta.call_args.kwargs["label"] == 1
        assert trainer.learn_meta.call_args.kwargs["p_long"] == pytest.approx(0.73)

    def test_an_unresolved_bar_is_a_real_zero_for_the_meta_model(self) -> None:
        """Meta learns which bars resolved at a barrier; 0 is data, not a gap."""
        trainer = MagicMock()
        engine = _engine(trainer)
        engine._p_long_by_bar[_ts_key(_TS[-1])] = 0.5
        engine._learn_online(_matrix(_TS, [1.0, -1.0, 0.0]))
        trainer.learn_meta.assert_called_once()
        assert trainer.learn_meta.call_args.kwargs["label"] == 0

    def test_meta_is_not_relearned_on_a_repeated_tick(self) -> None:
        trainer = MagicMock()
        engine = _engine(trainer)
        engine._p_long_by_bar[_ts_key(_TS[-1])] = 0.5
        fm = _matrix(_TS, [1.0, -1.0, 0.0])
        engine._learn_online(fm)
        engine._learn_online(fm)
        assert trainer.learn_meta.call_count == 1

    def test_the_same_bar_is_not_learned_twice(self) -> None:
        """Ticks arrive faster than bars close."""
        trainer = MagicMock()
        engine = _engine(trainer)
        fm = _matrix(_TS, [1.0, -1.0, 1.0])
        engine._learn_online(fm)
        engine._learn_online(fm)
        engine._learn_online(fm)
        assert trainer.learn_direction.call_count == 1

    def test_a_new_bar_is_learned(self) -> None:
        trainer = MagicMock()
        engine = _engine(trainer)
        engine._learn_online(_matrix(_TS, [1.0, -1.0, 1.0]))
        engine._learn_online(_matrix([*_TS, "2026-01-01 00:45"], [1.0, -1.0, 1.0, -1.0]))
        assert trainer.learn_direction.call_count == 2

    def test_all_null_labels_learn_nothing(self) -> None:
        trainer = MagicMock()
        engine = _engine(trainer)
        engine._learn_online(_matrix(_TS, [float("nan")] * 3))
        trainer.learn_direction.assert_not_called()

    def test_a_learner_fault_never_costs_a_tick(self) -> None:
        trainer = MagicMock()
        trainer.learn_direction.side_effect = RuntimeError("boom")
        engine = _engine(trainer)
        engine._learn_online(_matrix(_TS, [1.0, -1.0, 1.0]))  # must not raise

    def test_records_the_fitted_column_order(self) -> None:
        trainer = MagicMock()
        engine = _engine(trainer)
        engine._learn_online(_matrix(_TS, [1.0, -1.0, 1.0], columns=("a", "b", "c")))
        assert engine._online_feature_cols == ["a", "b", "c"]


class TestBlendOnline:
    def test_no_trainer_returns_the_batch_values(self) -> None:
        engine = _engine(None)
        assert engine._blend_online(0.7, 0.6, pd.Series({"f0": 1.0})) == (0.7, 0.6, 0.0)

    def test_cold_learner_returns_the_batch_values(self) -> None:
        """Never fitted => no column order => nothing to project onto."""
        engine = _engine(MagicMock())
        assert engine._blend_online(0.7, 0.6, pd.Series({"f0": 1.0})) == (0.7, 0.6, 0.0)

    def test_live_vector_is_projected_onto_the_fitted_columns(self) -> None:
        """
        The live inference vector carries intelligence columns the historical
        feature matrix does not. Without projection sklearn sees a different
        feature count than it was fitted with and every blend fails open.
        """
        trainer = MagicMock()
        trainer.blend.return_value = MagicMock(p_long=0.66, p_bet=0.55, online_weight=0.15)
        engine = _engine(trainer)
        engine._online_feature_cols = ["f0", "f1"]

        vec = pd.Series({"f0": 1.0, "f1": 2.0, "intelligence_extra": 9.0})
        assert engine._blend_online(0.7, 0.6, vec) == (0.66, 0.55, 0.15)
        passed = trainer.blend.call_args.kwargs["feature_vec"]
        assert list(passed) == [1.0, 2.0]

    def test_a_column_missing_from_the_live_vector_becomes_zero(self) -> None:
        trainer = MagicMock()
        trainer.blend.return_value = MagicMock(p_long=0.5, p_bet=0.5, online_weight=0.15)
        engine = _engine(trainer)
        engine._online_feature_cols = ["f0", "f1"]
        engine._blend_online(0.7, 0.6, pd.Series({"f0": 1.0}))
        assert list(trainer.blend.call_args.kwargs["feature_vec"]) == [1.0, 0.0]

    def test_a_blend_fault_falls_back_to_the_batch_values(self) -> None:
        trainer = MagicMock()
        trainer.blend.side_effect = RuntimeError("boom")
        engine = _engine(trainer)
        engine._online_feature_cols = ["f0"]
        assert engine._blend_online(0.7, 0.6, pd.Series({"f0": 1.0})) == (0.7, 0.6, 0.0)


class TestPersistence:
    def test_shutdown_saves_every_engines_learner(self) -> None:
        from src.engine.orchestrator import Orchestrator

        orch = object.__new__(Orchestrator)
        orch._log = MagicMock()
        first, second = MagicMock(), MagicMock()
        orch._engines = {
            "15m": MagicMock(_online_trainer=first),
            "4h": MagicMock(_online_trainer=second),
        }
        orch._persist_online_trainers()
        first.save.assert_called_once()
        second.save.assert_called_once()

    def test_one_failed_save_does_not_stop_the_others(self) -> None:
        """Shutdown still has executors to close after this."""
        from src.engine.orchestrator import Orchestrator

        orch = object.__new__(Orchestrator)
        orch._log = MagicMock()
        broken, healthy = MagicMock(), MagicMock()
        broken.save.side_effect = RuntimeError("disk full")
        orch._engines = {
            "15m": MagicMock(_online_trainer=broken),
            "4h": MagicMock(_online_trainer=healthy),
        }
        orch._persist_online_trainers()
        healthy.save.assert_called_once()

    def test_engines_without_a_learner_are_skipped(self) -> None:
        from src.engine.orchestrator import Orchestrator

        orch = object.__new__(Orchestrator)
        orch._log = MagicMock()
        orch._engines = {"15m": MagicMock(_online_trainer=None)}
        orch._persist_online_trainers()  # must not raise


class TestPLongRecording:
    def test_a_bar_without_a_recorded_p_long_is_not_meta_trained(self) -> None:
        """
        Faking a constant would train the meta model on a feature that never
        varies and then score it with one that does -- blend() appends the
        live batch_p_long at inference.
        """
        trainer = MagicMock()
        engine = _engine(trainer)
        engine._learn_online(_matrix(_TS, [1.0, -1.0, 1.0]))
        trainer.learn_meta.assert_not_called()
        # Direction learning is unaffected; it never consumed p_long.
        trainer.learn_direction.assert_called_once()

    def test_the_batch_probability_is_recorded_for_the_latest_bar(self) -> None:
        engine = _engine(MagicMock())
        bars = pd.DataFrame({"close": [1.0, 2.0]}, index=pd.DatetimeIndex(_TS[:2]))
        engine._record_p_long_for_bar(bars, 0.61)
        assert engine._p_long_by_bar[_ts_key(_TS[1])] == pytest.approx(0.61)

    def test_the_history_is_bounded(self) -> None:
        """An unbounded dict here would grow for the life of the process."""
        from src.engine.signal_engine import _P_LONG_HISTORY_BARS

        engine = _engine(MagicMock())
        base = pd.Timestamp("2026-01-01")
        for i in range(_P_LONG_HISTORY_BARS + 50):
            bars = pd.DataFrame(
                {"close": [1.0]}, index=pd.DatetimeIndex([base + pd.Timedelta(minutes=i)])
            )
            engine._record_p_long_for_bar(bars, 0.5)
        assert len(engine._p_long_by_bar) == _P_LONG_HISTORY_BARS

    def test_the_oldest_entries_are_evicted_first(self) -> None:
        from src.engine.signal_engine import _P_LONG_HISTORY_BARS

        engine = _engine(MagicMock())
        base = pd.Timestamp("2026-01-01")
        for i in range(_P_LONG_HISTORY_BARS + 1):
            bars = pd.DataFrame(
                {"close": [1.0]}, index=pd.DatetimeIndex([base + pd.Timedelta(minutes=i)])
            )
            engine._record_p_long_for_bar(bars, 0.5)
        assert int(base.value) not in engine._p_long_by_bar
        newest = base + pd.Timedelta(minutes=_P_LONG_HISTORY_BARS)
        assert int(newest.value) in engine._p_long_by_bar

    def test_empty_bars_record_nothing(self) -> None:
        engine = _engine(MagicMock())
        engine._record_p_long_for_bar(pd.DataFrame(), 0.5)
        assert len(engine._p_long_by_bar) == 0

    def test_the_recorded_value_is_the_pre_blend_probability(self) -> None:
        """
        Recording the blended value would let the online model learn from its
        own output and drift toward self-confirmation.
        """
        import inspect

        from src.engine.signal_engine import SignalEngine

        source = inspect.getsource(SignalEngine.tick)
        record_at = source.index("self._record_p_long_for_bar(")
        blend_at = source.index("self._blend_online(p_long, 0.5, vec)")
        assert record_at < blend_at
