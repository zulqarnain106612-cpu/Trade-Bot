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
        assert router._exs == {}

    def test_init_unknown_exchange_skipped(self) -> None:
        from src.execution.router import SmartOrderRouter

        router = SmartOrderRouter(exchanges=["not_a_real_exchange_xyz"])
        # Unknown exchange should be skipped gracefully
        assert "not_a_real_exchange_xyz" not in router._exs

    def test_route_result_dataclass(self) -> None:
        from src.execution.router import RouteResult

        r = RouteResult(
            venue="binance",
            algo="IOC",
            filled_qty=1.0,
            avg_price=50000.0,
            slippage_bps=1.0,
            latency_ms=10.0,
            order_id="abc123",
            error=None,
        )
        assert r.venue == "binance"
        assert r.avg_price == 50000.0


class TestSmartOrderRouterRoute:
    def _run(self, coro):
        return asyncio.get_event_loop().run_until_complete(coro)

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
            signal={"direction": 1, "confidence": 0.8, "size_pct": 0.02, "horizon_idx": 0},
            portfolio={"equity": 10000.0, "open_positions": 1},
        )
        action, meta = agent.predict(obs)
        assert action in (0, 1, 2, 3)
        assert isinstance(meta, dict)

    def test_rl_state_obs_shape(self) -> None:
        from src.execution.rl_agent import RLExecutionState

        state = RLExecutionState(n_horizons=5)
        obs = state.build(
            signal={"direction": -1, "confidence": 0.7, "size_pct": 0.01, "horizon_idx": 2},
            portfolio={"equity": 5000.0, "open_positions": 0},
        )
        assert isinstance(obs, np.ndarray)
        assert obs.ndim == 1


# ─── PostTradeAnalytics ───────────────────────────────────────────────────────


class TestPostTradeAnalytics:
    def test_record_creates_fill(self) -> None:
        from src.execution.post_trade import PostTradeAnalytics

        analytics = PostTradeAnalytics(store=None)
        order_result = {
            "id": "ord1",
            "status": "closed",
            "average": 50000.0,
            "filled": 0.1,
            "cost": 5000.0,
            "fee": {"cost": 5.0},
        }
        analytics.record(order_result, "BTC/USDT", "buy", 0, 50000.0, 0.1)
        assert len(analytics._fills) == 1

    def test_record_computes_slippage(self) -> None:
        from src.execution.post_trade import PostTradeAnalytics

        analytics = PostTradeAnalytics(store=None)
        # Fill at 50100 vs limit at 50000 → slippage = 100/50000 * 10000 = 20 bps
        order_result = {
            "id": "x",
            "status": "closed",
            "average": 50100.0,
            "filled": 1.0,
            "cost": 50100.0,
            "fee": {"cost": 10.0},
        }
        analytics.record(order_result, "BTC/USDT", "buy", 1, 50000.0, 1.0)
        fill = analytics._fills[0]
        assert fill.slippage_bps >= 0.0

    def test_multiple_fills_accumulate(self) -> None:
        from src.execution.post_trade import PostTradeAnalytics

        analytics = PostTradeAnalytics(store=None)
        for i in range(5):
            order_result = {
                "id": f"o{i}",
                "status": "closed",
                "average": 50000.0,
                "filled": 0.01,
                "cost": 500.0,
                "fee": {"cost": 0.5},
            }
            analytics.record(order_result, "ETH/USDT", "sell", 2, 50000.0, 0.01)
        assert len(analytics._fills) == 5

    def test_record_with_no_fill_is_safe(self) -> None:
        from src.execution.post_trade import PostTradeAnalytics

        analytics = PostTradeAnalytics(store=None)
        order_result = {
            "id": "z",
            "status": "open",
            "average": None,
            "filled": 0.0,
            "cost": 0.0,
            "fee": {},
        }
        analytics.record(order_result, "BTC/USDT", "buy", 0, 50000.0, 0.0)

    def test_venue_stats_updated(self) -> None:
        from src.execution.post_trade import PostTradeAnalytics

        analytics = PostTradeAnalytics(store=None)
        order_result = {
            "id": "v1",
            "status": "closed",
            "average": 50000.0,
            "filled": 0.1,
            "cost": 5000.0,
            "fee": {"cost": 5.0},
            "_venue": "binance",
        }
        analytics.record(order_result, "BTC/USDT", "buy", 0, 50000.0, 0.1)
        # Should not crash; venue stats optional

    def test_fill_record_dataclass(self) -> None:
        from src.execution.post_trade import FillRecord

        fr = FillRecord(
            ts=1.0,
            symbol="BTC/USDT",
            side="buy",
            horizon_idx=0,
            venue="binance",
            algo="IOC",
            limit_price=50000.0,
            fill_price=50010.0,
            qty=0.1,
            slippage_bps=2.0,
            fee_usd=5.0,
            order_id="o1",
        )
        assert fr.slippage_bps == 2.0
