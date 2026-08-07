"""Tests for SmartOrderRouter, RLExecutionAgent, PostTradeAnalytics."""

from __future__ import annotations

import asyncio

import numpy as np


# ─── SmartOrderRouter ─────────────────────────────────────────────────────────


class TestSelectAlgo:
    def test_short_horizon_low_impact_returns_ioc(self) -> None:
        from src.execution.router import SmartOrderRouter

        router = SmartOrderRouter(exchanges=[])
        algo = router._select_algo(horizon_idx=0, kyle_lambda=0.001, size_usd=10.0)
        assert algo == "IOC"

    def test_medium_horizon_returns_iceberg(self) -> None:
        from src.execution.router import SmartOrderRouter

        router = SmartOrderRouter(exchanges=[])
        algo = router._select_algo(horizon_idx=2, kyle_lambda=0.001, size_usd=15.0)
        assert algo == "iceberg"

    def test_large_impact_returns_twap(self) -> None:
        from src.execution.router import SmartOrderRouter

        router = SmartOrderRouter(exchanges=[])
        algo = router._select_algo(horizon_idx=0, kyle_lambda=1.0, size_usd=100.0)
        assert algo == "TWAP"

    def test_long_horizon_returns_twap(self) -> None:
        from src.execution.router import SmartOrderRouter

        router = SmartOrderRouter(exchanges=[])
        algo = router._select_algo(horizon_idx=7, kyle_lambda=0.001, size_usd=5.0)
        assert algo == "TWAP"


class TestSmartOrderRouterInit:
    def test_init_empty_exchanges(self) -> None:
        from src.execution.router import SmartOrderRouter

        router = SmartOrderRouter(exchanges=[])
        assert router._exchanges == {}

    def test_init_unknown_exchange_skipped(self) -> None:
        from src.execution.router import SmartOrderRouter

        router = SmartOrderRouter(exchanges=["not_a_real_exchange_xyz"])
        # Unknown exchange should be skipped gracefully
        assert "not_a_real_exchange_xyz" not in router._exchanges

    def test_route_result_dataclass(self) -> None:
        from src.execution.router import RouteResult

        r = RouteResult(
            venue="binance",
            algo="IOC",
            filled_qty=1.0,
            avg_price=50000.0,
            slippage_bps=1.0,
            fee_usd=5.0,
            order_id="abc123",
            success=True,
            error=None,
        )
        assert r.venue == "binance"
        assert r.avg_price == 50000.0


class TestSmartOrderRouterRoute:
    def _run(self, coro):
        return asyncio.run(coro)

    def test_route_no_exchanges_returns_error_result(self) -> None:
        from src.execution.router import SmartOrderRouter

        router = SmartOrderRouter(exchanges=[])
        signal = {
            "symbol": "BTC/USDT",
            "side": "buy",
            "price": 50000.0,
            "horizon": 0,
            "horizon_seconds": 30,
            "confidence": 0.8,
        }
        result = self._run(router.route(signal, kyle_lambda=0.001, size_usd=100.0))
        assert hasattr(result, "error")


# ─── RLExecutionAgent ─────────────────────────────────────────────────────────


class TestRLExecutionAgent:
    def test_init_no_model_file(self, tmp_path) -> None:
        from src.execution.rl_agent import RLExecutionAgent

        agent = RLExecutionAgent(model_path=tmp_path / "no_model.zip")
        assert agent is not None

    def test_predict_returns_valid_output(self, tmp_path) -> None:
        from src.execution.rl_agent import RLExecutionAgent, RLExecutionState

        agent = RLExecutionAgent(model_path=tmp_path / "no_model.zip")
        state = RLExecutionState(n_horizons=3)
        obs = state.build(
            horizon_confidences=[0.7, 0.6],
            regime_id=1,
            ecc_features={},
            realized_pnl=0.0,
            drawdown=0.0,
            kyle_lambda=1e-6,
        )
        action, meta = agent.predict(obs)
        assert action in (0, 1, 2, 3)
        assert isinstance(meta, dict)

    def test_rl_state_obs_shape(self) -> None:
        from src.execution.rl_agent import RLExecutionState

        state = RLExecutionState(n_horizons=5)
        obs = state.build(
            horizon_confidences=[0.7, 0.6],
            regime_id=1,
            ecc_features={},
            realized_pnl=0.0,
            drawdown=0.0,
            kyle_lambda=1e-6,
        )
        assert isinstance(obs, np.ndarray)
        assert obs.ndim == 1


# ─── PostTradeAnalytics ───────────────────────────────────────────────────────


def _make_route_result(
    avg_price=50000.0,
    filled_qty=0.1,
    slippage_bps=2.0,
    fee_usd=5.0,
    venue="binance",
    algo="IOC",
    error=None,
):
    from src.execution.router import RouteResult

    return RouteResult(
        venue=venue,
        algo=algo,
        filled_qty=filled_qty,
        avg_price=avg_price,
        slippage_bps=slippage_bps,
        order_id="ord1",
        fee_usd=fee_usd,
        success=(error is None),
        error=error,
    )


class TestPostTradeAnalytics:
    def test_record_creates_fill(self) -> None:
        from src.execution.post_trade import PostTradeAnalytics

        analytics = PostTradeAnalytics(store=None)
        result = _make_route_result()
        fill = analytics.record(result, "BTC/USDT", "buy", 0, 50000.0, 0.1)
        assert fill is not None
        assert len(analytics._fill_history) == 1

    def test_record_computes_slippage(self) -> None:
        from src.execution.post_trade import PostTradeAnalytics

        analytics = PostTradeAnalytics(store=None)
        result = _make_route_result(avg_price=50100.0, slippage_bps=20.0)
        fill = analytics.record(result, "BTC/USDT", "buy", 1, 50000.0, 1.0)
        assert fill.slippage_bps >= 0.0

    def test_multiple_fills_accumulate(self) -> None:
        from src.execution.post_trade import PostTradeAnalytics

        analytics = PostTradeAnalytics(store=None)
        for _ in range(5):
            result = _make_route_result(filled_qty=0.01, fee_usd=0.5)
            analytics.record(result, "ETH/USDT", "sell", 2, 50000.0, 0.01)
        assert len(analytics._fill_history) == 5

    def test_venue_stats_updated(self) -> None:
        from src.execution.post_trade import PostTradeAnalytics

        analytics = PostTradeAnalytics(store=None)
        result = _make_route_result(venue="binance")
        analytics.record(result, "BTC/USDT", "buy", 0, 50000.0, 0.1)
        assert "binance" in analytics._venue_stats

    def test_fill_record_fields(self) -> None:
        from src.execution.post_trade import PostTradeAnalytics

        analytics = PostTradeAnalytics(store=None)
        result = _make_route_result(avg_price=50010.0, slippage_bps=2.0, fee_usd=5.0)
        fill = analytics.record(result, "BTC/USDT", "buy", 0, 50000.0, 0.1)
        assert fill.slippage_bps == 2.0
        assert fill.symbol == "BTC/USDT"
        assert fill.side == "buy"

    def test_execution_quality_score_in_range(self) -> None:
        from src.execution.post_trade import PostTradeAnalytics

        analytics = PostTradeAnalytics(store=None)
        result = _make_route_result()
        fill = analytics.record(result, "BTC/USDT", "buy", 0, 50000.0, 0.1)
        assert 0.0 <= fill.execution_quality_score <= 1.0
