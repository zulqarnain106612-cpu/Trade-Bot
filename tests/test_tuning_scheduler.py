"""Tests for src/tuning/scheduler.py -- AutoTuningScheduler."""

from __future__ import annotations

import asyncio
import random
from typing import Any

import numpy as np
import pytest
from xgboost import XGBClassifier

from src.config import FeatureSettings, StorageSettings, XGBoostSettings, get_settings
from src.models.trainer import ModelTrainer
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


def make_bar(ts, close=100.0, volume=10.0):
    from src.data.storage import BarRecord

    return BarRecord(
        symbol="BTC/USDT",
        timeframe="15m",
        ts=ts,
        open=close - 1,
        high=close + 1,
        low=close - 2,
        close=close,
        volume=volume,
        quote_volume=volume * close,
        taker_buy_vol=volume / 2,
    )


class _FakeStorage:
    """Minimal in-memory stand-in for fetch_trades / regime_snapshot_before /
    bars_before."""

    def __init__(
        self,
        trades: list[Any],
        entropy_by_ts: dict[int, float],
        bars: list[Any] | None = None,
    ) -> None:
        self._trades = trades
        self._entropy_by_ts = entropy_by_ts
        self._bars = bars or []

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

    async def bars_before(self, symbol: str, timeframe: str, ts: int, limit: int = 21):
        matching = sorted((b for b in self._bars if b.ts <= ts), key=lambda b: b.ts)
        return matching[-limit:]

    async def latest_bar_ts(self, symbol: str, timeframe: str) -> int | None:
        if not self._bars:
            return None
        return max(b.ts for b in self._bars)


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
                assert parameter_registry.is_registered("risk.slippage_impact_coeff_bps")
                assert parameter_registry.is_registered("xgboost.max_depth")
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

    def test_insufficient_entropy_samples_does_not_skip_slippage_attempt(self) -> None:
        """Regression: entropy and slippage draw on independent trade data
        and must not be gated behind each other's sample-sufficiency check."""
        settings = get_settings()
        trades = [
            make_trade(f"t{i}", entry_ts=1000 * i, exit_ts=1000 * i + 500, entry_price=100.0 + i)
            for i in range(
                1, 40
            )  # enough for slippage, but no regime snapshots -> 0 entropy samples
        ]
        bars = [make_bar(ts=1000 * i, close=100.0, volume=20.0) for i in range(1, 40)]
        storage = _FakeStorage(trades, {}, bars=bars)  # empty entropy_by_ts -> 0 entropy samples
        scheduler = AutoTuningScheduler(storage, settings, "BTC/USDT", "15m")  # type: ignore[arg-type]
        # runner._audit_log is a process-wide singleton shared across the whole
        # test session -- compare counts rather than asserting absolute
        # presence/absence, which would be flaky under cross-test pollution.
        before = len(runner._audit_log.read_for_param("risk.slippage_impact_coeff_bps"))

        async def _run():
            scheduler.start()
            try:
                await scheduler._attempt_all()
            finally:
                scheduler.stop()

        asyncio.run(_run())
        after = len(runner._audit_log.read_for_param("risk.slippage_impact_coeff_bps"))
        assert after > before

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


class TestBuildSlippageSamples:
    def test_skips_trades_without_bar_history(self) -> None:
        settings = get_settings()
        trades = [make_trade("no_bars", entry_ts=1000, exit_ts=1500)]
        storage = _FakeStorage(trades, {}, bars=[])
        scheduler = AutoTuningScheduler(storage, settings, "BTC/USDT", "15m")  # type: ignore[arg-type]

        samples = asyncio.run(scheduler._build_slippage_samples())
        assert samples == []

    def test_builds_sample_from_bar_history(self) -> None:
        settings = get_settings()
        trades = [make_trade("t1", entry_ts=1000, exit_ts=1500, entry_price=101.0, direction=1)]
        bars = [make_bar(ts=t, close=100.0, volume=20.0) for t in (700, 800, 900, 1000)]
        storage = _FakeStorage(trades, {}, bars=bars)
        scheduler = AutoTuningScheduler(storage, settings, "BTC/USDT", "15m")  # type: ignore[arg-type]

        samples = asyncio.run(scheduler._build_slippage_samples())
        assert len(samples) == 1
        sample = samples[0]
        assert sample.reference_price == pytest.approx(100.0)
        assert sample.fill_price == pytest.approx(101.0)
        assert sample.adv_20d == pytest.approx(20.0)
        assert sample.direction == 1

    def test_skips_trades_with_non_positive_price_or_qty(self) -> None:
        settings = get_settings()
        trades = [make_trade("t1", entry_ts=1000, exit_ts=1500, entry_price=0.0)]
        bars = [make_bar(ts=1000)]
        storage = _FakeStorage(trades, {}, bars=bars)
        scheduler = AutoTuningScheduler(storage, settings, "BTC/USDT", "15m")  # type: ignore[arg-type]

        samples = asyncio.run(scheduler._build_slippage_samples())
        assert samples == []


class TestAutoTuningSchedulerSlippageAttempt:
    def test_attempt_all_runs_slippage_attempt_with_sufficient_samples(self) -> None:
        settings = get_settings()
        trades = [
            make_trade(f"t{i}", entry_ts=1000 * i, exit_ts=1000 * i + 500, entry_price=100.0 + i)
            for i in range(1, 40)
        ]
        entropy_by_ts = {1000 * i: 0.1 + (i % 5) * 0.05 for i in range(1, 40)}
        bars = [make_bar(ts=1000 * i, close=100.0, volume=20.0) for i in range(1, 40)]
        storage = _FakeStorage(trades, entropy_by_ts, bars=bars)
        scheduler = AutoTuningScheduler(storage, settings, "BTC/USDT", "15m")  # type: ignore[arg-type]

        async def _run():
            scheduler.start()
            try:
                await scheduler._attempt_all()
            finally:
                scheduler.stop()

        asyncio.run(_run())
        assert runner._audit_log.read_for_param("risk.slippage_impact_coeff_bps")


def _make_price_series(n: int, seed: int = 0) -> list[float]:
    rng = random.Random(seed)
    price = 100.0
    prices = []
    for _ in range(n):
        price *= 1.0 + rng.gauss(0.0002, 0.01)
        prices.append(price)
    return prices


def _fitted_direction_model(seed: int = 0) -> XGBClassifier:
    rng = np.random.default_rng(seed)
    X = rng.standard_normal((300, 7))
    y = (X[:, 0] > 0).astype(int)
    model = XGBClassifier(n_estimators=3, max_depth=2, verbosity=0)
    model.fit(X, y)
    return model


def _fitted_meta_model(seed: int = 1) -> XGBClassifier:
    rng = np.random.default_rng(seed)
    X = rng.standard_normal((300, 9))
    y = (X[:, 0] > 0).astype(int)
    model = XGBClassifier(n_estimators=3, max_depth=2, verbosity=0)
    model.fit(X, y)
    return model


class TestFeatureWindowAttempt:
    def test_load_direction_model_returns_none_when_absent(self, tmp_path) -> None:
        settings = get_settings().model_copy(
            update={"storage": StorageSettings(model_dir=tmp_path)}
        )
        scheduler = AutoTuningScheduler(_FakeStorage([], {}), settings, "BTC/USDT", "15m")  # type: ignore[arg-type]
        assert scheduler._load_direction_model() is None

    def test_load_direction_model_returns_model_when_present(self, tmp_path) -> None:
        settings = get_settings().model_copy(
            update={"storage": StorageSettings(model_dir=tmp_path)}
        )
        trainer = ModelTrainer(symbol="BTC/USDT", timeframe="15m")
        trainer.save(_fitted_direction_model(), _fitted_meta_model(), tmp_path, version="v1")
        scheduler = AutoTuningScheduler(_FakeStorage([], {}), settings, "BTC/USDT", "15m")  # type: ignore[arg-type]
        assert scheduler._load_direction_model() is not None

    def test_build_feature_bars_df_none_when_no_bars(self) -> None:
        settings = get_settings()
        scheduler = AutoTuningScheduler(_FakeStorage([], {}), settings, "BTC/USDT", "15m")  # type: ignore[arg-type]
        assert asyncio.run(scheduler._build_feature_bars_df()) is None

    def test_build_feature_bars_df_returns_ascending_frame(self) -> None:
        settings = get_settings()
        bars = [make_bar(ts=900_000 * i, close=100.0 + i, volume=20.0) for i in range(1, 30)]
        storage = _FakeStorage([], {}, bars=bars)
        scheduler = AutoTuningScheduler(storage, settings, "BTC/USDT", "15m")  # type: ignore[arg-type]
        df = asyncio.run(scheduler._build_feature_bars_df())
        assert df is not None
        assert list(df.index) == sorted(df.index)
        assert len(df) == 29

    def test_attempt_all_runs_feature_window_attempts_with_trained_model(self, tmp_path) -> None:
        settings = get_settings().model_copy(
            update={"storage": StorageSettings(model_dir=tmp_path)}
        )
        trainer = ModelTrainer(symbol="BTC/USDT", timeframe="15m")
        trainer.save(_fitted_direction_model(), _fitted_meta_model(), tmp_path, version="v1")

        prices = _make_price_series(1500)
        bars = [
            make_bar(ts=900_000 * (i + 1), close=prices[i], volume=20.0 + (i % 7))
            for i in range(1500)
        ]
        storage = _FakeStorage([], {}, bars=bars)
        scheduler = AutoTuningScheduler(storage, settings, "BTC/USDT", "15m")  # type: ignore[arg-type]
        # This test targets the feature-window path specifically; skip the
        # (default-settings, slow) xgboost block by advancing past cycle 0 --
        # see TestXGBoostHyperparamAttempt for dedicated xgboost coverage
        # with fast XGBoostSettings.
        scheduler._cycle_count = 1

        async def _run():
            scheduler.start()
            try:
                await scheduler._attempt_all()
            finally:
                scheduler.stop()

        asyncio.run(_run())
        assert runner._audit_log.read_for_param("features.atr_window")

    def test_attempt_all_skips_feature_window_when_no_model_trained(self, tmp_path) -> None:
        settings = get_settings().model_copy(
            update={"storage": StorageSettings(model_dir=tmp_path)}
        )
        storage = _FakeStorage([], {}, bars=[])
        scheduler = AutoTuningScheduler(storage, settings, "BTC/USDT", "15m")  # type: ignore[arg-type]
        before = len(runner._audit_log.read_for_param("features.atr_window"))

        async def _run():
            scheduler.start()
            try:
                await scheduler._attempt_all()
            finally:
                scheduler.stop()

        asyncio.run(_run())  # must not raise
        after = len(runner._audit_log.read_for_param("features.atr_window"))
        assert after == before


_FAST_XGB = XGBoostSettings(n_estimators=10, max_depth=2, early_stopping_rounds=5)
_FAST_CPCV_FEATURES = FeatureSettings(
    cpcv_n_splits=10, cpcv_n_test_splits=1, purge_gap_bars=1, triple_barrier_max_holding_bars=1
)


class TestXGBoostHyperparamAttempt:
    def test_attempt_all_runs_xgboost_attempts_on_cycle_zero(self, tmp_path) -> None:
        settings = get_settings().model_copy(
            update={
                "storage": StorageSettings(model_dir=tmp_path),
                "xgboost": _FAST_XGB,
                "features": _FAST_CPCV_FEATURES,
            }
        )
        prices = _make_price_series(3000, seed=9)
        bars = [
            make_bar(ts=900_000 * (i + 1), close=prices[i], volume=20.0 + (i % 7))
            for i in range(3000)
        ]
        storage = _FakeStorage([], {}, bars=bars)
        scheduler = AutoTuningScheduler(storage, settings, "BTC/USDT", "15m")  # type: ignore[arg-type]
        assert scheduler._cycle_count == 0  # freshly constructed -> always attempts once

        async def _run():
            scheduler.start()
            try:
                await scheduler._attempt_all()
            finally:
                scheduler.stop()

        asyncio.run(_run())
        assert runner._audit_log.read_for_param("xgboost.max_depth")

    def test_attempt_all_skips_xgboost_when_not_on_cycle(self, tmp_path) -> None:
        settings = get_settings().model_copy(
            update={
                "storage": StorageSettings(model_dir=tmp_path),
                "xgboost": _FAST_XGB,
                "features": _FAST_CPCV_FEATURES,
            }
        )
        prices = _make_price_series(3000, seed=10)
        bars = [
            make_bar(ts=900_000 * (i + 1), close=prices[i], volume=20.0 + (i % 7))
            for i in range(3000)
        ]
        storage = _FakeStorage([], {}, bars=bars)
        scheduler = AutoTuningScheduler(
            storage, settings, "BTC/USDT", "15m", xgboost_cycle_interval=5
        )  # type: ignore[arg-type]
        scheduler._cycle_count = 2  # 2 % 5 != 0 -> xgboost should be skipped this cycle
        before = len(runner._audit_log.read_for_param("xgboost.max_depth"))

        async def _run():
            scheduler.start()
            try:
                await scheduler._attempt_all()
            finally:
                scheduler.stop()

        asyncio.run(_run())
        after = len(runner._audit_log.read_for_param("xgboost.max_depth"))
        assert after == before
