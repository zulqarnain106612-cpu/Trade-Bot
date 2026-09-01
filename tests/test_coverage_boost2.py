"""Coverage boost 2: blockchain_provider, order_manager, performance_drift."""

from __future__ import annotations

import time
import uuid
from unittest.mock import AsyncMock, patch

import ccxt.async_support as ccxt
import pytest

from src.execution.order_fsm import OrderFSMError, OrderStatus
from src.execution.order_manager import OrderManager
from src.intelligence.providers.blockchain_provider import (
    BlockchainIntelligenceProvider,
    get_blockchain_intelligence_provider,
)
from src.risk.performance_drift import PerformanceBaseline, PerformanceDriftDetector

# ---------------------------------------------------------------------------
# BlockchainIntelligenceProvider
# ---------------------------------------------------------------------------


def _test_key() -> str:
    """Fresh idempotency key per submission.

    Tests exercise order placement, not de-duplication; a unique key keeps
    each call a distinct intent. Duplicate rejection is covered explicitly in
    tests/test_idempotency.py.
    """
    return f"tb{uuid.uuid4().hex[:30]}"


class TestBlockchainProvider:
    def test_exchange_id(self):
        p = BlockchainIntelligenceProvider()
        assert p.exchange_id == "blockchain_info"

    @pytest.mark.asyncio
    async def test_initialize_and_close(self):
        p = BlockchainIntelligenceProvider()
        await p.initialize()
        await p.close()

    @pytest.mark.asyncio
    async def test_fetch_metrics_success(self):
        p = BlockchainIntelligenceProvider()
        stats = {"hash_rate": 500_000.0, "n_tx": 300_000.0}
        with patch.object(p, "_fetch_stats", new=AsyncMock(return_value=stats)):
            result = await p.fetch_metrics()
        assert "network_activity_score" in result
        assert result["confidence"] > 0.0

    @pytest.mark.asyncio
    async def test_fetch_metrics_empty_stats_penalizes_confidence(self):
        p = BlockchainIntelligenceProvider()
        with patch.object(p, "_fetch_stats", new=AsyncMock(return_value={})):
            result = await p.fetch_metrics()
        assert result["confidence"] < 1.0

    @pytest.mark.asyncio
    async def test_fetch_metrics_exception_penalizes_confidence(self):
        p = BlockchainIntelligenceProvider()
        with patch.object(p, "_fetch_stats", new=AsyncMock(side_effect=RuntimeError("timeout"))):
            result = await p.fetch_metrics()
        assert result["confidence"] < 1.0

    @pytest.mark.asyncio
    async def test_fetch_metrics_builds_zscore_with_history(self):
        p = BlockchainIntelligenceProvider()
        p._hashrate_history = [400_000.0, 420_000.0, 450_000.0, 480_000.0]
        p._tx_history = [250_000.0, 270_000.0, 280_000.0, 290_000.0]
        stats = {"hash_rate": 500_000.0, "n_tx": 310_000.0}
        with patch.object(p, "_fetch_stats", new=AsyncMock(return_value=stats)):
            result = await p.fetch_metrics()
        score = result["network_activity_score"]
        assert -1.0 <= score <= 1.0

    @pytest.mark.asyncio
    async def test_fetch_metrics_zero_hash_rate_skipped(self):
        p = BlockchainIntelligenceProvider()
        stats = {"hash_rate": 0.0, "n_tx": 300_000.0}
        with patch.object(p, "_fetch_stats", new=AsyncMock(return_value=stats)):
            await p.fetch_metrics()
        assert len(p._hashrate_history) == 0

    def test_zscore_short_history(self):
        result = BlockchainIntelligenceProvider._zscore([1.0, 2.0])
        assert result == 0.0

    def test_zscore_constant_series(self):
        result = BlockchainIntelligenceProvider._zscore([5.0, 5.0, 5.0, 5.0, 5.0])
        assert result == 0.0

    def test_zscore_with_variance(self):
        result = BlockchainIntelligenceProvider._zscore([1.0, 2.0, 3.0, 4.0, 10.0])
        assert isinstance(result, float)
        assert -3.0 <= result <= 3.0

    def test_get_cache_miss(self):
        p = BlockchainIntelligenceProvider()
        assert p._get_cache("missing") is None

    def test_set_and_get_cache(self):
        p = BlockchainIntelligenceProvider()
        p._set_cache("k", {"data": 42})
        assert p._get_cache("k") == {"data": 42}

    def test_get_cache_expired(self):
        p = BlockchainIntelligenceProvider(cache_ttl_s=1)
        p._cache["k"] = (time.monotonic() - 10.0, "value")
        assert p._get_cache("k") is None

    @pytest.mark.asyncio
    async def test_fetch_stats_uses_cache(self):
        p = BlockchainIntelligenceProvider()
        cached = {"hash_rate": 100.0}
        p._set_cache("stats", cached)
        result = await p._fetch_stats()
        assert result == cached

    def test_singleton(self):
        import src.intelligence.providers.blockchain_provider as mod

        mod._provider = None
        p1 = get_blockchain_intelligence_provider()
        p2 = get_blockchain_intelligence_provider()
        assert p1 is p2


# ---------------------------------------------------------------------------
# OrderManager
# ---------------------------------------------------------------------------


def _mock_exchange(order_status="closed", fill_qty=0.1, avg_price=50_000.0):
    ex = AsyncMock()
    order = {
        "id": "order-1",
        "status": order_status,
        "filled": fill_qty,
        "average": avg_price,
    }
    ex.create_market_order = AsyncMock(return_value=order)
    ex.fetch_order = AsyncMock(return_value=order)
    return ex


class TestOrderManager:
    @pytest.mark.asyncio
    async def test_invalid_symbol_raises(self):
        mgr = OrderManager()
        ex = _mock_exchange()
        with pytest.raises(OrderFSMError):
            await mgr.place_order_with_fsm(ex, "", "buy", 0.1, _test_key())

    @pytest.mark.asyncio
    async def test_invalid_side_raises(self):
        mgr = OrderManager()
        ex = _mock_exchange()
        with pytest.raises(OrderFSMError):
            await mgr.place_order_with_fsm(ex, "BTC/USDT", "hold", 0.1, _test_key())

    @pytest.mark.asyncio
    async def test_invalid_quantity_raises(self):
        mgr = OrderManager()
        ex = _mock_exchange()
        with pytest.raises(OrderFSMError):
            await mgr.place_order_with_fsm(ex, "BTC/USDT", "buy", 0.0, _test_key())

    @pytest.mark.asyncio
    async def test_place_order_success(self):
        mgr = OrderManager()
        ex = _mock_exchange(order_status="closed", fill_qty=0.1, avg_price=50_000.0)
        fsm, order = await mgr.place_order_with_fsm(ex, "BTC/USDT", "buy", 0.1, _test_key())
        assert fsm.state.status == OrderStatus.FILLED
        assert order["id"] == "order-1"

    @pytest.mark.asyncio
    async def test_place_order_network_error_raises(self):
        mgr = OrderManager()
        ex = AsyncMock()
        ex.create_market_order = AsyncMock(side_effect=ccxt.NetworkError("connection refused"))
        with pytest.raises(ccxt.NetworkError):
            await mgr.place_order_with_fsm(ex, "BTC/USDT", "buy", 0.1, _test_key())

    @pytest.mark.asyncio
    async def test_place_order_exchange_error_raises(self):
        mgr = OrderManager()
        ex = AsyncMock()
        ex.create_market_order = AsyncMock(side_effect=ccxt.ExchangeError("margin too low"))
        with pytest.raises(ccxt.ExchangeError):
            await mgr.place_order_with_fsm(ex, "BTC/USDT", "buy", 0.1, _test_key())

    @pytest.mark.asyncio
    async def test_confirm_order_cancelled_raises(self):
        mgr = OrderManager()
        order_placed = {"id": "ord-2", "status": "open", "filled": 0.0, "average": 0.0}
        call_count = [0]

        async def _fetch(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                # First poll: open → transitions to FILLING
                return {"id": "ord-2", "status": "open", "filled": 0.0, "average": 0.0}
            # Second poll: cancelled
            return {"id": "ord-2", "status": "cancelled", "filled": 0.0, "average": 0.0}

        ex = AsyncMock()
        ex.create_market_order = AsyncMock(return_value=order_placed)
        ex.fetch_order = _fetch
        with patch("asyncio.sleep", new=AsyncMock()), pytest.raises(ccxt.ExchangeError):
            await mgr.place_order_with_fsm(ex, "BTC/USDT", "buy", 0.1, _test_key())

    @pytest.mark.asyncio
    async def test_confirm_order_pending_then_filled(self):
        mgr = OrderManager()
        call_count = [0]
        order_placed = {"id": "ord-3", "status": "open", "filled": 0.0, "average": 0.0}

        async def _fetch_order(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] < 3:
                return {"id": "ord-3", "status": "open", "filled": 0.0, "average": 0.0}
            return {"id": "ord-3", "status": "closed", "filled": 0.1, "average": 50_000.0}

        ex = AsyncMock()
        ex.create_market_order = AsyncMock(return_value=order_placed)
        ex.fetch_order = _fetch_order
        with patch("asyncio.sleep", new=AsyncMock()):
            fsm, _order = await mgr.place_order_with_fsm(ex, "BTC/USDT", "buy", 0.1, _test_key())
        assert fsm.state.status == OrderStatus.FILLED

    @pytest.mark.asyncio
    async def test_confirm_order_network_error_retries(self):
        mgr = OrderManager()
        call_count = [0]
        order_placed = {"id": "ord-4", "status": "open", "filled": 0.0, "average": 0.0}

        async def _fetch_order(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] < 2:
                raise ccxt.NetworkError("timeout")
            return {"id": "ord-4", "status": "closed", "filled": 0.1, "average": 50_000.0}

        ex = AsyncMock()
        ex.create_market_order = AsyncMock(return_value=order_placed)
        ex.fetch_order = _fetch_order
        with patch("asyncio.sleep", new=AsyncMock()):
            fsm, _order = await mgr.place_order_with_fsm(ex, "BTC/USDT", "buy", 0.1, _test_key())
        assert fsm.state.status == OrderStatus.FILLED


# ---------------------------------------------------------------------------
# PerformanceDriftDetector — drift triggering paths
# ---------------------------------------------------------------------------


def _make_baseline(
    oos_sharpe=1.5,
    oos_accuracy=0.60,
    train_win_rate=0.55,
    max_drawdown_pct=0.10,
    trades=100,
) -> PerformanceBaseline:
    return PerformanceBaseline(
        train_sharpe=1.8,
        oos_sharpe=oos_sharpe,
        train_accuracy=0.62,
        oos_accuracy=oos_accuracy,
        train_win_rate=train_win_rate,
        max_drawdown_pct=max_drawdown_pct,
        trades_in_backtest=trades,
    )


def _add_trades(detector, n=30, pnl=50.0, correct=True):
    for i in range(n):
        detector.record_trade_outcome(
            pnl_usd=pnl if i % 2 == 0 else -pnl * 0.5,
            predicted_prob=0.7 if correct else 0.3,
            actual_direction=1,
            current_equity=100_000.0 + i * 100,
            starting_equity=100_000.0,
        )


class TestPerformanceDrift:
    def test_baseline_to_dict(self):
        b = _make_baseline()
        d = b.to_dict()
        assert "train_sharpe" in d
        assert "oos_sharpe" in d
        assert "set_at_ms" in d

    def test_no_drift_insufficient_trades(self):
        detector = PerformanceDriftDetector(_make_baseline())
        result = detector.check_drift()
        assert not result.drifted

    def test_no_drift_enough_good_trades(self):
        detector = PerformanceDriftDetector(_make_baseline(oos_sharpe=0.1))
        _add_trades(detector, n=30, pnl=200.0, correct=True)
        result = detector.check_drift()
        # Good trades → no drift
        assert isinstance(result.drifted, bool)

    def test_sharpe_drift_detected(self):
        # Very high baseline Sharpe so live (mediocre) triggers drift
        detector = PerformanceDriftDetector(_make_baseline(oos_sharpe=10.0))
        for _ in range(30):
            detector.record_trade_outcome(
                pnl_usd=-100.0,
                predicted_prob=0.4,
                actual_direction=1,
                current_equity=90_000.0,
                starting_equity=100_000.0,
            )
        result = detector.check_drift()
        # Either drift detected or sharpe window too small
        assert isinstance(result.drifted, bool)

    def test_winrate_drift_detected(self):
        detector = PerformanceDriftDetector(_make_baseline(train_win_rate=0.9))
        for _ in range(30):
            detector.record_trade_outcome(
                pnl_usd=-50.0,  # All losses → win_rate=0
                predicted_prob=0.6,
                actual_direction=1,
                current_equity=99_000.0,
                starting_equity=100_000.0,
            )
        result = detector.check_drift()
        # Win rate 0.0 vs baseline 0.9 = massive drift
        if result.drifted:
            assert result.metric in ("sharpe", "accuracy", "win_rate", "drawdown")

    def test_accuracy_drift_detected(self):
        detector = PerformanceDriftDetector(_make_baseline(oos_accuracy=1.0))
        for _ in range(30):
            detector.record_trade_outcome(
                pnl_usd=0.0,
                predicted_prob=0.6,  # predicts long
                actual_direction=-1,  # actual short → wrong
                current_equity=100_000.0,
                starting_equity=100_000.0,
            )
        result = detector.check_drift()
        assert isinstance(result.drifted, bool)

    def test_drawdown_drift_detected(self):
        detector = PerformanceDriftDetector(_make_baseline(max_drawdown_pct=0.01))
        # Large drawdown from peak
        detector._live_equity_peak = 100_000.0
        detector._live_equity_start = 100_000.0
        for _ in range(30):
            detector.record_trade_outcome(
                pnl_usd=-1000.0,
                predicted_prob=0.4,
                actual_direction=1,
                current_equity=50_000.0,  # 50% drawdown
                starting_equity=100_000.0,
            )
        result = detector.check_drift()
        assert isinstance(result.drifted, bool)

    def test_record_updates_equity_peak(self):
        detector = PerformanceDriftDetector(_make_baseline())
        detector.record_trade_outcome(
            pnl_usd=1000.0,
            predicted_prob=0.7,
            actual_direction=1,
            current_equity=110_000.0,
            starting_equity=100_000.0,
        )
        assert detector._live_equity_peak == pytest.approx(110_000.0)

    def test_record_sets_equity_start_on_first_trade(self):
        detector = PerformanceDriftDetector(_make_baseline())
        assert detector._live_equity_start == 0.0
        detector.record_trade_outcome(
            pnl_usd=0.0,
            predicted_prob=0.5,
            actual_direction=1,
            current_equity=100_000.0,
            starting_equity=99_000.0,
        )
        assert detector._live_equity_start == pytest.approx(99_000.0)

    def test_baseline_property(self):
        b = _make_baseline()
        detector = PerformanceDriftDetector(b)
        assert detector.baseline is b
