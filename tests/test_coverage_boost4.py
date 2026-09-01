"""Coverage boost 4: ensemble_predictor, live.py place paths, api/main remaining."""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pandas as pd
import pytest

from src.execution.live import LiveExecutor
from src.execution.order_manager import OrderManager

# ---------------------------------------------------------------------------
# EnsemblePredictor — model fit/predict paths
# ---------------------------------------------------------------------------
from src.intelligence.ensemble_predictor import (
    ARIMAPredictor,
    EnsemblePrediction,
    EnsemblePredictor,
    GaussianProcessPredictor,
    TreeEnsemblePredictor,
    XGBoostPredictor,
)
from src.risk.gates import DrawdownTracker


def _make_X(n=30, f=5) -> pd.DataFrame:
    rng = np.random.default_rng(42)
    return pd.DataFrame(rng.standard_normal((n, f)), columns=[f"f{i}" for i in range(f)])


def _make_y(n=30) -> pd.Series:
    rng = np.random.default_rng(42)
    return pd.Series(rng.standard_normal(n))


class TestARIMAPredictor:
    def test_predict_without_fit_returns_zero(self):
        p = ARIMAPredictor()
        result = p.predict(pd.DataFrame())
        assert result == 0.0

    def test_predict_with_uncertainty_without_fit(self):
        p = ARIMAPredictor()
        point, unc = p.predict_with_uncertainty(pd.DataFrame())
        assert point == 0.0
        assert isinstance(unc, float)

    def test_get_performance_metrics(self):
        p = ARIMAPredictor()
        m = p.get_performance_metrics()
        assert m["model_type"] == "ARIMA"

    def test_fit_importerror_handled(self):
        p = ARIMAPredictor()
        with patch.dict(
            "sys.modules",
            {
                "statsmodels": None,
                "statsmodels.tsa": None,
                "statsmodels.tsa.arima": None,
                "statsmodels.tsa.arima.model": None,
            },
        ):
            p.fit(pd.Series([1.0, 2.0, 3.0]))
        assert p.model is None


class TestXGBoostPredictor:
    def test_predict_without_fit_returns_zero(self):
        p = XGBoostPredictor()
        result = p.predict(_make_X(5))
        assert result == 0.0

    def test_fit_and_predict(self):
        p = XGBoostPredictor()
        X, y = _make_X(), _make_y()
        p.fit(X, y)
        if p.model is not None:
            result = p.predict(X.head(1))
            assert isinstance(result, float)

    def test_predict_with_uncertainty(self):
        p = XGBoostPredictor()
        point, unc = p.predict_with_uncertainty(_make_X(5))
        assert point == 0.0
        assert isinstance(unc, float)

    def test_get_metrics(self):
        p = XGBoostPredictor()
        m = p.get_performance_metrics()
        assert m["model_type"] == "XGBoost"


class TestGaussianProcessPredictor:
    def test_predict_without_fit_returns_zero(self):
        p = GaussianProcessPredictor()
        result = p.predict(_make_X(3))
        assert result == 0.0

    def test_predict_with_uncertainty_without_fit(self):
        p = GaussianProcessPredictor()
        point, unc = p.predict_with_uncertainty(_make_X(3))
        assert point == 0.0
        assert isinstance(unc, float)

    def test_fit_with_insufficient_data(self):
        p = GaussianProcessPredictor()
        X, y = _make_X(3), _make_y(3)
        p.fit(X, y)
        assert p.model is None

    def test_fit_and_predict(self):
        p = GaussianProcessPredictor(n_restarts_optimizer=0)
        X, y = _make_X(20), _make_y(20)
        p.fit(X, y)
        if p.model is not None:
            result = p.predict(X.head(1))
            assert isinstance(result, float)

    def test_get_metrics(self):
        p = GaussianProcessPredictor()
        m = p.get_performance_metrics()
        assert m["model_type"] == "GaussianProcess"


class TestTreeEnsemblePredictor:
    def test_predict_without_fit_returns_zero(self):
        p = TreeEnsemblePredictor()
        result = p.predict(_make_X(5))
        assert result == 0.0

    def test_fit_and_predict(self):
        p = TreeEnsemblePredictor(n_estimators=5, n_bootstrap=2)
        X, y = _make_X(), _make_y()
        p.fit(X, y)
        result = p.predict(X.head(1))
        assert isinstance(result, float)

    def test_predict_with_uncertainty_with_bootstrap(self):
        p = TreeEnsemblePredictor(n_estimators=5, n_bootstrap=3)
        X, y = _make_X(), _make_y()
        p.fit(X, y)
        point, unc = p.predict_with_uncertainty(X.head(1))
        assert isinstance(point, float)
        assert isinstance(unc, float)

    def test_predict_with_uncertainty_no_bootstrap(self):
        p = TreeEnsemblePredictor(n_estimators=5, n_bootstrap=0)
        X, y = _make_X(), _make_y()
        p.fit(X, y)
        point, _unc = p.predict_with_uncertainty(X.head(1))
        assert isinstance(point, float)

    def test_get_metrics(self):
        p = TreeEnsemblePredictor()
        m = p.get_performance_metrics()
        assert m["model_type"] == "TreeEnsemble(GBM+bootstrap)"


class TestEnsemblePredictor:
    def test_init_with_default_models(self):
        ep = EnsemblePredictor()
        assert "arima" in ep.models
        assert "xgboost" in ep.models

    def test_fit_calls_all_models(self):
        """
        The name promises fit reached the members; assert it, rather than
        only that nothing raised. _feature_cols is None until fit() records
        the column order, and the weights are recomputed from each member's
        realised RMSE, so both being populated is evidence the members ran.
        """
        ep = EnsemblePredictor()
        X, y = _make_X(), _make_y()
        assert ep._feature_cols is None

        ep.fit(X, y)

        assert ep._feature_cols == list(X.columns)
        assert set(ep.weights) == set(ep.models)
        assert ep.weights and all(w >= 0.0 for w in ep.weights.values())

    def test_predict_returns_ensemble_prediction(self):
        ep = EnsemblePredictor()
        X, y = _make_X(), _make_y()
        ep.fit(X, y)
        result = ep.predict(X.head(1))
        assert isinstance(result, EnsemblePrediction)
        assert isinstance(result.point_estimate, float)

    def test_predict_without_fit_refuses(self):
        # No member is fitted, so none has an opinion. A 0.0 point estimate
        # here would be a maximally confident forecast of exactly zero.
        ep = EnsemblePredictor()
        with pytest.raises(RuntimeError, match="every ensemble member failed"):
            ep.predict(_make_X(1))

    def test_predict_with_member_exception_excludes_that_member(self):
        ep = EnsemblePredictor()
        ep.fit(_make_X(), _make_y())
        ep.models["xgboost"].predict_with_uncertainty = MagicMock(side_effect=RuntimeError("fail"))
        result = ep.predict(_make_X(1))
        assert isinstance(result, EnsemblePrediction)
        assert "xgboost" not in result.individual_predictions


# ---------------------------------------------------------------------------
# LiveExecutor — _place_and_record and _place_market_order paths
# ---------------------------------------------------------------------------


def _make_executor(starting_capital: float = 100_000.0, cash: float | None = None) -> LiveExecutor:
    import structlog

    ex = object.__new__(LiveExecutor)
    ex._starting_capital = starting_capital
    ex._cash = cash if cash is not None else starting_capital
    ex._peak_equity = starting_capital
    ex._positions = {}
    ex._approval_queue = {}
    ex._lock = asyncio.Lock()
    ex._trade_semaphore = asyncio.Semaphore(1)
    ex._drawdown_tracker = DrawdownTracker(starting_capital)
    ex._order_manager = OrderManager()
    ex._order_fsm_registry = OrderedDict()
    ex._initialized = True
    ex._storage = AsyncMock()
    ex._fetcher = MagicMock()
    # v8: initialize() reconciles against exchange truth; an unavailable
    # snapshot blocks new entries, so give it an explicit empty book.
    ex._fetcher.fetch_exchange_holdings = AsyncMock(return_value={})
    ex._recovery_discrepancies = []
    ex._cfg = MagicMock()
    ex._risk_cfg = MagicMock(notional_limit_usd=10_000.0, approval_timeout_s=30.0)
    ex._log = structlog.get_logger().bind(component="test")
    return ex


class TestLiveExecutorPlacePaths:
    @pytest.mark.asyncio
    async def test_get_order_fsm_state_not_found(self):
        ex = _make_executor()
        result = await ex.get_order_fsm_state("nonexistent-order")
        assert result is None

    @pytest.mark.asyncio
    async def test_get_order_fsm_state_found(self):
        ex = _make_executor()
        fsm_mock = MagicMock()
        ex._order_fsm_registry["order-1"] = fsm_mock
        result = await ex.get_order_fsm_state("order-1")
        assert result is fsm_mock

    @pytest.mark.asyncio
    async def test_equity_usd_with_positions(self):
        from src.execution.live import LivePosition

        ex = _make_executor(cash=90_000.0)
        pos = LivePosition(
            trade_id="t1",
            exchange_order_id="ord-1",
            symbol="BTC/USDT",
            timeframe="15m",
            direction=1,
            entry_price=50_000.0,
            quantity=0.1,
            notional_usd=5_000.0,
            entry_ts=int(time.time() * 1000),
            kelly_fraction=0.02,
            regime_at_entry=1,
            meta_label_prob=0.6,
            raw_signal=0.7,
            approved_by="auto",
            execution_mode="automatic",
            fee_usd=1.0,
        )
        pos.mark(51_000.0)
        ex._positions["t1"] = pos
        equity = ex.equity_usd
        assert equity > 90_000.0

    @pytest.mark.asyncio
    async def test_open_positions_safe(self):
        ex = _make_executor()
        result = await ex.open_positions_safe()
        assert result == []

    @pytest.mark.asyncio
    async def test_pending_approvals_safe(self):
        ex = _make_executor()
        result = await ex.pending_approvals_safe()
        assert result == []

    @pytest.mark.asyncio
    async def test_get_consecutive_losses(self):
        ex = _make_executor()
        ex._storage.count_consecutive_losses = AsyncMock(return_value=2)
        result = await ex.get_consecutive_losses("BTC/USDT")
        assert isinstance(result, int)

    @pytest.mark.asyncio
    async def test_get_daily_pnl(self):
        ex = _make_executor()
        ex._storage.daily_pnl = AsyncMock(return_value=500.0)
        result = await ex.get_daily_pnl("BTC/USDT")
        assert result == pytest.approx(500.0)
