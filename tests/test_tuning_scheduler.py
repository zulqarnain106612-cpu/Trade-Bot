"""Tests for src/tuning/scheduler.py -- AutoTuningScheduler."""

from __future__ import annotations

import asyncio
import random
import time
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
                assert parameter_registry.is_registered("risk.ensemble_blend_weight")
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

    def test_loop_increments_cycle_count_after_attempt_not_before(self) -> None:
        """Off-by-one regression: _loop() must attempt BEFORE incrementing
        _cycle_count. A freshly constructed scheduler starts at cycle 0, and
        _attempt_all()'s own XGBoost throttle gate (cycle_count % interval
        != 0 -> skip) depends on still seeing 0 on the true first
        invocation -- incrementing first would make the real first cycle
        look like cycle 1 and silently skip XGBoost tuning until cycle 24."""
        settings = get_settings()
        storage = _FakeStorage([], {})
        scheduler = AutoTuningScheduler(storage, settings, "BTC/USDT", "15m", interval_hours=1.0)  # type: ignore[arg-type]

        seen_cycle_counts: list[int] = []
        original_attempt_all = scheduler._attempt_all

        async def _spy_attempt_all() -> None:
            seen_cycle_counts.append(scheduler._cycle_count)
            await original_attempt_all()

        scheduler._attempt_all = _spy_attempt_all  # type: ignore[method-assign]

        async def _run():
            scheduler.start()
            await asyncio.sleep(0.05)
            scheduler.stop()
            await asyncio.sleep(0.05)

        asyncio.run(_run())
        assert seen_cycle_counts == [0]
        assert scheduler._cycle_count == 1

    def test_check_redteam_due_logs_when_never_run(self) -> None:
        """A freshly constructed scheduler's RedTeamScheduler has no last_run,
        so is_due() is True immediately -- _check_redteam_due() must log a
        reminder without ever calling record_run() (that would fabricate a
        replay that never happened)."""
        settings = get_settings()
        storage = _FakeStorage([], {})
        scheduler = AutoTuningScheduler(storage, settings, "BTC/USDT", "15m")  # type: ignore[arg-type]

        assert scheduler._redteam_scheduler.last_run is None
        scheduler._check_redteam_due()  # must not raise
        assert scheduler._redteam_scheduler.last_run is None  # still not recorded

    def test_check_redteam_due_silent_after_manual_recent_run(self) -> None:
        settings = get_settings()
        storage = _FakeStorage([], {})
        scheduler = AutoTuningScheduler(storage, settings, "BTC/USDT", "15m")  # type: ignore[arg-type]

        now_ms = int(time.time() * 1000)
        scheduler._redteam_scheduler.record_run(ran_at_ms=now_ms, breached_floor=False)
        assert not scheduler._redteam_scheduler.is_due(now_ms)
        scheduler._check_redteam_due()  # must not raise, and finds nothing due

    def test_attempt_all_uses_registry_champion_not_stale_settings(self, monkeypatch) -> None:
        """Regression (Finding 1): once hmm.entropy_scalar_floor has been
        promoted, hmm.entropy_threshold's OWN evaluation must hold the
        promoted floor constant -- not the raw startup Settings snapshot --
        or the loop keeps proposing challengers against a champion the
        runner already moved past."""
        import src.tuning.scheduler as scheduler_module

        settings = get_settings()
        trades = [
            make_trade(f"t{i}", entry_ts=1000 * i, exit_ts=1000 * i + 500) for i in range(1, 40)
        ]
        entropy_by_ts = {1000 * i: 0.1 + (i % 5) * 0.05 for i in range(1, 40)}
        storage = _FakeStorage(trades, entropy_by_ts)
        scheduler = AutoTuningScheduler(storage, settings, "BTC/USDT", "15m")  # type: ignore[arg-type]

        captured_floors: list[float] = []
        original = scheduler_module.run_entropy_threshold_backtest

        def _spy(*args, **kwargs):
            captured_floors.append(kwargs["champion_floor"])
            return original(*args, **kwargs)

        monkeypatch.setattr(scheduler_module, "run_entropy_threshold_backtest", _spy)
        # runner is a process-wide singleton (src/tuning/state.py) shared
        # across the whole test session: `enabled` defaults False (kill
        # switch), and a prior test's PROPOSED audit entry for this same
        # param name can leave the real cooldown active within the same
        # session. Neither is what this test is about, so bypass both --
        # this test only cares about which champion_floor value evaluate()
        # is built with.
        monkeypatch.setattr(runner._settings, "enabled", True)
        monkeypatch.setattr(runner, "_cooldown_active", lambda param_name: False)

        async def _run():
            scheduler.start()
            try:
                parameter_registry.update_current("hmm.entropy_scalar_floor", 0.42)
                await scheduler._attempt_all()
            finally:
                scheduler.stop()

        asyncio.run(_run())
        assert captured_floors  # sanity: the spy actually intercepted calls
        assert all(f == pytest.approx(0.42) for f in captured_floors)
        assert settings.hmm.entropy_scalar_floor == 0.5  # the stale value that must NOT appear

    def test_attempt_all_intra_cycle_promotion_visible_within_same_cycle(self, monkeypatch) -> None:
        """Tighter regression than the cross-cycle test above: a promotion
        made by the hmm.entropy_threshold iteration must be visible to the
        hmm.entropy_scalar_floor iteration's evaluate() closure in the SAME
        _attempt_all() call -- not just on a later cycle. Catches capturing
        champion_threshold/champion_floor once before the `for param_name in
        (...)` loop instead of fresh inside each evaluate() call."""
        import src.tuning.scheduler as scheduler_module
        from src.tuning.proposer import Proposal
        from src.tuning.runner import AttemptResult

        settings = get_settings()
        trades = [
            make_trade(f"t{i}", entry_ts=1000 * i, exit_ts=1000 * i + 500) for i in range(1, 40)
        ]
        entropy_by_ts = {1000 * i: 0.1 + (i % 5) * 0.05 for i in range(1, 40)}
        storage = _FakeStorage(trades, entropy_by_ts)
        scheduler = AutoTuningScheduler(storage, settings, "BTC/USDT", "15m")  # type: ignore[arg-type]

        captured_thresholds: list[float] = []
        original_backtest = scheduler_module.run_entropy_threshold_backtest

        def _spy_backtest(*args, **kwargs):
            captured_thresholds.append(kwargs["champion_threshold"])
            return original_backtest(*args, **kwargs)

        monkeypatch.setattr(scheduler_module, "run_entropy_threshold_backtest", _spy_backtest)

        def _fake_attempt(param_name, evaluate_fn, primary_metric):
            # Bypasses the real proposer/gate entirely -- this test isolates
            # ONLY the evaluate() closure's registry-read timing, not
            # promotion logic (already covered by tests/test_tuning_runner.py).
            if param_name == "hmm.entropy_threshold":
                # Simulate this iteration promoting a new champion, exactly
                # as TuningRunner.attempt() would via registry.update_current
                # on an accepted live promotion.
                parameter_registry.update_current("hmm.entropy_threshold", 0.58)
            param = parameter_registry.get(param_name)
            proposal = Proposal(
                param_name=param_name,
                champion_value=param.current,
                challenger_value=param.current,
                step_pct=0.1,
            )
            evaluate_fn(param, proposal)
            return AttemptResult(
                param_name=param_name,
                attempted=True,
                accepted=False,
                promoted=False,
                reasons=(),
                challenger_value=param.current,
            )

        monkeypatch.setattr(runner, "attempt", _fake_attempt)

        async def _run():
            scheduler.start()
            try:
                await scheduler._attempt_all()
            finally:
                scheduler.stop()

        asyncio.run(_run())
        # Two evaluate() calls happen: hmm.entropy_threshold's own (which
        # passes proposal.champion_value == pre-promotion 0.5, not the
        # captured champion_threshold var) and hmm.entropy_scalar_floor's
        # (which DOES use the captured champion_threshold var). Only the
        # second is a meaningful assertion here.
        assert len(captured_thresholds) == 2
        assert captured_thresholds[-1] == pytest.approx(0.58)

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


def _enable_runner_for_test(monkeypatch) -> None:
    """Bypass both gates that block TuningRunner.attempt() from ever calling
    evaluate_fn in the default test config: the enabled=False kill switch
    (SelfTuningSettings.enabled defaults False) and cross-test cooldown
    pollution (runner._audit_log is a process-wide singleton not reset by
    _reset_tuning_state, so an earlier test's PROPOSED entry for the same
    param can still be within min_hours_between_attempts=24h). Same pattern
    as test_attempt_all_uses_registry_champion_not_stale_settings above."""
    monkeypatch.setattr(runner._settings, "enabled", True)
    monkeypatch.setattr(runner, "_cooldown_active", lambda param_name: False)


class TestSchedulerLifecycleBranches:
    """Branch coverage for start()'s already-registered skips, stop()
    without a prior start(), and _loop()'s immediate-exit path."""

    def test_start_twice_skips_reregistration(self) -> None:
        settings = get_settings()
        scheduler = AutoTuningScheduler(_FakeStorage([], {}), settings, "BTC/USDT", "15m")  # type: ignore[arg-type]

        async def _run():
            scheduler.start()
            try:
                # Second start() call: every is_registered() check is now
                # True, so every register_* branch is skipped this time.
                scheduler.start()
                assert parameter_registry.is_registered("hmm.entropy_threshold")
                assert parameter_registry.is_registered("hmm.entropy_scalar_floor")
                assert parameter_registry.is_registered("risk.slippage_impact_coeff_bps")
                assert parameter_registry.is_registered("risk.ensemble_blend_weight")
                assert parameter_registry.is_registered("features.atr_window")
                assert parameter_registry.is_registered("xgboost.max_depth")
            finally:
                scheduler.stop()

        asyncio.run(_run())

    def test_stop_without_start_is_a_noop(self) -> None:
        settings = get_settings()
        scheduler = AutoTuningScheduler(_FakeStorage([], {}), settings, "BTC/USDT", "15m")  # type: ignore[arg-type]
        assert scheduler._task is None
        scheduler.stop()  # must not raise -- self._task is None branch
        assert scheduler._stopped is True

    def test_loop_exits_immediately_when_already_stopped(self) -> None:
        settings = get_settings()
        scheduler = AutoTuningScheduler(_FakeStorage([], {}), settings, "BTC/USDT", "15m")  # type: ignore[arg-type]
        scheduler._stopped = True
        asyncio.run(scheduler._loop())  # while-condition False on entry -> returns immediately

    def test_loop_logs_and_continues_when_attempt_all_raises(self) -> None:
        settings = get_settings()
        scheduler = AutoTuningScheduler(
            _FakeStorage([], {}), settings, "BTC/USDT", "15m", interval_hours=1.0
        )  # type: ignore[arg-type]

        async def _boom() -> None:
            raise RuntimeError("simulated attempt failure")

        scheduler._attempt_all = _boom  # type: ignore[method-assign]

        async def _run():
            scheduler.start()
            await asyncio.sleep(0.05)  # one iteration: _boom() raises, loop logs and sleeps
            scheduler.stop()
            await asyncio.sleep(0.05)

        asyncio.run(_run())  # must not propagate the exception


class TestSchedulerEvaluateClosuresInvoked:
    """These exercise the evaluate_*() closures' bodies (and their
    surrounding try/except) which TuningRunner.attempt() only calls when
    the self-tuning kill switch is on and no cooldown is active -- see
    _enable_runner_for_test()."""

    def test_slippage_evaluate_closure_invoked(self, monkeypatch) -> None:
        _enable_runner_for_test(monkeypatch)
        settings = get_settings().model_copy(update={"features": _FAST_CPCV_FEATURES})
        trades = [
            make_trade(f"slip{i}", entry_ts=1000 * i, exit_ts=1000 * i + 500, entry_price=100.0 + i)
            for i in range(1, 40)
        ]
        bars = [make_bar(ts=1000 * i, close=100.0, volume=20.0) for i in range(1, 40)]
        storage = _FakeStorage(trades, {}, bars=bars)
        scheduler = AutoTuningScheduler(storage, settings, "BTC/USDT", "15m")  # type: ignore[arg-type]

        async def _run():
            scheduler.start()
            try:
                await scheduler._attempt_all()
            finally:
                scheduler.stop()

        asyncio.run(_run())
        entries = runner._audit_log.read_for_param("risk.slippage_impact_coeff_bps")
        assert any(e.event_type.value == "evaluated" for e in entries)

    def test_slippage_evaluate_exception_is_caught(self, monkeypatch) -> None:
        import src.tuning.scheduler as scheduler_module

        _enable_runner_for_test(monkeypatch)

        def _boom(*args, **kwargs):
            raise RuntimeError("simulated slippage backtest failure")

        monkeypatch.setattr(scheduler_module, "run_slippage_coeff_backtest", _boom)

        settings = get_settings()
        trades = [
            make_trade(
                f"slipx{i}", entry_ts=1000 * i, exit_ts=1000 * i + 500, entry_price=100.0 + i
            )
            for i in range(1, 40)
        ]
        bars = [make_bar(ts=1000 * i, close=100.0, volume=20.0) for i in range(1, 40)]
        storage = _FakeStorage(trades, {}, bars=bars)
        scheduler = AutoTuningScheduler(storage, settings, "BTC/USDT", "15m")  # type: ignore[arg-type]

        async def _run():
            scheduler.start()
            try:
                await scheduler._attempt_all()  # must not raise -- caught and logged
            finally:
                scheduler.stop()

        asyncio.run(_run())

    def test_feature_window_evaluate_closure_invoked(self, monkeypatch, tmp_path) -> None:
        _enable_runner_for_test(monkeypatch)
        settings = get_settings().model_copy(
            update={"storage": StorageSettings(model_dir=tmp_path)}
        )
        trainer = ModelTrainer(symbol="BTC/USDT", timeframe="15m")
        trainer.save(_fitted_direction_model(), _fitted_meta_model(), tmp_path, version="v1")

        prices = _make_price_series(1500, seed=21)
        bars = [
            make_bar(ts=900_000 * (i + 1), close=prices[i], volume=20.0 + (i % 7))
            for i in range(1500)
        ]
        storage = _FakeStorage([], {}, bars=bars)
        scheduler = AutoTuningScheduler(storage, settings, "BTC/USDT", "15m")  # type: ignore[arg-type]
        scheduler._cycle_count = 1  # skip the (slow) xgboost block this test doesn't cover

        async def _run():
            scheduler.start()
            try:
                await scheduler._attempt_all()
            finally:
                scheduler.stop()

        asyncio.run(_run())
        entries = runner._audit_log.read_for_param("features.atr_window")
        assert any(e.event_type.value == "evaluated" for e in entries)

    def test_feature_window_evaluate_exception_is_caught(self, monkeypatch, tmp_path) -> None:
        import src.tuning.scheduler as scheduler_module

        _enable_runner_for_test(monkeypatch)

        def _boom(*args, **kwargs):
            raise RuntimeError("simulated feature-window backtest failure")

        monkeypatch.setattr(scheduler_module, "run_feature_window_backtest", _boom)

        settings = get_settings().model_copy(
            update={"storage": StorageSettings(model_dir=tmp_path)}
        )
        trainer = ModelTrainer(symbol="BTC/USDT", timeframe="15m")
        trainer.save(_fitted_direction_model(), _fitted_meta_model(), tmp_path, version="v1")

        prices = _make_price_series(1500, seed=22)
        bars = [
            make_bar(ts=900_000 * (i + 1), close=prices[i], volume=20.0 + (i % 7))
            for i in range(1500)
        ]
        storage = _FakeStorage([], {}, bars=bars)
        scheduler = AutoTuningScheduler(storage, settings, "BTC/USDT", "15m")  # type: ignore[arg-type]
        scheduler._cycle_count = 1

        async def _run():
            scheduler.start()
            try:
                await scheduler._attempt_all()  # must not raise -- caught and logged
            finally:
                scheduler.stop()

        asyncio.run(_run())

    def test_xgboost_feature_matrix_value_error_is_caught(self, monkeypatch, tmp_path) -> None:
        import src.tuning.scheduler as scheduler_module

        _enable_runner_for_test(monkeypatch)

        def _boom(*args, **kwargs):
            raise ValueError("simulated feature matrix build failure")

        monkeypatch.setattr(scheduler_module, "build_feature_matrix", _boom)

        settings = get_settings().model_copy(
            update={"storage": StorageSettings(model_dir=tmp_path)}
        )
        trainer = ModelTrainer(symbol="BTC/USDT", timeframe="15m")
        trainer.save(_fitted_direction_model(), _fitted_meta_model(), tmp_path, version="v1")

        prices = _make_price_series(1500, seed=23)
        bars = [
            make_bar(ts=900_000 * (i + 1), close=prices[i], volume=20.0 + (i % 7))
            for i in range(1500)
        ]
        storage = _FakeStorage([], {}, bars=bars)
        scheduler = AutoTuningScheduler(storage, settings, "BTC/USDT", "15m")  # type: ignore[arg-type]
        assert scheduler._cycle_count == 0  # cycle 0 -> xgboost block is attempted, hits the stub

        async def _run():
            scheduler.start()
            try:
                await scheduler._attempt_all()  # must not raise -- ValueError caught and logged
            finally:
                scheduler.stop()

        asyncio.run(_run())

    def test_xgboost_evaluate_closure_invoked_and_exception_caught(
        self, monkeypatch, tmp_path
    ) -> None:
        """Covers both the evaluate_xgb() closure body (int-field rounding +
        the backtest call) and its surrounding try/except in one pass: stub
        run_xgboost_hyperparam_backtest to raise on the first call the
        closure actually makes, avoiding a real CPCV retrain."""
        import src.tuning.scheduler as scheduler_module

        _enable_runner_for_test(monkeypatch)

        calls: list[dict[str, object]] = []

        def _boom(*args, **kwargs):
            calls.append(kwargs)
            raise RuntimeError("simulated xgboost backtest failure")

        monkeypatch.setattr(scheduler_module, "run_xgboost_hyperparam_backtest", _boom)

        settings = get_settings().model_copy(
            update={"storage": StorageSettings(model_dir=tmp_path)}
        )
        trainer = ModelTrainer(symbol="BTC/USDT", timeframe="15m")
        trainer.save(_fitted_direction_model(), _fitted_meta_model(), tmp_path, version="v1")

        prices = _make_price_series(1500, seed=24)
        bars = [
            make_bar(ts=900_000 * (i + 1), close=prices[i], volume=20.0 + (i % 7))
            for i in range(1500)
        ]
        storage = _FakeStorage([], {}, bars=bars)
        scheduler = AutoTuningScheduler(storage, settings, "BTC/USDT", "15m")  # type: ignore[arg-type]

        async def _run():
            scheduler.start()
            try:
                await scheduler._attempt_all()  # must not raise -- caught and logged
            finally:
                scheduler.stop()

        asyncio.run(_run())
        assert calls  # the evaluate_xgb() closure body did call the (stubbed) backtest
        # int-field rounding branch: max_depth is in XGBOOST_INT_FIELDS
        int_calls = [c for c in calls if c.get("field_name") == "max_depth"]
        assert int_calls
        assert isinstance(int_calls[0]["champion_value"], int)


class _EmptyBarsFakeStorage(_FakeStorage):
    """latest_bar_ts() reports data exists, but bars_before() returns
    nothing for it -- simulates a race between the two storage reads."""

    async def bars_before(self, symbol: str, timeframe: str, ts: int, limit: int = 21):
        return []


class TestSchedulerHelperEdgeCases:
    def test_build_feature_bars_df_none_when_bars_before_empty(self) -> None:
        settings = get_settings()
        bars = [make_bar(ts=900_000, close=100.0, volume=20.0)]
        storage = _EmptyBarsFakeStorage([], {}, bars=bars)
        scheduler = AutoTuningScheduler(storage, settings, "BTC/USDT", "15m")  # type: ignore[arg-type]
        assert asyncio.run(scheduler._build_feature_bars_df()) is None

    def test_build_slippage_samples_skips_non_positive_reference_price(self) -> None:
        settings = get_settings()
        trades = [make_trade("t1", entry_ts=1000, exit_ts=1500, entry_price=101.0)]
        bars = [make_bar(ts=1000, close=0.0, volume=20.0)]  # reference_price <= 0
        storage = _FakeStorage(trades, {}, bars=bars)
        scheduler = AutoTuningScheduler(storage, settings, "BTC/USDT", "15m")  # type: ignore[arg-type]
        samples = asyncio.run(scheduler._build_slippage_samples())
        assert samples == []

    def test_build_slippage_samples_skips_non_positive_adv(self) -> None:
        settings = get_settings()
        trades = [make_trade("t1", entry_ts=1000, exit_ts=1500, entry_price=101.0)]
        # Only the reference bar (ts=1000) is left after excluding it from
        # history -- adv_20d averages over an empty `history` slice's
        # volume, all zero, so adv_20d <= 0.
        bars = [make_bar(ts=1000, close=100.0, volume=0.0)]
        storage = _FakeStorage(trades, {}, bars=bars)
        scheduler = AutoTuningScheduler(storage, settings, "BTC/USDT", "15m")  # type: ignore[arg-type]
        samples = asyncio.run(scheduler._build_slippage_samples())
        assert samples == []

    def test_build_trade_samples_skips_missing_exit_price_or_bad_entry_price(self) -> None:
        settings = get_settings()
        trades = [
            make_trade("no_exit_price", entry_ts=1000, exit_ts=1500, exit_price=None),
            make_trade("bad_entry", entry_ts=2000, exit_ts=2500, entry_price=0.0),
            make_trade("good", entry_ts=3000, exit_ts=3500),
        ]
        storage = _FakeStorage(trades, {1000: 0.1, 2000: 0.1, 3000: 0.2})
        scheduler = AutoTuningScheduler(storage, settings, "BTC/USDT", "15m")  # type: ignore[arg-type]
        samples = asyncio.run(scheduler._build_trade_samples())
        assert len(samples) == 1
