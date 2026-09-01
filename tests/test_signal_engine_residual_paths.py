"""Residual branches inside SignalEngine.tick().

These sit deep in the tick: the regime-ensemble disagreement warning, the
shadow-evaluation and p_long-recording fail-open arms, the CVaR notional
ceiling composing with the Carver cap, the capital-floor rejection and the
advisory size scalar. They are driven through the same harness the rest of
the SignalEngine tests use.
"""

from __future__ import annotations

import contextlib
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd
import pytest
from test_signal_engine import (
    _TICK,
    _fm,
    _make_bars,
    _make_engine,
    _mock_cognitive,
    _mock_kelly,
    _pass_gate,
)


def _engine():
    """Engine whose bar loader yields a full, closed history.

    _make_engine's storage double returns a DataFrame, but _load_bars expects
    BarRecords -- the tick-level tests all substitute the loader instead.
    """
    engine = _make_engine()
    bars = _make_bars(n=320)

    async def _load():
        return bars

    engine._load_bars = _load
    return engine


@contextlib.contextmanager
def _full_path(engine, gate=None):
    """Patch the tick's collaborators so it runs end to end and trades.

    Mirrors test_signal_engine.py's full-pipeline test; the branches under
    test here all sit after the gate, which the raw fixtures never reach.
    """
    filter_pass = {"passes": True, "scalar": 1.0, "filters_failed": [], "details": {"hurst": 0.5}}
    with contextlib.ExitStack() as stack:
        for ctx in (
            # keep the tick off the network: the intel aggregator otherwise
            # tries real provider calls and each tick waits out their timeouts
            patch(
                "src.engine.signal_engine._get_intel_aggregator",
                side_effect=RuntimeError("no intel providers in tests"),
            ),
            patch("src.engine.signal_engine.build_feature_matrix", return_value=_fm()),
            patch(
                "src.engine.signal_engine.build_inference_features",
                return_value=pd.Series({"f0": 1.0}),
            ),
            patch(
                "src.engine.signal_engine.evaluate_all_gates",
                return_value=gate if gate is not None else _pass_gate(),
            ),
            patch("src.engine.signal_engine.compute_position_size", return_value=_mock_kelly()),
            patch(
                "src.engine.signal_engine.get_cognitive_engine",
                return_value=_mock_cognitive(passed=True),
            ),
            patch("src.engine.signal_engine.apply_all_strategy_filters", return_value=filter_pass),
            patch.object(engine._trainer, "predict_direction", return_value=(1, 0.8)),
            patch.object(engine._trainer, "predict_meta", return_value=(1, 0.8)),
        ):
            stack.enter_context(ctx)
        yield


@pytest.mark.asyncio
async def test_regime_disagreement_halves_the_position_scalar():
    engine = _engine()

    class _Result:
        agreement_score = 0.1
        regime_state = 2

    with (
        _full_path(engine),
        patch("src.engine.signal_engine.combine_regime_votes", return_value=_Result()),
    ):
        result = await engine.tick(**_TICK)

    assert result.tradeable is True


@pytest.mark.asyncio
async def test_a_failing_regime_ensemble_does_not_stop_the_tick():
    engine = _engine()

    with (
        _full_path(engine),
        patch(
            "src.engine.signal_engine.combine_regime_votes",
            side_effect=RuntimeError("vote combiner blew up"),
        ),
    ):
        result = await engine.tick(**_TICK)

    assert result.tradeable is True


@pytest.mark.asyncio
async def test_a_failing_shadow_evaluation_does_not_cost_the_signal():
    engine = _engine()

    async def _boom(*_a, **_kw):
        raise RuntimeError("shadow bundle missing")

    engine._evaluate_shadow_tick = _boom
    with _full_path(engine):
        result = await engine.tick(**_TICK)

    assert result.tradeable is True


@pytest.mark.asyncio
async def test_an_unusable_equity_mark_skips_the_tick():
    engine = _engine()
    engine._capital_floor.update_equity = MagicMock(side_effect=ValueError("negative equity"))

    with _full_path(engine):
        result = await engine.tick(**_TICK)

    assert result.tradeable is False
    assert result.skip_reason == "invalid_equity_mark"


class TestCvarNotionalCap:
    def _bars(self, n: int = 300) -> pd.DataFrame:
        return _make_bars(n)

    def test_no_ceiling_when_the_limit_is_not_configured(self):
        engine = _make_engine()
        engine._cfg.risk.cvar_limit_pct = None

        assert engine._cvar_notional_cap(self._bars(), 10_000.0) is None

    def test_no_ceiling_without_enough_history_for_a_tail_estimate(self):
        engine = _make_engine()
        engine._cfg.risk.cvar_limit_pct = 0.02

        assert engine._cvar_notional_cap(self._bars(50), 10_000.0) is None

    def test_no_ceiling_when_the_tail_estimate_finds_no_loss(self):
        engine = _make_engine()
        engine._cfg.risk.cvar_limit_pct = 0.02
        engine._risk_quantifier = MagicMock()
        engine._risk_quantifier.value_at_risk.return_value = {"cvar": 0.0}

        assert engine._cvar_notional_cap(self._bars(), 10_000.0) is None

    def test_a_loss_tail_produces_a_notional_ceiling(self):
        engine = _make_engine()
        engine._cfg.risk.cvar_limit_pct = 0.02
        engine._risk_quantifier = MagicMock()
        engine._risk_quantifier.value_at_risk.return_value = {"cvar": -0.04}

        cap = engine._cvar_notional_cap(self._bars(), 10_000.0)

        assert cap == pytest.approx(0.02 * 10_000.0 / 0.04)

    def test_no_ceiling_when_the_estimate_raises(self):
        engine = _make_engine()
        engine._cfg.risk.cvar_limit_pct = 0.02
        engine._risk_quantifier = MagicMock()
        engine._risk_quantifier.value_at_risk.side_effect = RuntimeError("solver failed")

        assert engine._cvar_notional_cap(self._bars(), 10_000.0) is None

    def test_a_non_finite_estimate_publishes_no_ceiling(self):
        engine = _make_engine()
        engine._cfg.risk.cvar_limit_pct = 0.02
        engine._risk_quantifier = MagicMock()
        engine._risk_quantifier.value_at_risk.return_value = {"cvar": float("-inf")}

        assert engine._cvar_notional_cap(self._bars(), 10_000.0) is None


@pytest.mark.asyncio
async def test_the_cvar_ceiling_composes_with_the_carver_cap():
    engine = _engine()
    engine._cvar_notional_cap = MagicMock(return_value=1.0)  # tighter than any Carver cap

    with _full_path(engine):
        result = await engine.tick(**_TICK)

    assert result is not None
    engine._cvar_notional_cap.assert_called_once()


@pytest.mark.asyncio
async def test_a_p_long_recording_failure_is_only_logged():
    engine = _engine()
    engine._p_long_by_bar = MagicMock()
    engine._p_long_by_bar.__setitem__.side_effect = RuntimeError("mapping is broken")

    with _full_path(engine):
        result = await engine.tick(**_TICK)

    assert result.tradeable is True


class TestOnlineLearning:
    def test_no_online_trainer_is_a_no_op(self):
        engine = _make_engine()
        engine._online_trainer = None

        engine._learn_online(MagicMock())

    def test_a_feature_matrix_without_labels_is_skipped(self):
        engine = _make_engine()
        engine._online_trainer = MagicMock()
        fm = MagicMock()
        fm.labels = None

        engine._learn_online(fm)

        engine._online_trainer.partial_fit.assert_not_called()

    def test_labels_that_are_all_unresolved_are_skipped(self):
        engine = _make_engine()
        engine._online_trainer = MagicMock()
        fm = MagicMock()
        fm.labels = pd.Series([np.nan, np.nan], index=pd.RangeIndex(2))

        engine._learn_online(fm)

        engine._online_trainer.partial_fit.assert_not_called()

    def test_a_label_whose_bar_is_missing_from_the_features_is_skipped(self):
        engine = _make_engine()
        engine._online_trainer = MagicMock()
        fm = MagicMock()
        fm.labels = pd.Series([1.0], index=pd.to_datetime(["2025-01-02"]))
        fm.features = pd.DataFrame(index=pd.to_datetime(["2025-01-01"]))

        engine._learn_online(fm)

        engine._online_trainer.partial_fit.assert_not_called()


def _advisory_gate(scalar: float):
    """A gate that passes but asks for a smaller position."""
    from src.risk.gates import GateResult

    passed = GateResult.pass_gate()
    return GateResult(
        status=passed.status,
        passed=True,
        reason="advisory reduction",
        details=dict(passed.details),
        size_scalar=scalar,
    )


@pytest.mark.asyncio
async def test_an_advisory_gate_reduces_the_order_size():
    engine = _engine()

    with _full_path(engine, gate=_advisory_gate(0.5)):
        result = await engine.tick(**_TICK)

    assert result.tradeable is True
    assert result.kelly_result.notional_usd < 500.0


@pytest.mark.asyncio
async def test_a_reduction_below_the_exchange_minimum_skips_the_trade():
    engine = _engine()

    with (
        _full_path(engine, gate=_advisory_gate(0.5)),
        patch("src.engine.signal_engine.apply_size_scalar", return_value=None),
    ):
        result = await engine.tick(**_TICK)

    assert result.tradeable is False
    assert result.skip_reason == "advisory_scalar_below_minimum"


class TestPLongHistory:
    def test_no_bars_records_nothing(self):
        engine = _engine()

        engine._record_p_long_for_bar(None, 0.7)
        engine._record_p_long_for_bar(pd.DataFrame(), 0.7)

        assert not engine._p_long_by_bar

    def test_the_history_is_trimmed_to_its_bound(self):
        from src.engine.signal_engine import _P_LONG_HISTORY_BARS

        engine = _engine()
        bars = _make_bars(n=_P_LONG_HISTORY_BARS + 10)
        for i in range(len(bars)):
            engine._record_p_long_for_bar(bars.iloc[: i + 1], 0.5 + i * 1e-6)

        assert len(engine._p_long_by_bar) == _P_LONG_HISTORY_BARS
        # the oldest bars were evicted, the newest kept
        assert int(pd.Timestamp(bars.index[-1]).value) in engine._p_long_by_bar
        assert int(pd.Timestamp(bars.index[0]).value) not in engine._p_long_by_bar


@pytest.mark.asyncio
async def test_a_pending_observation_from_a_replaced_bundle_is_discarded():
    from src.engine.signal_engine import ShadowBundle, _PendingShadowObservation

    engine = _engine()
    engine._cfg.xgboost.shadow_mode_enabled = True
    engine._shadow = ShadowBundle(
        model_id="new-bundle",
        direction_model=MagicMock(),
        meta_model=MagicMock(),
        detector=MagicMock(),
    )
    bars = _make_bars(n=320)
    engine._pending_shadow = _PendingShadowObservation(
        model_id="old-bundle",  # a bundle that has since been swapped out
        bar_ts=int(bars.index[-2]),
        close=float(bars["close"].iloc[-2]),
        live_p_long=0.6,
        shadow_p_long=0.7,
    )
    engine._registry = MagicMock()

    await engine._evaluate_shadow_tick(bars, pd.Series({"f0": 1.0}), 0.8)

    engine._registry.record_shadow_prediction.assert_not_called()


def test_a_history_of_mostly_missing_closes_publishes_no_cvar_ceiling():
    engine = _engine()
    engine._cfg.risk.cvar_limit_pct = 0.02
    bars = _make_bars(n=150).copy()
    # 101 closes reach the length check, but the NaNs leave under 100 returns
    bars.iloc[-100:, bars.columns.get_loc("close")] = np.nan

    assert engine._cvar_notional_cap(bars, 10_000.0) is None
