"""
Walk-forward backtest for Crypto-Box engines (CAT-7).

Train: rolling 180 candles (1h) = 7.5 days.
Test:  next 30 candles (OOS).
Step:  advance 30 candles, retrain, re-test.
Gap G-07 fix: 1-candle gap between train-end and feature computation window.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd
import structlog


log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

_TRAIN_WINDOW = 180
_TEST_WINDOW = 30
_STEP = 30
_GAP = 1  # G-07: 1-candle look-ahead gap


def directional_accuracy(pred_dirs: np.ndarray, actual_rets: np.ndarray) -> float:
    """Fraction of predictions with correct direction."""
    if len(pred_dirs) == 0:
        return 0.0
    correct = (np.sign(pred_dirs) == np.sign(actual_rets)).sum()
    return float(correct / len(pred_dirs))


def rmse_pct(pred_prices: np.ndarray, actual_prices: np.ndarray) -> float:
    if len(pred_prices) == 0 or actual_prices.mean() == 0:
        return float("inf")
    return float(np.sqrt(np.mean((pred_prices - actual_prices) ** 2)) / actual_prices.mean())


def signal_sharpe(returns: np.ndarray) -> float:
    std = returns.std()
    if std == 0:
        return 0.0
    return float(returns.mean() / std * np.sqrt(8760))


def max_drawdown(returns: np.ndarray) -> float:
    cum = np.cumprod(1 + returns)
    peak = np.maximum.accumulate(cum)
    dd = (cum - peak) / (peak + 1e-12)
    return float(abs(dd.min()))


@dataclass
class EngineBacktestResult:
    engine_id: str
    n_windows: int
    directional_accuracy: float  # threshold > 0.55
    rmse_pct: float  # threshold < 2%
    signal_sharpe: float  # threshold > 1.0
    max_drawdown: float  # threshold < 30%
    passes_gate: bool
    per_window: list[dict[str, Any]] = field(default_factory=list)


class EngineWalkForwardBacktest:
    """
    Walk-forward backtest for a single engine.

    Accepts a callable `engine_fn(data: dict) -> EngineOutput` that wraps
    the engine's `run()` coroutine in a synchronous façade for batched use.
    """

    def __init__(self, engine_id: str, engine_fn: Any) -> None:
        self._engine_id = engine_id
        self._engine_fn = engine_fn

    async def run(self, df: pd.DataFrame, symbol: str = "BTC/USDT") -> EngineBacktestResult:
        n = len(df)
        min_start = _TRAIN_WINDOW + _GAP
        if n < min_start + _TEST_WINDOW:
            log.warning("backtest_insufficient_data", engine=self._engine_id, n=n)
            return EngineBacktestResult(
                engine_id=self._engine_id,
                n_windows=0,
                directional_accuracy=0.0,
                rmse_pct=float("inf"),
                signal_sharpe=0.0,
                max_drawdown=0.0,
                passes_gate=False,
            )

        all_pred_dirs: list[float] = []
        all_actual_rets: list[float] = []
        all_pred_prices: list[float] = []
        all_actual_prices: list[float] = []
        signal_rets: list[float] = []
        per_window: list[dict] = []

        for start in range(min_start, n - _TEST_WINDOW, _STEP):
            train_end = start - _GAP  # G-07: gap prevents look-ahead
            train_df = df.iloc[start - _TRAIN_WINDOW : train_end]
            test_df = df.iloc[start : start + _TEST_WINDOW]

            spot = float(train_df["close"].iloc[-1])
            data = {"ohlcv": train_df, "spot": spot}

            try:
                output = await self._engine_fn(symbol, data)
            except Exception as exc:
                log.warning("backtest_engine_error", engine=self._engine_id, exc=str(exc))
                continue

            # Compare against first test candle close
            actual_price = float(test_df["close"].iloc[0])
            actual_ret = (actual_price - spot) / spot

            pred_dir = float(output.direction)
            pred_price = float(output.predicted_price)

            all_pred_dirs.append(pred_dir)
            all_actual_rets.append(actual_ret)
            all_pred_prices.append(pred_price)
            all_actual_prices.append(actual_price)

            # Signal return: apply direction to actual return
            signal_ret = float(output.direction) * actual_ret - 0.0002  # fee
            signal_rets.append(signal_ret)

            per_window.append(
                {
                    "train_start": start - _TRAIN_WINDOW,
                    "train_end": train_end,
                    "test_start": start,
                    "directional": float(np.sign(pred_dir) == np.sign(actual_ret)),
                    "signal_ret": signal_ret,
                }
            )

        if not all_pred_dirs:
            return EngineBacktestResult(
                engine_id=self._engine_id,
                n_windows=0,
                directional_accuracy=0.0,
                rmse_pct=float("inf"),
                signal_sharpe=0.0,
                max_drawdown=0.0,
                passes_gate=False,
            )

        da = directional_accuracy(np.array(all_pred_dirs), np.array(all_actual_rets))
        rp = rmse_pct(np.array(all_pred_prices), np.array(all_actual_prices))
        ss = signal_sharpe(np.array(signal_rets))
        md = max_drawdown(np.array(signal_rets))

        passes = da > 0.5 and rp < 0.02 and md < 0.30

        result = EngineBacktestResult(
            engine_id=self._engine_id,
            n_windows=len(per_window),
            directional_accuracy=da,
            rmse_pct=rp,
            signal_sharpe=ss,
            max_drawdown=md,
            passes_gate=passes,
            per_window=per_window,
        )

        log.info(
            "backtest_complete",
            engine=self._engine_id,
            n_windows=len(per_window),
            da=round(da, 3),
            rmse_pct=round(rp * 100, 2),
            sharpe=round(ss, 2),
            max_dd=round(md * 100, 2),
            passes=passes,
        )
        return result


async def run_all_engine_backtests(
    df: pd.DataFrame, symbol: str = "BTC/USDT"
) -> dict[str, EngineBacktestResult]:
    """Run walk-forward backtests for all 18 engines. Returns results keyed by engine_id."""
    from src.engines.e01_statistical import E01Statistical
    from src.engines.e02_microstructure import E02Microstructure
    from src.engines.e03_information_theory import E03InformationTheory
    from src.engines.e04_fourier import E04Fourier
    from src.engines.e05_onchain import E05OnChain
    from src.engines.e06_fractal import E06Fractal
    from src.engines.e07_linear_algebra import E07LinearAlgebra
    from src.engines.e08_topology import E08Topology
    from src.engines.e09_ml_meta import E09MlMeta
    from src.engines.e10_supply import E10Supply
    from src.engines.e11_stochastic import E11Stochastic
    from src.engines.e12_options import E12Options
    from src.engines.e13_contagion import E13Contagion
    from src.engines.e14_sentiment import E14Sentiment
    from src.engines.e15_rl import E15RL
    from src.engines.e16_adversarial import E16Adversarial
    from src.engines.e17_liquidity import E17Liquidity
    from src.engines.e18_network import E18Network

    engines: list[Any] = [
        E01Statistical(),
        E02Microstructure(),
        E03InformationTheory(),
        E04Fourier(),
        E05OnChain(),
        E06Fractal(),
        E07LinearAlgebra(),
        E08Topology(),
        E09MlMeta(),
        E10Supply(),
        E11Stochastic(),
        E12Options(),
        E13Contagion(),
        E14Sentiment(),
        E15RL(),
        E16Adversarial(),
        E17Liquidity(),
        E18Network(),
    ]

    results: dict[str, EngineBacktestResult] = {}
    for i, engine in enumerate(engines):
        eid = f"E-{i + 1:02d}"
        bt = EngineWalkForwardBacktest(eid, engine.run)
        results[eid] = await bt.run(df, symbol)

    return results


async def retrain_e09_walkforward(df: pd.DataFrame, symbol: str = "BTC/USDT") -> int:
    """
    Walk-forward retrain for E-09 (ML meta-engine).

    Runs E-01..E-08 on rolling 180-candle windows, builds the feature matrix
    used by E09MlMeta._build_features(), labels each window by next-bar direction,
    then calls E09MlMeta.train(X, y) on the full collected set.

    Returns the number of training samples collected (0 = skipped/failed).
    """
    import os

    if os.environ.get("CRYPTO_BOX", "").lower() not in ("1", "true", "yes"):
        return 0

    from src.engines.e01_statistical import E01Statistical
    from src.engines.e02_microstructure import E02Microstructure
    from src.engines.e03_information_theory import E03InformationTheory
    from src.engines.e04_fourier import E04Fourier
    from src.engines.e05_onchain import E05OnChain
    from src.engines.e06_fractal import E06Fractal
    from src.engines.e07_linear_algebra import E07LinearAlgebra
    from src.engines.e08_topology import E08Topology
    from src.engines.e09_ml_meta import E09MlMeta

    feature_engines: list[Any] = [
        E01Statistical(),
        E02Microstructure(),
        E03InformationTheory(),
        E04Fourier(),
        E05OnChain(),
        E06Fractal(),
        E07LinearAlgebra(),
        E08Topology(),
    ]
    e09 = E09MlMeta()

    X_rows: list[np.ndarray] = []
    y_vals: list[int] = []

    n = len(df)
    min_start = _TRAIN_WINDOW + _GAP
    for start in range(min_start, n - _TEST_WINDOW, _STEP):
        train_end = start - _GAP
        train_df = df.iloc[start - _TRAIN_WINDOW : train_end]
        spot = float(train_df["close"].iloc[-1])
        next_close = float(df["close"].iloc[start])
        label = 1 if next_close > spot else 0

        data = {"ohlcv": train_df, "spot": spot}
        engine_outputs: dict[str, object] = {}
        for eng in feature_engines:
            try:
                out = await eng.run(symbol, data)
                engine_outputs[out.engine_id] = out
            except Exception:
                pass

        feat = e09._build_features(engine_outputs, spot)  # type: ignore[arg-type]
        X_rows.append(feat.flatten())
        y_vals.append(label)

    if len(X_rows) < 20:
        log.warning("e09_retrain_insufficient_samples", n=len(X_rows))
        return 0

    X = np.array(X_rows, dtype=np.float32)
    y = np.array(y_vals, dtype=np.int32)
    e09.train(X, y)
    log.info("e09_walkforward_retrain_done", n_samples=len(y))
    return len(y)
