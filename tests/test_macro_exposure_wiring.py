"""
Wiring tests for the v7 macro exposure overlay.

The budget module and its classifier both shipped with unit tests but no
caller; these cover the orchestrator producer that now feeds them, with the
emphasis on the fail-safe paths — a macro fault must never widen exposure.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pandas as pd
import pytest
from conftest import settings_double

from src.config import Timeframe, TradingMode

MIN_ROWS = 12


def _make_storage(features: pd.DataFrame | None = None, latest_ts: int | None = 1_700_000_000_000):
    storage = MagicMock()
    storage.latest_bar_ts = AsyncMock(return_value=latest_ts)
    storage.fetch_intelligence_features = AsyncMock(
        return_value=pd.DataFrame() if features is None else features
    )
    return storage


def _make_orch(storage, *, enabled: bool = True, lookback: int = 30):
    from src.engine.orchestrator import Orchestrator

    with patch("src.engine.orchestrator.get_settings") as mock_cfg:
        cfg = settings_double()
        cfg.primary_symbol = "BTC/USDT"
        cfg.active_timeframes = [Timeframe.INTRADAY]
        cfg.primary_timeframe = Timeframe.INTRADAY
        cfg.trading_mode = TradingMode.PAPER
        cfg.starting_capital_usd = 1000.0
        cfg.storage.model_dir = "/tmp/models"
        cfg.risk.macro_exposure_enabled = enabled
        cfg.risk.macro_exposure_lookback_bars = lookback
        mock_cfg.return_value = cfg
        return Orchestrator(storage, MagicMock())


def _risk_off_features(rows: int = MIN_ROWS) -> pd.DataFrame:
    """Crowded longs + heavy exchange inflows + shrinking stablecoins."""
    return pd.DataFrame(
        {
            "intelligence_binance_funding_rate_pct": [0.01] * (rows - 1) + [0.5],
            "intelligence_stablecoin_reserve_ratio": [1.0] * (rows - 1) + [0.5],
            "intelligence_exchange_netflow_7d_zscore": [0.5] * (rows - 1) + [3.0],
        },
        index=pd.RangeIndex(rows, name="bar_ts"),
    )


@pytest.mark.asyncio
async def test_disabled_overlay_returns_none() -> None:
    storage = _make_storage(_risk_off_features())
    orch = _make_orch(storage, enabled=False)
    assert await orch._macro_exposure_budget(Timeframe.INTRADAY) is None
    storage.fetch_intelligence_features.assert_not_awaited()


@pytest.mark.asyncio
async def test_no_bars_returns_none() -> None:
    storage = _make_storage(_risk_off_features(), latest_ts=None)
    orch = _make_orch(storage)
    assert await orch._macro_exposure_budget(Timeframe.INTRADAY) is None


@pytest.mark.asyncio
async def test_empty_feature_history_returns_none_not_a_neutral_shrink() -> None:
    """No macro data must mean no overlay, not a ~0.62 blanket haircut."""
    orch = _make_orch(_make_storage(None))
    assert await orch._macro_exposure_budget(Timeframe.INTRADAY) is None


@pytest.mark.asyncio
async def test_risk_off_window_produces_a_shrinking_scalar() -> None:
    orch = _make_orch(_make_storage(_risk_off_features()))
    budget = await orch._macro_exposure_budget(Timeframe.INTRADAY)
    assert budget is not None
    assert 0.25 <= budget.scalar < 1.0
    assert "risk_appetite" in budget.reason


@pytest.mark.asyncio
async def test_scalar_never_exceeds_one_on_maximal_risk_on() -> None:
    """The overlay is shrink-only: it must never widen the Kelly ceiling."""
    rows = MIN_ROWS
    features = pd.DataFrame(
        {
            "intelligence_binance_funding_rate_pct": [0.5] * (rows - 1) + [-0.5],
            "intelligence_stablecoin_reserve_ratio": [1.0] * (rows - 1) + [10.0],
            "intelligence_exchange_netflow_7d_zscore": [0.0] * (rows - 1) + [-9.0],
        },
        index=pd.RangeIndex(rows, name="bar_ts"),
    )
    orch = _make_orch(_make_storage(features))
    budget = await orch._macro_exposure_budget(Timeframe.INTRADAY)
    assert budget is not None
    assert budget.scalar <= 1.0


@pytest.mark.asyncio
async def test_lookback_window_is_measured_backwards_from_the_latest_bar() -> None:
    """
    fetch_intelligence_features orders ascending, so the window has to be
    selected with since_ts; a bare LIMIT would return the oldest rows.
    """
    from src.config import TIMEFRAME_SECONDS

    latest_ts = 1_700_000_000_000
    storage = _make_storage(_risk_off_features(), latest_ts=latest_ts)
    orch = _make_orch(storage, lookback=30)
    await orch._macro_exposure_budget(Timeframe.INTRADAY)

    kwargs = storage.fetch_intelligence_features.await_args.kwargs
    expected = latest_ts - 30 * TIMEFRAME_SECONDS[Timeframe.INTRADAY] * 1000
    assert kwargs["since_ts"] == expected
    assert kwargs["symbol"] == "BTC/USDT"
    assert kwargs["timeframe"] == Timeframe.INTRADAY.value
