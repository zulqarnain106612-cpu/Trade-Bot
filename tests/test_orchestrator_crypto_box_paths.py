"""Crypto-Box augmentation and the surrounding error arms in Orchestrator._tick.

CRYPTO_BOX is off by default, so the whole augmentation block (Kelly scaling,
the manipulation circuit breaker, the audit-trail write and the fail-open
handler) never ran. A fake adapter switches it on without standing up the
18-engine stack; everything inside the block is the real code.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from test_orchestrator_coverage import _make_executor, _make_orch, _make_storage

from src.config import Timeframe
from src.engine.signal_engine import SignalResult
from src.engines.signal_gate import TradeSignal
from src.risk.kelly import KellyResult


def _kelly(adjusted: float = 0.04) -> KellyResult:
    return KellyResult(
        kelly_fraction=0.05,
        adjusted_fraction=adjusted,
        capital_usd=10_000.0,
        entry_price=42_000.0,
        quantity=0.01,
        notional_usd=420.0,
        is_capped=False,
    )


def _tradeable(direction: int = 1) -> SignalResult:
    return SignalResult(
        tradeable=True,
        direction=direction,
        p_long=0.75,
        p_bet=0.7,
        kelly_result=_kelly(),
        regime=None,
        gate_result=None,
        skip_reason=None,
    )


def _cb_signal(**overrides) -> TradeSignal:
    base = dict(
        symbol="BTC/USDT",
        direction=1,
        confidence=0.8,
        kelly_multiplier=0.5,
        regime="trending",
        ttl_hours=4,
        warnings=[],
    )
    return TradeSignal(**(base | overrides))


class _FakeCryptoBox:
    def __init__(self, signal, raises: Exception | None = None) -> None:
        self.enabled = True
        self._signal = signal
        self._raises = raises
        self.seen_data: dict | None = None

    async def get_signal(self, symbol: str, data: dict):
        self.seen_data = data
        if self._raises is not None:
            raise self._raises
        return self._signal


def _orch_with_crypto_box(signal, raises: Exception | None = None, result=None):
    orch = _make_orch()
    orch._executor = _make_executor()
    orch._executor.submit_signal = AsyncMock(return_value=("t1", "opened"))
    orch._executor.get_current_equity = AsyncMock(return_value=10_000.0)
    orch._executor.open_positions_safe = AsyncMock(return_value=[])
    orch._storage = _make_storage()
    orch._storage.latest_close = AsyncMock(return_value=None)
    orch._crypto_box = _FakeCryptoBox(signal, raises)

    engine = MagicMock()
    engine.tick = AsyncMock(return_value=result if result is not None else _tradeable())
    orch._engines = {Timeframe.INTRADAY.value: engine}
    orch._tick_counts = {Timeframe.INTRADAY.value: 0}
    orch._last_tick_ts = {Timeframe.INTRADAY.value: 0.0}
    return orch


async def _run_tick(orch) -> None:
    with (
        patch("src.engine.orchestrator.compute_win_loss_stats", return_value=(0, 0.0, 0.0, 0.0)),
        patch("src.engine.orchestrator.update_metrics"),
    ):
        await orch._tick(Timeframe.INTRADAY)


class TestCryptoBoxAugmentation:
    @pytest.mark.asyncio
    async def test_a_matching_direction_scales_kelly_by_the_multiplier(self):
        orch = _orch_with_crypto_box(_cb_signal(direction=1, kelly_multiplier=0.5))

        await _run_tick(orch)

        submitted = orch._executor.submit_signal.await_args.kwargs
        kelly = submitted.get("kelly_result") or orch._executor.submit_signal.await_args.args[0]
        assert kelly.adjusted_fraction == pytest.approx(0.02)  # 0.04 * 0.5

    @pytest.mark.asyncio
    async def test_a_neutral_crypto_box_direction_still_scales(self):
        orch = _orch_with_crypto_box(_cb_signal(direction=0, kelly_multiplier=0.25))

        await _run_tick(orch)

        kelly = orch._executor.submit_signal.await_args.kwargs["kelly_result"]
        assert kelly.adjusted_fraction == pytest.approx(0.01)

    @pytest.mark.asyncio
    async def test_a_conflicting_direction_halves_the_size(self):
        orch = _orch_with_crypto_box(_cb_signal(direction=-1, kelly_multiplier=0.9))

        await _run_tick(orch)

        kelly = orch._executor.submit_signal.await_args.kwargs["kelly_result"]
        assert kelly.adjusted_fraction == pytest.approx(0.02)  # halved, not 0.9x

    @pytest.mark.asyncio
    async def test_the_multiplier_can_never_raise_the_kelly_fraction(self):
        orch = _orch_with_crypto_box(_cb_signal(direction=1, kelly_multiplier=5.0))

        await _run_tick(orch)

        kelly = orch._executor.submit_signal.await_args.kwargs["kelly_result"]
        assert kelly.adjusted_fraction == pytest.approx(0.04)  # capped at the original

    @pytest.mark.asyncio
    async def test_the_manipulation_circuit_breaker_suppresses_the_trade(self):
        orch = _orch_with_crypto_box(
            _cb_signal(kelly_multiplier=1.0, warnings=["manipulation_circuit_breaker"])
        )

        await _run_tick(orch)

        orch._executor.submit_signal.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_zero_multiplier_leaves_the_size_untouched(self):
        orch = _orch_with_crypto_box(_cb_signal(kelly_multiplier=0.0))

        await _run_tick(orch)

        kelly = orch._executor.submit_signal.await_args.kwargs["kelly_result"]
        assert kelly.adjusted_fraction == pytest.approx(0.04)

    @pytest.mark.asyncio
    async def test_no_crypto_box_signal_leaves_the_result_alone(self):
        orch = _orch_with_crypto_box(None)

        await _run_tick(orch)

        kelly = orch._executor.submit_signal.await_args.kwargs["kelly_result"]
        assert kelly.adjusted_fraction == pytest.approx(0.04)

    @pytest.mark.asyncio
    async def test_the_engine_bars_and_cache_snapshot_are_passed_through(self):
        orch = _orch_with_crypto_box(_cb_signal())

        await _run_tick(orch)

        data = orch._crypto_box.seen_data
        assert data is not None
        assert data["spot"] > 0.0
        assert data["ohlcv"] is not None

    @pytest.mark.asyncio
    async def test_an_empty_bar_history_yields_a_zero_spot(self):
        orch = _orch_with_crypto_box(_cb_signal())
        orch._storage.fetch_bars = AsyncMock(return_value=[])

        await _run_tick(orch)

        assert orch._crypto_box.seen_data["ohlcv"] is None
        assert orch._crypto_box.seen_data["spot"] == 0.0

    @pytest.mark.asyncio
    async def test_augmentation_fails_open_when_the_adapter_raises(self):
        orch = _orch_with_crypto_box(None, raises=RuntimeError("engines down"))

        await _run_tick(orch)

        kelly = orch._executor.submit_signal.await_args.kwargs["kelly_result"]
        assert kelly.adjusted_fraction == pytest.approx(0.04)  # unmodified

    @pytest.mark.asyncio
    async def test_an_audit_trail_failure_does_not_lose_the_trade(self):
        orch = _orch_with_crypto_box(_cb_signal())

        with patch(
            "src.diagnostics.audit_trail.get_audit_trail",
            side_effect=RuntimeError("audit sink down"),
        ):
            await _run_tick(orch)

        orch._executor.submit_signal.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_the_signal_is_written_to_the_audit_trail(self):
        orch = _orch_with_crypto_box(_cb_signal(warnings=["thin_book"]))
        trail = MagicMock()

        with patch("src.diagnostics.audit_trail.get_audit_trail", return_value=trail):
            await _run_tick(orch)

        kwargs = trail.record.call_args.kwargs
        assert kwargs["event_type"] == "crypto_box_signal"
        assert kwargs["reason_code"] == "trending"
        assert kwargs["details"]["warnings"] == "thin_book"

    @pytest.mark.asyncio
    async def test_augmentation_is_skipped_when_there_is_no_kelly_result(self):
        skipped = SignalResult(
            tradeable=False,
            direction=0,
            p_long=0.3,
            p_bet=0.3,
            kelly_result=None,
            regime=None,
            gate_result=None,
            skip_reason="no_edge",
        )
        orch = _orch_with_crypto_box(_cb_signal(), result=skipped)

        await _run_tick(orch)

        assert orch._crypto_box.seen_data is None


def _with_agreement(orch, scalar: float) -> None:
    """Pin the portfolio agreement for the tick.

    _tick clears the previous tick's value before re-evaluating, so the scalar
    has to be installed from the evaluation itself rather than set beforehand.
    """

    async def _evaluate(tf):
        orch._last_portfolio_agreement[tf.value] = scalar
        return None

    orch._evaluate_strategy_portfolio = _evaluate


class TestPortfolioAgreementScalar:
    @pytest.mark.asyncio
    async def test_an_agreement_below_one_reduces_the_order(self):
        orch = _orch_with_crypto_box(None)
        orch._crypto_box.enabled = False
        _with_agreement(orch, 0.5)

        await _run_tick(orch)

        kelly = orch._executor.submit_signal.await_args.kwargs["kelly_result"]
        assert kelly.notional_usd < 420.0

    @pytest.mark.asyncio
    async def test_a_scalar_that_falls_below_the_exchange_minimum_skips_the_trade(self):
        orch = _orch_with_crypto_box(None)
        orch._crypto_box.enabled = False
        _with_agreement(orch, 0.5)

        with patch("src.engine.orchestrator.apply_size_scalar", return_value=None):
            await _run_tick(orch)

        orch._executor.submit_signal.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_failing_scalar_keeps_the_unreduced_order(self):
        orch = _orch_with_crypto_box(None)
        orch._crypto_box.enabled = False
        _with_agreement(orch, 0.5)

        with patch(
            "src.engine.orchestrator.apply_size_scalar",
            side_effect=RuntimeError("precision lookup failed"),
        ):
            await _run_tick(orch)

        kelly = orch._executor.submit_signal.await_args.kwargs["kelly_result"]
        assert kelly.notional_usd == pytest.approx(420.0)


class TestCryptoBoxProviderTasks:
    def test_provider_loops_are_spawned_for_every_provider(self):
        import asyncio

        async def _run():
            orch = _make_orch()
            tasks = orch._crypto_box_provider_tasks()
            for task in tasks:
                task.cancel()
            return tasks

        tasks = asyncio.run(_run())
        names = {t.get_name() for t in tasks}
        assert {
            "cb_sentiment_fg",
            "cb_sentiment_rss",
            "cb_macro",
            "cb_exchange_flows",
            "cb_block_height",
            "cb_deribit_BTC",
            "cb_deribit_ETH",
        } == names

    def test_a_provider_that_cannot_be_built_yields_no_tasks(self):
        import asyncio

        async def _run():
            orch = _make_orch()
            with patch(
                "src.data.sentiment_provider.SentimentProvider",
                side_effect=RuntimeError("no api key"),
            ):
                return orch._crypto_box_provider_tasks()

        assert asyncio.run(_run()) == []


class TestTickErrorArms:
    @pytest.mark.asyncio
    async def test_a_failing_strategy_correlation_scalar_keeps_the_asset_scalar(self):
        orch = _orch_with_crypto_box(None)
        orch._crypto_box.enabled = False
        orch._strategy_correlation_scalar = MagicMock(side_effect=RuntimeError("no peers"))

        await _run_tick(orch)

        orch._executor.submit_signal.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_a_failing_macro_budget_does_not_stop_the_tick(self):
        orch = _orch_with_crypto_box(None)
        orch._crypto_box.enabled = False
        orch._macro_exposure_budget = AsyncMock(side_effect=RuntimeError("macro feed down"))

        await _run_tick(orch)

        orch._executor.submit_signal.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_a_failing_ensemble_field_write_does_not_lose_the_trade(self):
        result = _tradeable()
        result = type(result)(
            **{
                **{f: getattr(result, f) for f in result.__dataclass_fields__},
                "ensemble_point_estimate": 0.61,
                "ensemble_blend_weight": 0.3,
            }
        )
        orch = _orch_with_crypto_box(None, result=result)
        orch._crypto_box.enabled = False
        orch._storage.update_trade_ensemble_fields = AsyncMock(
            side_effect=RuntimeError("column missing")
        )

        await _run_tick(orch)

        orch._storage.update_trade_ensemble_fields.assert_awaited_once()


def test_blend_audit_is_none_when_the_blend_inputs_are_incomplete():
    from src.engine.orchestrator import _blend_audit

    partial = SignalResult(
        tradeable=True,
        direction=1,
        p_long=0.6,
        p_bet=0.55,
        kelly_result=None,
        regime=None,
        gate_result=None,
        skip_reason=None,
        pre_blend_p_long=0.58,
        ensemble_p_long=None,  # the blend never completed
        ensemble_blend_weight=0.4,
    )

    assert _blend_audit(partial) is None
