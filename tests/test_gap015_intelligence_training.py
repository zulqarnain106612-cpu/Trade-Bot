"""
Tests for GAP-015: intelligence features actually reaching training.

The trainer resolved its column set from
`getattr(fm, "intelligence_coverage", None)` and nothing ever set that
attribute — so coverage was always None, the active set was always the 8
base columns, and the 18 intelligence features never reached training.
Inference injected them anyway, where predict_direction/predict_meta sliced
them straight back off to match the model's n_features_in_.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pandas as pd
import pytest

from src.config import Timeframe
from src.features.pipeline import BASE_FEATURE_COLUMNS, get_active_feature_columns


def _fm(index) -> MagicMock:
    fm = MagicMock()
    fm.features = pd.DataFrame({c: [0.1] * len(index) for c in BASE_FEATURE_COLUMNS}, index=index)
    return fm


def _orch(coverage: dict | None, intel: pd.DataFrame | None, *, raises=False):
    from src.engine.orchestrator import Orchestrator

    orch = object.__new__(Orchestrator)
    orch._symbol = "BTC/USDT"
    orch._log = MagicMock()
    storage = MagicMock()
    if raises:
        storage.intelligence_feature_coverage = AsyncMock(side_effect=RuntimeError("db"))
    else:
        storage.intelligence_feature_coverage = AsyncMock(
            return_value={"coverage": coverage} if coverage is not None else {}
        )
    storage.fetch_intelligence_features = AsyncMock(
        return_value=intel if intel is not None else pd.DataFrame()
    )
    orch._storage = storage
    return orch


_TS = [1_000, 2_000, 3_000]
_COVERAGE = {"intelligence_binance_funding_rate_pct": 0.9, "intelligence_sopr": 0.2}


def _intel_frame(index=None) -> pd.DataFrame:
    index = index if index is not None else _TS
    return pd.DataFrame(
        {
            "intelligence_binance_funding_rate_pct": [0.01] * len(index),
            "intelligence_sopr": [1.0] * len(index),
        },
        index=index,
    )


class TestAttachment:
    @pytest.mark.asyncio
    async def test_coverage_is_attached_so_the_trainer_can_see_it(self) -> None:
        """The attribute the trainer reads was never set by anything."""
        fm = _fm(_TS)
        await _orch(_COVERAGE, _intel_frame())._attach_intelligence_features(fm, Timeframe.INTRADAY)
        assert fm.intelligence_coverage == _COVERAGE

    @pytest.mark.asyncio
    async def test_intelligence_columns_are_joined_onto_the_matrix(self) -> None:
        """
        Coverage alone is not enough: the trainer drops active columns that
        are absent from the matrix, so it would just log and discard them.
        """
        fm = _fm(_TS)
        await _orch(_COVERAGE, _intel_frame())._attach_intelligence_features(fm, Timeframe.INTRADAY)
        assert "intelligence_binance_funding_rate_pct" in fm.features.columns
        assert "intelligence_sopr" in fm.features.columns

    @pytest.mark.asyncio
    async def test_base_columns_survive_the_join(self) -> None:
        fm = _fm(_TS)
        await _orch(_COVERAGE, _intel_frame())._attach_intelligence_features(fm, Timeframe.INTRADAY)
        assert set(BASE_FEATURE_COLUMNS) <= set(fm.features.columns)

    @pytest.mark.asyncio
    async def test_a_bar_without_intelligence_is_kept_not_dropped(self) -> None:
        """
        Left join on bar timestamp: bars are authoritative. The resulting NaN
        is what the coverage gate exists to judge.
        """
        fm = _fm(_TS)
        await _orch(_COVERAGE, _intel_frame(index=[1_000]))._attach_intelligence_features(
            fm, Timeframe.INTRADAY
        )
        assert len(fm.features) == 3
        assert fm.features["intelligence_sopr"].isna().sum() == 2


class TestDegradesToBaseFeatures:
    @pytest.mark.asyncio
    async def test_no_coverage_report_attaches_nothing(self) -> None:
        fm = _fm(_TS)
        await _orch(None, _intel_frame())._attach_intelligence_features(fm, Timeframe.INTRADAY)
        assert not hasattr(fm.features, "intelligence_sopr")
        assert "intelligence_sopr" not in fm.features.columns

    @pytest.mark.asyncio
    async def test_an_empty_intelligence_table_attaches_nothing(self) -> None:
        """The state of a deployment that never persisted live intelligence."""
        fm = _fm(_TS)
        await _orch(_COVERAGE, pd.DataFrame())._attach_intelligence_features(fm, Timeframe.INTRADAY)
        assert list(fm.features.columns) == list(BASE_FEATURE_COLUMNS)

    @pytest.mark.asyncio
    async def test_a_storage_fault_degrades_instead_of_aborting_the_retrain(self) -> None:
        """Intelligence is an enrichment; losing it must not cost the retrain."""
        fm = _fm(_TS)
        orch = _orch(_COVERAGE, _intel_frame(), raises=True)
        await orch._attach_intelligence_features(fm, Timeframe.INTRADAY)  # must not raise
        assert list(fm.features.columns) == list(BASE_FEATURE_COLUMNS)
        orch._log.warning.assert_called_once()


class TestCoverageGateStillApplies:
    def test_low_coverage_columns_are_excluded_from_the_active_set(self) -> None:
        """
        Attaching everything does not mean training on everything — sopr at
        20% coverage stays out.
        """
        active = get_active_feature_columns(coverage=_COVERAGE, min_coverage=0.6)
        assert "intelligence_binance_funding_rate_pct" in active
        assert "intelligence_sopr" not in active

    def test_no_coverage_yields_exactly_the_base_columns(self) -> None:
        """The behaviour that was silently universal before this fix."""
        assert get_active_feature_columns(coverage=None) == list(BASE_FEATURE_COLUMNS)


def test_the_training_path_actually_calls_it() -> None:
    """The defect was a read with no corresponding write."""
    import inspect

    from src.engine.orchestrator import Orchestrator

    source = inspect.getsource(Orchestrator._train_models)
    assert "self._attach_intelligence_features(" in source
