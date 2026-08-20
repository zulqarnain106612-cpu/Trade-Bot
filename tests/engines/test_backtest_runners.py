"""
Coverage for the batch runners in `src.tuning.engine_backtest`.

`run_all_engine_backtests` and `retrain_e09_walkforward` instantiate all 18
engines and drive them over rolling windows. Running the real engines here
would cost minutes, so the engines are replaced with deterministic stubs and
only the runner wiring (construction, ids, gating, sample accounting) is
under test.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import numpy as np
import pandas as pd
import pytest

from src.engines.schema import EngineOutput
from src.tuning import engine_backtest as eb


_ENGINE_MODULES = [
    ("src.engines.e01_statistical", "E01Statistical", "E-01"),
    ("src.engines.e02_microstructure", "E02Microstructure", "E-02"),
    ("src.engines.e03_information_theory", "E03InformationTheory", "E-03"),
    ("src.engines.e04_fourier", "E04Fourier", "E-04"),
    ("src.engines.e05_onchain", "E05OnChain", "E-05"),
    ("src.engines.e06_fractal", "E06Fractal", "E-06"),
    ("src.engines.e07_linear_algebra", "E07LinearAlgebra", "E-07"),
    ("src.engines.e08_topology", "E08Topology", "E-08"),
]


def make_ohlcv(n: int) -> pd.DataFrame:
    rng = np.random.default_rng(7)
    closes = np.cumprod(1 + rng.normal(0.0002, 0.01, n)) * 50_000.0
    return pd.DataFrame(
        {
            "timestamp_utc": pd.date_range("2024-01-01", periods=n, freq="h", tz="UTC"),
            "open": closes * 0.999,
            "high": closes * 1.004,
            "low": closes * 0.996,
            "close": closes,
            "volume": np.full(n, 1000.0),
        }
    )


def _output(engine_id: str, spot: float) -> EngineOutput:
    return EngineOutput(
        engine_id=engine_id,
        symbol="BTC/USDT",
        timestamp_utc=datetime.now(UTC),
        predicted_price=spot * 1.001,
        confidence=0.6,
        direction=1,
        horizon_hours=4,
    )


def _stub_engine(engine_id: str) -> Any:
    class _Stub:
        async def run(self, symbol: str, data: dict) -> EngineOutput:
            return _output(engine_id, float(data["spot"]))

    return _Stub


# ---------------------------------------------------------------------------
# run_all_engine_backtests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_all_engine_backtests_returns_one_result_per_engine(monkeypatch) -> None:
    """All 18 engines are constructed and each gets its own keyed result."""
    seen: list[str] = []

    async def fake_run(self, df: pd.DataFrame, symbol: str = "BTC/USDT") -> Any:
        seen.append(self._engine_id)
        return eb.EngineBacktestResult(
            engine_id=self._engine_id,
            n_windows=1,
            directional_accuracy=0.6,
            rmse_pct=0.01,
            signal_sharpe=1.5,
            max_drawdown=0.1,
            passes_gate=True,
        )

    monkeypatch.setattr(eb.EngineWalkForwardBacktest, "run", fake_run)

    results = await eb.run_all_engine_backtests(make_ohlcv(250), "ETH/USDT")

    assert len(results) == 18
    assert sorted(results) == [f"E-{i:02d}" for i in range(1, 19)]
    assert seen == [f"E-{i:02d}" for i in range(1, 19)]
    assert all(r.engine_id == eid for eid, r in results.items())


# ---------------------------------------------------------------------------
# EngineWalkForwardBacktest error path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_backtest_skips_windows_where_the_engine_raises() -> None:
    """An engine that always raises yields zero windows rather than crashing."""

    async def boom(symbol: str, data: dict) -> EngineOutput:
        raise RuntimeError("engine exploded")

    bt = eb.EngineWalkForwardBacktest("E-99", boom)
    result = await bt.run(make_ohlcv(400))

    assert result.n_windows == 0
    assert result.passes_gate is False
    assert result.rmse_pct == float("inf")


def test_rmse_pct_is_infinite_for_empty_predictions() -> None:
    assert eb.rmse_pct(np.array([]), np.array([])) == float("inf")


def test_rmse_pct_is_infinite_when_actual_prices_average_to_zero() -> None:
    assert eb.rmse_pct(np.array([1.0, -1.0]), np.array([1.0, -1.0])) == float("inf")


# ---------------------------------------------------------------------------
# retrain_e09_walkforward
# ---------------------------------------------------------------------------


def _patch_feature_engines(monkeypatch) -> None:
    for module, name, engine_id in _ENGINE_MODULES:
        monkeypatch.setattr(f"{module}.{name}", _stub_engine(engine_id))


@pytest.mark.asyncio
async def test_e09_retrain_is_skipped_when_crypto_box_is_disabled(monkeypatch) -> None:
    monkeypatch.delenv("CRYPTO_BOX", raising=False)
    assert await eb.retrain_e09_walkforward(make_ohlcv(400)) == 0


@pytest.mark.asyncio
async def test_e09_retrain_returns_zero_below_the_minimum_sample_count(monkeypatch) -> None:
    """Fewer than 20 collected windows must not trigger a train() call."""
    monkeypatch.setenv("CRYPTO_BOX", "1")
    _patch_feature_engines(monkeypatch)

    trained: list[int] = []
    monkeypatch.setattr(
        "src.engines.e09_ml_meta.E09MlMeta.train",
        lambda self, X, y: trained.append(len(y)),
    )

    # 250 candles → only ~2 windows at the default 180/30/30 geometry.
    assert await eb.retrain_e09_walkforward(make_ohlcv(250)) == 0
    assert trained == []


@pytest.mark.asyncio
async def test_e09_retrain_trains_on_the_collected_samples(monkeypatch) -> None:
    monkeypatch.setenv("CRYPTO_BOX", "true")
    _patch_feature_engines(monkeypatch)
    # Shrink the walk-forward geometry so 20+ windows fit in a small frame.
    monkeypatch.setattr(eb, "_TRAIN_WINDOW", 20)
    monkeypatch.setattr(eb, "_TEST_WINDOW", 2)
    monkeypatch.setattr(eb, "_STEP", 1)

    trained: list[tuple[int, int]] = []
    monkeypatch.setattr(
        "src.engines.e09_ml_meta.E09MlMeta.train",
        lambda self, X, y: trained.append((X.shape[0], len(y))),
    )

    n_samples = await eb.retrain_e09_walkforward(make_ohlcv(80))

    assert n_samples >= 20
    assert trained == [(n_samples, n_samples)]


@pytest.mark.asyncio
async def test_e09_retrain_tolerates_a_failing_feature_engine(monkeypatch) -> None:
    """A raising engine drops its features but must not abort the retrain."""
    monkeypatch.setenv("CRYPTO_BOX", "yes")
    _patch_feature_engines(monkeypatch)

    class _Broken:
        async def run(self, symbol: str, data: dict) -> EngineOutput:
            raise RuntimeError("feature engine down")

    monkeypatch.setattr("src.engines.e03_information_theory.E03InformationTheory", _Broken)
    monkeypatch.setattr(eb, "_TRAIN_WINDOW", 20)
    monkeypatch.setattr(eb, "_TEST_WINDOW", 2)
    monkeypatch.setattr(eb, "_STEP", 1)

    trained: list[int] = []
    monkeypatch.setattr(
        "src.engines.e09_ml_meta.E09MlMeta.train",
        lambda self, X, y: trained.append(len(y)),
    )

    assert await eb.retrain_e09_walkforward(make_ohlcv(80)) >= 20
    assert trained
