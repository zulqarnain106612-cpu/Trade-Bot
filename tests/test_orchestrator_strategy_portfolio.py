"""
Tests for the orchestrator's strategy-portfolio wiring.

The registry was populated, weighted and kill-switchable long before
anything called generate_signal(). These tests pin the hand-off that closes
that gap, and — just as importantly — that the hand-off cannot take a tick
down: the incumbent signal has already cleared its gates by the time the
portfolio is polled.
"""

from __future__ import annotations

import pandas as pd
import pytest
import structlog

import src.engine.orchestrator as orch_mod
from src.config import Timeframe
from src.engine.orchestrator import Orchestrator
from src.engine.strategy_portfolio import (
    PortfolioInputs,
    StrategyPortfolioRunner,
    VerdictStatus,
)
from src.strategies.breakout import BreakoutStrategy
from src.strategies.registry import StrategyRegistry


class _Bar:
    def __init__(self, i: int, *, spike: bool = False) -> None:
        self.close = 100.0 + i * 0.01
        self.high = self.close + 1.0
        self.low = self.close - 1.0
        self.volume = 100.0 if spike else 10.0


class _Storage:
    """Storage stub exposing only what _build_portfolio_inputs reads."""

    def __init__(
        self, *, bars: list[_Bar] | None = None, intel: pd.DataFrame | None = None
    ) -> None:
        self._bars = bars if bars is not None else []
        self._intel = intel if intel is not None else pd.DataFrame()

    async def fetch_bars(self, symbol, timeframe, since_ts=0, limit=0):
        return self._bars

    async def fetch_intelligence_features(self, symbol, timeframe):
        return self._intel


class _FailingStorage:
    async def fetch_bars(self, *a, **k):
        raise RuntimeError("db down")

    async def fetch_intelligence_features(self, *a, **k):
        raise RuntimeError("db down")


def _orchestrator(storage: object) -> Orchestrator:
    """Orchestrator without __init__ — only the portfolio path is under test."""
    orch = object.__new__(Orchestrator)
    orch._log = structlog.get_logger().bind(component="orchestrator_test")
    orch._storage = storage
    orch._symbol = "BTC/USDT"
    orch._last_portfolio_evaluation = {}
    orch._fetcher = _Fetcher({})
    return orch


async def test_build_inputs_carries_bar_window():
    bars = [_Bar(i) for i in range(120)]
    inputs = await _orchestrator(_Storage(bars=bars))._build_portfolio_inputs(Timeframe.INTRADAY)
    assert inputs.symbol == "BTC/USDT"
    assert inputs.timeframe == "15m"
    assert inputs.closes is not None
    assert len(inputs.closes) == 120
    assert inputs.highs is not None and len(inputs.highs) == 120


async def test_build_inputs_takes_the_window_ending_at_now():
    # fetch_bars returns the oldest rows first, so the tail is the window
    # ending at the current bar — a head slice would evaluate stale history.
    bars = [_Bar(i) for i in range(400)]
    inputs = await _orchestrator(_Storage(bars=bars))._build_portfolio_inputs(Timeframe.INTRADAY)
    assert inputs.closes is not None
    assert inputs.closes[-1] == pytest.approx(bars[-1].close)


async def test_build_inputs_carries_funding_history():
    intel = pd.DataFrame({"intelligence_binance_funding_rate_pct": [0.01] * 39 + [0.06]})
    inputs = await _orchestrator(_Storage(intel=intel))._build_portfolio_inputs(Timeframe.INTRADAY)
    assert inputs.funding_rate_pct == pytest.approx(0.06)
    assert inputs.funding_history_pct is not None
    assert len(inputs.funding_history_pct) == 40


async def test_build_inputs_survives_a_dead_storage():
    # One dead feed must abstain the families that need it, not abort the
    # evaluation for the families that do not.
    inputs = await _orchestrator(_FailingStorage())._build_portfolio_inputs(Timeframe.INTRADAY)
    assert inputs.closes is None
    assert inputs.funding_rate_pct is None


async def test_build_inputs_handles_missing_funding_column():
    intel = pd.DataFrame({"something_else": [1.0, 2.0]})
    inputs = await _orchestrator(_Storage(intel=intel))._build_portfolio_inputs(Timeframe.INTRADAY)
    assert inputs.funding_rate_pct is None


async def test_build_inputs_with_no_bars_abstains_rather_than_emitting_empty_series():
    inputs = await _orchestrator(_Storage(bars=[]))._build_portfolio_inputs(Timeframe.INTRADAY)
    assert inputs.closes is None


def _wire(monkeypatch, registry: StrategyRegistry, *, enabled: set[str] | None = None) -> None:
    monkeypatch.setattr(orch_mod, "get_default_registry", lambda: registry)
    monkeypatch.setattr(orch_mod, "get_portfolio_runner", lambda: StrategyPortfolioRunner())

    class _KS:
        def enabled_ids(self, ids):
            got = set(ids)
            return got if enabled is None else got & enabled

    monkeypatch.setattr(orch_mod, "get_strategy_kill_switch_manager", lambda: _KS())

    class _Ctl:
        def applied(self):
            return {}

    monkeypatch.setattr(orch_mod, "get_allocation_controller", lambda *_a, **_k: _Ctl())


class _Cfg:
    class strategy_portfolio:  # noqa: N801 - mirrors the settings attribute path
        max_allocation_shift_per_step = 0.10


async def test_evaluate_polls_registered_strategies_and_records_the_result(monkeypatch):
    registry = StrategyRegistry()
    registry.register(BreakoutStrategy(0.15))
    _wire(monkeypatch, registry)

    bars = [_Bar(i) for i in range(120)]
    orch = _orchestrator(_Storage(bars=bars))
    orch._cfg = _Cfg()

    evaluation = await orch._evaluate_strategy_portfolio(Timeframe.INTRADAY)

    assert evaluation is not None
    assert [v.strategy_id for v in evaluation.verdicts] == ["breakout_volume_v1"]
    # Recorded for the API surface, keyed by timeframe.
    assert orch.portfolio_evaluation("15m")["verdicts"][0]["strategy_id"] == "breakout_volume_v1"
    assert "15m" in orch.portfolio_evaluation()


async def test_evaluate_returns_none_on_an_empty_registry(monkeypatch):
    _wire(monkeypatch, StrategyRegistry())
    orch = _orchestrator(_Storage())
    orch._cfg = _Cfg()
    assert await orch._evaluate_strategy_portfolio(Timeframe.INTRADAY) is None


async def test_kill_switched_strategy_is_not_polled(monkeypatch):
    registry = StrategyRegistry()
    registry.register(BreakoutStrategy(0.15))
    _wire(monkeypatch, registry, enabled=set())

    orch = _orchestrator(_Storage(bars=[_Bar(i) for i in range(120)]))
    orch._cfg = _Cfg()
    evaluation = await orch._evaluate_strategy_portfolio(Timeframe.INTRADAY)

    assert evaluation is not None
    assert evaluation.verdicts[0].status is VerdictStatus.DISABLED


async def test_evaluate_never_raises_into_the_tick(monkeypatch):
    def _boom():
        raise RuntimeError("registry exploded")

    monkeypatch.setattr(orch_mod, "get_default_registry", _boom)
    monkeypatch.setattr(orch_mod, "get_portfolio_runner", lambda: StrategyPortfolioRunner())

    orch = _orchestrator(_Storage())
    orch._cfg = _Cfg()
    assert await orch._evaluate_strategy_portfolio(Timeframe.INTRADAY) is None


def test_portfolio_evaluation_is_empty_before_the_first_tick():
    orch = _orchestrator(_Storage())
    assert orch.portfolio_evaluation() == {}
    assert orch.portfolio_evaluation("15m") == {}


def test_portfolio_inputs_defaults_are_all_optional():
    # Every optional field abstains its family rather than failing the build,
    # which is what lets the runner ship before every feed exists.
    inputs = PortfolioInputs(symbol="BTC/USDT", timeframe="15m")
    assert inputs.closes is None
    assert inputs.funding_history_pct is None
    assert inputs.extra == {}


# ------------------------------------------------------- venue quoting


class _Fetcher:
    """Fetcher stub: per-venue price, or an exception to simulate an outage."""

    def __init__(self, prices: dict[str, object]) -> None:
        self._prices = prices
        self.calls: list[str] = []

    async def fetch_ticker_price(self, symbol: str, exchange_id: str = "binance") -> float:
        self.calls.append(exchange_id)
        value = self._prices.get(exchange_id)
        if isinstance(value, Exception):
            raise value
        if value is None:
            raise RuntimeError(f"no price for {exchange_id}")
        return float(value)


def _orch_with_fetcher(fetcher: object) -> Orchestrator:
    orch = _orchestrator(_Storage())
    orch._fetcher = fetcher
    return orch


async def test_venue_prices_quote_every_configured_venue():
    fetcher = _Fetcher({"binance": 100.0, "okx": 101.0})
    prices, stamps = await _orch_with_fetcher(fetcher)._fetch_venue_prices()
    assert prices == {"binance": 100.0, "okx": 101.0}
    assert set(stamps) == {"binance", "okx"}
    assert sorted(fetcher.calls) == ["binance", "okx"]


async def test_one_venue_outage_drops_only_that_venue():
    # An exchange being down must not abstain the whole evaluation, only
    # leave too few venues for the cross-exchange family to pair.
    fetcher = _Fetcher({"binance": 100.0, "okx": RuntimeError("503")})
    prices, stamps = await _orch_with_fetcher(fetcher)._fetch_venue_prices()
    assert prices == {"binance": 100.0}
    assert set(stamps) == {"binance"}


async def test_non_positive_quote_is_discarded():
    fetcher = _Fetcher({"binance": 100.0, "okx": 0.0})
    prices, _ = await _orch_with_fetcher(fetcher)._fetch_venue_prices()
    assert prices == {"binance": 100.0}


async def test_all_venues_down_yields_empty_mapping():
    fetcher = _Fetcher({"binance": RuntimeError("x"), "okx": RuntimeError("y")})
    prices, stamps = await _orch_with_fetcher(fetcher)._fetch_venue_prices()
    assert prices == {}
    assert stamps == {}


async def test_build_inputs_carries_venue_prices():
    orch = _orchestrator(_Storage(bars=[_Bar(i) for i in range(120)]))
    orch._fetcher = _Fetcher({"binance": 100.0, "okx": 101.0})
    inputs = await orch._build_portfolio_inputs(Timeframe.INTRADAY)
    assert inputs.venue_prices == {"binance": 100.0, "okx": 101.0}
    # Concurrent quotes must land close enough together for the builder's
    # staleness guard to accept them.
    assert abs(inputs.venue_price_ts["binance"] - inputs.venue_price_ts["okx"]) < 2.0
