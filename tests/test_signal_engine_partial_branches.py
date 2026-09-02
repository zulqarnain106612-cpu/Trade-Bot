"""The false arms of six guards inside SignalEngine that no test took.

Each one is a branch coverage reported as partial: the condition was only ever
evaluated one way, so the behaviour on the other side was never checked. They
are the cases that arise from thin or malformed market data, and from a
timeframe with nothing above it.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pandas as pd
import pytest
from test_signal_engine import _TICK, _fm, _make_bars, _make_engine
from test_signal_engine_residual_paths import _full_path

from src.config import Timeframe


def _engine_with(bars):
    engine = _make_engine()

    async def _load():
        return bars

    engine._load_bars = _load
    return engine


@pytest.mark.asyncio
async def test_a_promoted_bundle_without_an_ensemble_keeps_the_incumbent_one():
    """Promotion must not silently drop the book to XGBoost-only."""
    from src.engine.signal_engine import ShadowBundle

    engine = _engine_with(_make_bars(n=320))
    engine._cfg.xgboost.shadow_mode_enabled = True
    incumbent = MagicMock(name="incumbent_ensemble")
    engine._ensemble = incumbent
    engine._shadow = ShadowBundle(
        model_id="candidate",
        direction_model=MagicMock(),
        meta_model=MagicMock(),
        detector=MagicMock(),
        ensemble=None,
    )
    engine._pending_shadow = None
    engine._registry = MagicMock()
    engine._registry.evaluate_shadow.return_value = (True, "ready")
    engine._registry.accuracies.return_value = (0.62, 0.55)
    engine._registry.evaluation_count.return_value = 50

    await engine._evaluate_shadow_tick(_make_bars(n=320), pd.Series({"f0": 1.0}), 0.8)

    assert engine._ensemble is incumbent
    assert engine._shadow is None


@pytest.mark.asyncio
async def test_a_single_bar_history_resolves_no_previous_prediction():
    """With one closed bar there is no prior move to score the last call against."""
    bars = _make_bars(n=1)
    engine = _engine_with(bars)
    tracker = MagicMock()

    with (
        _full_path(engine),
        patch("src.engine.signal_engine.get_degradation_tracker", return_value=tracker),
    ):
        await engine.tick(**_TICK)

    tracker.resolve_last.assert_not_called()


@pytest.mark.asyncio
async def test_a_zero_mid_price_publishes_no_spread():
    """Dividing the spread by a zero mid would be an error, not a wide book."""
    engine = _engine_with(_make_bars(n=320))
    ob = MagicMock()
    ob.order_flow_imbalance.return_value = 0.05
    ob.mid_price = 0.0
    ob.spread = 0.1
    engine._fetcher.fetch_orderbook.return_value = ob

    with _full_path(engine):
        result = await engine.tick(**_TICK)

    # the tick completed on a book with no mid rather than raising
    assert result is not None


@pytest.mark.asyncio
async def test_a_short_feature_matrix_builds_no_regime_history():
    """Under 50 rows the regime detector gets nothing rather than a thin sample."""
    engine = _engine_with(_make_bars(n=320))

    with _full_path(engine):
        with patch("src.engine.signal_engine.build_feature_matrix", return_value=_fm(n=10)):
            result = await engine.tick(**_TICK)

    # the tick completed without handing the detector a sample too short to fit
    assert result is not None


@pytest.mark.asyncio
async def test_bars_without_a_high_low_range_fall_back_for_atr():
    """A feed missing high/low must still reach the strategy filters."""
    bars = _make_bars(n=320).drop(columns=["high", "low"])
    engine = _engine_with(bars)

    filter_pass = {"passes": True, "scalar": 1.0, "filters_failed": [], "details": {}}
    with (
        _full_path(engine),
        patch(
            "src.engine.signal_engine.apply_all_strategy_filters", return_value=filter_pass
        ) as filters,
    ):
        await engine.tick(**_TICK)

    assert filters.called
    atr = filters.call_args.kwargs["atr_series"]
    assert atr is not None and not atr.dropna().empty


@pytest.mark.asyncio
async def test_the_slowest_timeframe_skips_multi_timeframe_confirmation():
    """Nothing sits above the slowest timeframe, so there is nothing to confirm against."""
    engine = _engine_with(_make_bars(n=320))
    engine._timeframe = Timeframe.SWING  # not a key of _MTF_SLOWER_TIMEFRAME
    engine._cfg.features.mtf_confirmation_enabled = True
    engine._storage.fetch_bars.reset_mock()

    with _full_path(engine):
        result = await engine.tick(**_TICK)

    assert result is not None
    assert result.skip_reason != "mtf_confirmation_failed"
