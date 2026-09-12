"""
Golden signal cases: the shared definition used by both the generator and the
test.

SIG-001. One module, two consumers, so the fixture that gets written and the
fixture that gets checked can never be produced by two slightly different code
paths -- which is the usual way a golden-file suite stops testing anything.

What a case pins, end to end:

```
market input → data-quality verdict → features → regime → model output
    → signal → risk decision → expected final action
```

The model output is **supplied by the case**, not computed. That is
deliberate: a gradient-boosted tree retrained on a different scikit-learn
build produces slightly different probabilities, so pinning it would make the
suite fail for reasons that have nothing to do with this project's code. What
is pinned is everything downstream of it -- the layers that are supposed to be
deterministic, and the layers that decide whether money moves.

Everything else is real: `DataQualityGate.check_bars`, `build_feature_matrix`
and `evaluate_all_gates` are the production functions, not stand-ins.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from src.config import RiskSettings, TradingMode
from src.data.quality_gate import DataQualityGate, FreshnessBudget
from src.features.pipeline import build_feature_matrix
from src.risk.gates import RiskGateContext, evaluate_all_gates

#: Bars per case. Above the pipeline's widest window (a 200-bar fractional
#: differentiation burn-in plus the triple-barrier horizon) with enough margin
#: that the feature vector at the decision bar is fully warmed up.
BARS = 320

#: 15-minute bars, the project's primary intraday timeframe.
INTERVAL_S = 900

#: A fixed wall clock. The fixtures record a freshness verdict, and "now" has
#: to be a recorded input or every fixture would be stale the moment it was
#: written.
NOW_MS = 1_800_000_000_000

#: Decimal places the recorded feature values are rounded to. Tight enough to
#: catch a behavioural change, loose enough to survive the last bits of a
#: different BLAS.
FEATURE_PRECISION = 6


@dataclass(frozen=True)
class GoldenCase:
    """One recorded market situation and the inputs that are not computed."""

    case_id: str
    description: str
    symbol: str
    #: Compound annualised drift and per-bar volatility of the synthetic path.
    drift: float
    volatility: float
    seed: int
    #: Regime index the detector is taken to have produced: 0 ranging,
    #: 1 trending, 2 volatile.
    regime_state: int
    #: The direction model's P(long). Supplied, not computed -- see the module
    #: docstring.
    p_long: float
    expected_edge_bps: float
    capital_usd: float = 100_000.0
    daily_pnl_usd: float = 0.0
    consecutive_loss_count: int = 0
    notional_usd: float = 2_000.0
    #: Applied to the generated bars to produce a deliberately broken frame.
    corruption: str = ""
    #: Seconds subtracted from NOW_MS when stamping the last bar, to make a
    #: case deliberately stale.
    staleness_s: float = 0.0
    tags: tuple[str, ...] = field(default=())


CASES: tuple[GoldenCase, ...] = (
    GoldenCase(
        case_id="BTCUSDT_15m_case_001",
        description=(
            "A clean uptrend in a trending regime with a confident long model. "
            "Everything passes; this is the case that proves the happy path is "
            "still the happy path."
        ),
        symbol="BTC/USDT",
        drift=0.0006,
        volatility=0.004,
        seed=101,
        regime_state=1,
        p_long=0.68,
        expected_edge_bps=45.0,
        tags=("clean", "trending", "long"),
    ),
    GoldenCase(
        case_id="BTCUSDT_15m_case_002",
        description=(
            "The same clean data in a volatile regime. The regime gate halts, "
            "so no order is produced however confident the model is."
        ),
        symbol="BTC/USDT",
        drift=0.0006,
        volatility=0.004,
        seed=101,
        regime_state=2,
        p_long=0.91,
        expected_edge_bps=120.0,
        tags=("clean", "volatile", "halted"),
    ),
    GoldenCase(
        case_id="BTCUSDT_15m_case_003",
        description=(
            "A choppy, directionless market with a model that has no edge. The "
            "gates pass and the signal is flat: 'no trade' arrived at by having "
            "nothing to say, not by being blocked."
        ),
        symbol="BTC/USDT",
        drift=0.0,
        volatility=0.003,
        seed=202,
        regime_state=0,
        p_long=0.50,
        expected_edge_bps=0.0,
        tags=("clean", "ranging", "flat"),
    ),
    GoldenCase(
        case_id="ETHUSDT_15m_case_001",
        description=(
            "A downtrend with a short signal and a loss-making day, close to but "
            "inside the daily drawdown limit. Pins that a bad day does not halt "
            "until the limit is actually reached."
        ),
        symbol="ETH/USDT",
        drift=-0.0005,
        volatility=0.005,
        seed=303,
        regime_state=1,
        p_long=0.29,
        expected_edge_bps=38.0,
        daily_pnl_usd=-1_900.0,
        tags=("clean", "trending", "short", "near-drawdown-limit"),
    ),
    GoldenCase(
        case_id="ETHUSDT_15m_case_002",
        description=(
            "The same market with one impossible bar: high below low. The data "
            "quality gate refuses the frame, so the feature pipeline is never "
            "reached and no order is produced."
        ),
        symbol="ETH/USDT",
        drift=-0.0005,
        volatility=0.005,
        seed=303,
        regime_state=1,
        p_long=0.29,
        expected_edge_bps=38.0,
        corruption="impossible_ohlc",
        tags=("corrupt", "data-quality"),
    ),
    GoldenCase(
        case_id="ETHUSDT_15m_case_003",
        description=(
            "A well-formed frame that stopped updating an hour ago. Stale data "
            "must not be treated as current (INV-008), so the frame is refused "
            "even though every value in it is valid."
        ),
        symbol="ETH/USDT",
        drift=-0.0005,
        volatility=0.005,
        seed=303,
        regime_state=1,
        p_long=0.29,
        expected_edge_bps=38.0,
        staleness_s=3_600.0,
        tags=("stale", "data-quality"),
    ),
)


def build_bars(case: GoldenCase) -> pd.DataFrame:
    """A deterministic synthetic OHLCV path for one case."""
    rng = np.random.default_rng(case.seed)
    steps = rng.normal(case.drift, case.volatility, size=BARS)
    close = 50_000.0 * np.exp(np.cumsum(steps))
    spread = np.abs(rng.normal(0.0, case.volatility / 2.0, size=BARS)) * close

    last_ms = NOW_MS - int(case.staleness_s * 1000) - INTERVAL_S * 1000
    index = [last_ms - (BARS - 1 - i) * INTERVAL_S * 1000 for i in range(BARS)]

    bars = pd.DataFrame(
        {
            "open": close * (1.0 + rng.normal(0.0, case.volatility / 8.0, size=BARS)),
            "high": np.maximum(close, close) + spread,
            "low": np.minimum(close, close) - spread,
            "close": close,
            "volume": np.abs(rng.normal(1_000.0, 80.0, size=BARS)),
        },
        index=index,
    )
    # Keep the synthetic opens inside the high/low envelope so a clean case is
    # genuinely clean rather than accidentally tripping the OHLC check.
    bars["high"] = bars[["high", "open", "close"]].max(axis=1)
    bars["low"] = bars[["low", "open", "close"]].min(axis=1)

    if case.corruption == "impossible_ohlc":
        bad = bars.index[BARS // 2]
        bars.loc[bad, "high"] = bars.loc[bad, "low"] - 1.0
    elif case.corruption:  # pragma: no cover - guards a typo in a case
        raise ValueError(f"unknown corruption {case.corruption!r}")

    return bars


def _direction(p_long: float) -> int:
    """The project's convention: long above 0.5, short below, flat at it."""
    if p_long > 0.5:
        return 1
    if p_long < 0.5:
        return -1
    return 0


def build_record(case: GoldenCase) -> dict[str, Any]:
    """
    Replay one case through the real deterministic path and record the result.

    This is the single definition of what a fixture contains. The generator
    writes it; the test recomputes it and compares.
    """
    bars = build_bars(case)
    gate = DataQualityGate()
    budget = FreshnessBudget.for_timeframe(INTERVAL_S, bars=3.0)
    now = pd.Timestamp(NOW_MS, unit="ms", tz="UTC").to_pydatetime()

    quality = gate.check_bars(bars, budget=budget, now=now)

    record: dict[str, Any] = {
        "case_id": case.case_id,
        "description": case.description,
        "tags": list(case.tags),
        "market": {
            "symbol": case.symbol,
            "timeframe_s": INTERVAL_S,
            "bar_count": int(len(bars)),
            "first_ts_ms": int(bars.index[0]),
            "last_ts_ms": int(bars.index[-1]),
            "now_ms": NOW_MS,
            "first_close": round(float(bars["close"].iloc[0]), 4),
            "last_close": round(float(bars["close"].iloc[-1]), 4),
        },
        "data_quality": {
            "passed": bool(quality.passed),
            "reason": quality.reason,
            "checks_run": list(quality.checks_run),
        },
        "regime": {"state": case.regime_state},
        "model": {"p_long": case.p_long},
        "signal": {
            "direction": _direction(case.p_long),
            "expected_edge_bps": case.expected_edge_bps,
        },
    }

    if not quality.passed:
        # The frame never reaches the feature pipeline, and recording an empty
        # feature block would imply it did.
        record["features"] = None
        record["risk"] = None
        record["expected_action"] = "no_trade_data_quality"
        return record

    matrix = build_feature_matrix(bars)
    last = matrix.features.iloc[-1]
    record["features"] = {
        name: (None if not math.isfinite(float(value)) else round(float(value), FEATURE_PRECISION))
        for name, value in last.items()
    }

    cfg = RiskSettings(_env_file=None)
    ctx = RiskGateContext(
        daily_pnl_usd=case.daily_pnl_usd,
        starting_equity_usd=case.capital_usd,
        consecutive_loss_count=case.consecutive_loss_count,
        regime_state=case.regime_state,
        notional_usd=case.notional_usd,
        capital_usd=case.capital_usd,
        trading_mode=TradingMode.PAPER,
        direction_gate_pass=True,
        meta_gate_pass=True,
        paper_trading_days=365,
        expected_edge_bps=case.expected_edge_bps,
    )
    gate_result = evaluate_all_gates(ctx, cfg)
    record["risk"] = {
        "status": gate_result.status.value,
        "passed": bool(gate_result.passed),
        "reason": gate_result.reason,
        "size_scalar": round(float(gate_result.size_scalar), 6),
    }

    direction = _direction(case.p_long)
    if not gate_result.passed:
        action = f"no_trade_{gate_result.status.value}"
    elif direction == 0:
        action = "no_trade_flat_signal"
    else:
        action = "enter_long" if direction == 1 else "enter_short"
    record["expected_action"] = action
    return record
