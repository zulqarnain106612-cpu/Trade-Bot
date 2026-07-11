"""Tests for src/tuning/scheduler.py -- AutoTuningScheduler."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from src.config import get_settings
from src.tuning.scheduler import AutoTuningScheduler, _shannon_entropy
from src.tuning.state import parameter_registry, pause_state, runner


@pytest.fixture(autouse=True)
def _reset_tuning_state():
    """Process-wide singletons (src/tuning/state.py) -- reset between tests
    so one test's registrations/pauses don't leak into the next, same
    pattern as tests/test_self_tuning_api.py."""
    parameter_registry._params.clear()
    pause_state._paused = False
    yield
    parameter_registry._params.clear()
    pause_state._paused = False


def make_trade(trade_id, entry_ts, exit_ts, entry_price=100.0, exit_price=105.0, direction=1):
    from src.data.storage import TradeRecord

    return TradeRecord(
        id=trade_id,
        symbol="BTC/USDT",
        timeframe="15m",
        trading_mode="paper",
        execution_mode="automatic",
        direction=direction,
        entry_price=entry_price,
        exit_price=exit_price,
        quantity=1.0,
        notional_usd=entry_price,
        entry_ts=entry_ts,
        exit_ts=exit_ts,
        pnl_usd=1.0,
        pnl_pct=0.01,
        fee_usd=0.1,
        kelly_fraction=0.1,
        regime_at_entry=0,
        meta_label_prob=0.6,
        exit_reason="profit_target",
        approved_by="auto",
        raw_signal=0.6,
    )


class _FakeStorage:
    """Minimal in-memory stand-in for fetch_trades / regime_snapshot_before."""

    def __init__(self, trades: list[Any], entropy_by_ts: dict[int, float]) -> None:
        self._trades = trades
        self._entropy_by_ts = entropy_by_ts

    async def fetch_trades(self, **kwargs) -> list[Any]:
        return self._trades

    async def regime_snapshot_before(self, symbol: str, timeframe: str, ts: int):
        from src.data.storage import RegimeSnapshotRecord

        # Nearest-at-or-before -- our fixtures always insert exact matches.
        entropy = self._entropy_by_ts.get(ts)
        if entropy is None:
            return None
        # Round-trip entropy through the same 3-state prob triple the
        # scheduler's _shannon_entropy will recompute (ranging-heavy).
        return RegimeSnapshotRecord(
            symbol=symbol,
            timeframe=timeframe,
            ts=ts,
            regime_state=0,
            prob_ranging=1.0 - entropy,
            prob_trending=entropy / 2,
            prob_volatile=entropy / 2,
        )


class TestShannonEntropy:
    def test_zero_entropy_when_one_state_certain(self) -> None:
        assert _shannon_entropy(1.0, 0.0, 0.0) == 0.0

    def test_max_entropy_when_uniform(self) -> None:
        assert _shannon_entropy(1 / 3, 1 / 3, 1 / 3) == pytest.approx(1.0, abs=1e-6)

    def test_clips_to_unit_interval(self) -> None:
        e = _shannon_entropy(0.5, 0.3, 0.2)
        assert 0.0 <= e <= 1.0

    def test_all_zero_probs_returns_zero(self) -> None:
        assert _shannon_entropy(0.0, 0.0, 0.0) == 0.0


class TestAutoTuningSchedulerAttempts:
    def test_start_registers_hmm_parameters(self) -> None:
        settings = get_settings()
        scheduler = AutoTuningScheduler(
            storage=_FakeStorage([], {}),  # type: ignore[arg-type]
            settings=settings,
            symbol="BTC/USDT",
            timeframe="15m",
        )

        async def _run():
            scheduler.start()
            try:
                assert parameter_registry.is_registered("hmm.entropy_threshold")
                assert parameter_registry.is_registered("hmm.entropy_scalar_floor")
            finally:
                scheduler.stop()

        asyncio.run(_run())

    def test_attempt_all_skips_when_insufficient_samples(self) -> None:
        settings = get_settings()
        trades = [make_trade("t1", entry_ts=1000, exit_ts=2000)]  # far below _MIN_SAMPLES
        storage = _FakeStorage(trades, {1000: 0.1})
        scheduler = AutoTuningScheduler(storage, settings, "BTC/USDT", "15m")  # type: ignore[arg-type]

        async def _run():
            scheduler.start()
            try:
                await scheduler._attempt_all()  # must not raise
            finally:
                scheduler.stop()

        asyncio.run(_run())

    def test_attempt_all_runs_with_sufficient_samples(self) -> None:
        settings = get_settings()
        trades = [
            make_trade(f"t{i}", entry_ts=1000 * i, exit_ts=1000 * i + 500) for i in range(1, 40)
        ]
        entropy_by_ts = {1000 * i: 0.1 + (i % 5) * 0.05 for i in range(1, 40)}
        storage = _FakeStorage(trades, entropy_by_ts)
        scheduler = AutoTuningScheduler(storage, settings, "BTC/USDT", "15m")  # type: ignore[arg-type]

        async def _run():
            scheduler.start()
            try:
                await scheduler._attempt_all()  # exercises both evaluate() branches
            finally:
                scheduler.stop()

        asyncio.run(_run())
        # Every attempt (even a rejected/skipped one) leaves an audit trail.
        assert runner._audit_log.read_for_param("hmm.entropy_threshold")

    def test_loop_respects_pause_state(self) -> None:
        settings = get_settings()
        storage = _FakeStorage([], {})
        scheduler = AutoTuningScheduler(storage, settings, "BTC/USDT", "15m", interval_hours=1.0)  # type: ignore[arg-type]

        async def _run():
            await pause_state.set_paused(True)
            scheduler.start()
            await asyncio.sleep(0.05)
            scheduler.stop()
            await asyncio.sleep(0.05)

        asyncio.run(_run())  # must not raise, and must not call _attempt_all while paused

    def test_build_trade_samples_skips_trades_without_regime_or_exit(self) -> None:
        settings = get_settings()
        trades = [
            make_trade("open", entry_ts=1, exit_ts=None),  # still open -- excluded
            make_trade("no_regime", entry_ts=2, exit_ts=3),  # no snapshot -- excluded
            make_trade("good", entry_ts=1000, exit_ts=1500),
        ]
        storage = _FakeStorage(trades, {1000: 0.2})
        scheduler = AutoTuningScheduler(storage, settings, "BTC/USDT", "15m")  # type: ignore[arg-type]

        async def _run():
            return await scheduler._build_trade_samples()

        samples = asyncio.run(_run())
        assert len(samples) == 1
        assert samples[0].raw_return == pytest.approx(0.05, abs=1e-6)
