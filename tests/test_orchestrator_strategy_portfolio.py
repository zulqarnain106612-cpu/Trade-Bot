"""
Tests for the orchestrator's strategy-portfolio wiring.

The registry was populated, weighted and kill-switchable long before
anything called generate_signal(). These tests pin the hand-off that closes
that gap, and — just as importantly — that the hand-off cannot take a tick
down: the incumbent signal has already cleared its gates by the time the
portfolio is polled.
"""

from __future__ import annotations

from datetime import UTC, datetime

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
        self.bar_query = {"since_ts": since_ts, "limit": limit}
        return self._bars

    async def fetch_intelligence_features(self, symbol, timeframe, since_ts=0, limit=100_000):
        self.intel_query = {"since_ts": since_ts, "limit": limit}
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
    orch._last_portfolio_peer_evaluation = {}
    orch._last_portfolio_agreement = {}
    orch._fetcher = _Fetcher({})
    orch._quote_cache = {}
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
        basis_perp_symbol = ""


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


# ------------------------------------------------- peer view / agreement


class _TwoWay:
    def __init__(self, sid: str, direction: int) -> None:
        self.strategy_id = sid
        self._direction = direction

    def generate_signal(self, bar: object) -> object:
        from src.strategies.registry import Signal

        return Signal(direction=self._direction, confidence=0.9, regime_fit=0.9)

    def required_capital_fraction(self) -> float:
        return 0.3


def _portfolio_orch(monkeypatch, registry: StrategyRegistry) -> Orchestrator:
    from src.engine.strategy_portfolio import build_signal_engine_context

    builders = {s.strategy_id: build_signal_engine_context for s in registry.all()}
    monkeypatch.setattr(orch_mod, "get_default_registry", lambda: registry)
    monkeypatch.setattr(
        orch_mod,
        "get_portfolio_runner",
        lambda: StrategyPortfolioRunner(builders=builders),
    )

    class _KS:
        def enabled_ids(self, ids):
            return set(ids)

    monkeypatch.setattr(orch_mod, "get_strategy_kill_switch_manager", lambda: _KS())

    class _Ctl:
        def applied(self):
            return {}

    monkeypatch.setattr(orch_mod, "get_allocation_controller", lambda *_a, **_k: _Ctl())

    orch = _orchestrator(_Storage())
    orch._cfg = _Cfg()
    return orch


async def test_peer_view_excludes_the_incumbent(monkeypatch):
    # The incumbent must not confirm itself: with only signal_engine_v1
    # registered, the peer view has no voters at all.
    registry = StrategyRegistry()
    registry.register(_TwoWay("signal_engine_v1", 1))
    orch = _portfolio_orch(monkeypatch, registry)

    await orch._evaluate_strategy_portfolio(Timeframe.INTRADAY)

    payload = orch.portfolio_evaluation("15m")
    assert payload["direction"] == 1
    assert payload["peer"]["voting"] == []
    # No peers means no evidence, so no reduction.
    assert payload["agreement_scalar"] == 1.0


async def test_opposing_peers_produce_a_reduction(monkeypatch):
    registry = StrategyRegistry()
    registry.register(_TwoWay("signal_engine_v1", 1))
    registry.register(_TwoWay("peer_a", -1))
    registry.register(_TwoWay("peer_b", -1))
    orch = _portfolio_orch(monkeypatch, registry)

    await orch._evaluate_strategy_portfolio(Timeframe.INTRADAY)

    payload = orch.portfolio_evaluation("15m")
    assert payload["peer"]["direction"] == -1
    assert 0.0 < payload["agreement_scalar"] < 1.0


async def test_agreeing_peers_produce_no_reduction(monkeypatch):
    registry = StrategyRegistry()
    registry.register(_TwoWay("signal_engine_v1", 1))
    registry.register(_TwoWay("peer_a", 1))
    registry.register(_TwoWay("peer_b", 1))
    orch = _portfolio_orch(monkeypatch, registry)

    await orch._evaluate_strategy_portfolio(Timeframe.INTRADAY)

    assert orch.portfolio_evaluation("15m")["agreement_scalar"] == 1.0


# --------------------------------------------------- query bounds


async def test_bar_window_is_selected_by_since_ts_not_by_limit_alone():
    # fetch_bars is `ts >= since_ts ORDER BY ts ASC LIMIT n`. A limit with
    # since_ts=0 returns the OLDEST rows, so the portfolio would evaluate the
    # start of history forever — stale by years, and never raising.
    storage = _Storage(bars=[_Bar(i) for i in range(120)])
    orch = _orchestrator(storage)
    await orch._build_portfolio_inputs(Timeframe.INTRADAY)

    now_ms = datetime.now(tz=UTC).timestamp() * 1000
    assert storage.bar_query["since_ts"] > 0
    # Window must reach back from now, not forward from epoch.
    assert storage.bar_query["since_ts"] < now_ms
    assert now_ms - storage.bar_query["since_ts"] < 400 * 24 * 3600 * 1000


async def test_intelligence_query_is_bounded():
    # Unbounded this rebuilt the whole intelligence history into a DataFrame
    # every tick of every timeframe to read ~120 funding observations.
    storage = _Storage(intel=pd.DataFrame({"intelligence_binance_funding_rate_pct": [0.01] * 5}))
    orch = _orchestrator(storage)
    await orch._build_portfolio_inputs(Timeframe.INTRADAY)

    assert storage.intel_query["limit"] < 100_000
    assert storage.intel_query["since_ts"] > 0


async def test_funding_lookback_is_calendar_based_not_bar_based():
    # Funding steps every 8h. A bar-count window would span ~3 days at 15m
    # and hand the carry family a near-constant series.
    storage = _Storage(intel=pd.DataFrame({"intelligence_binance_funding_rate_pct": [0.01] * 5}))
    orch = _orchestrator(storage)
    await orch._build_portfolio_inputs(Timeframe.INTRADAY)
    fast = storage.intel_query["since_ts"]

    storage2 = _Storage(intel=pd.DataFrame({"intelligence_binance_funding_rate_pct": [0.01] * 5}))
    orch2 = _orchestrator(storage2)
    await orch2._build_portfolio_inputs(Timeframe.SWING)

    # Same calendar span regardless of timeframe (allow a second of clock drift).
    assert abs(fast - storage2.intel_query["since_ts"]) < 2_000


# ------------------------------------------------------------ basis legs


class _CfgWithPerp:
    class strategy_portfolio:  # noqa: N801 - mirrors the settings attribute path
        max_allocation_shift_per_step = 0.10
        basis_perp_symbol = "BTC/USDT:USDT"


class _CfgNoPerp:
    class strategy_portfolio:  # noqa: N801 - mirrors the settings attribute path
        max_allocation_shift_per_step = 0.10
        basis_perp_symbol = ""


async def test_basis_legs_abstain_when_no_perp_is_configured():
    # The spot/perp mapping is venue- and settlement-specific; guessing it
    # would price one instrument's basis against another.
    orch = _orchestrator(_Storage())
    orch._cfg = _CfgNoPerp()
    orch._fetcher = _Fetcher({"binance": 100.0})
    assert await orch._fetch_basis_legs() == (None, None, None, None)


class _SymbolFetcher:
    """Prices keyed by symbol rather than venue, for the basis path."""

    def __init__(self, prices: dict[str, object]) -> None:
        self._prices = prices
        self.symbols: list[str] = []

    async def fetch_ticker_price(self, symbol: str, exchange_id: str = "binance") -> float:
        self.symbols.append(symbol)
        value = self._prices.get(symbol)
        if isinstance(value, Exception):
            raise value
        if value is None:
            raise RuntimeError(f"no price for {symbol}")
        return float(value)


async def test_basis_legs_are_quoted_together_from_one_venue():
    fetcher = _SymbolFetcher({"BTC/USDT": 100.0, "BTC/USDT:USDT": 101.0})
    orch = _orchestrator(_Storage())
    orch._cfg = _CfgWithPerp()
    orch._fetcher = fetcher

    spot, spot_ts, perp, perp_ts = await orch._fetch_basis_legs()
    assert (spot, perp) == (100.0, 101.0)
    assert sorted(fetcher.symbols) == ["BTC/USDT", "BTC/USDT:USDT"]
    # Concurrent, so the builder's skew guard will accept the pair.
    assert abs(spot_ts - perp_ts) < 2.0


async def test_a_missing_leg_abstains_the_whole_pair():
    # Half a basis is not a basis; returning the spot leg alone would let a
    # later change pair it against something else.
    orch = _orchestrator(_Storage())
    orch._cfg = _CfgWithPerp()
    orch._fetcher = _SymbolFetcher({"BTC/USDT": 100.0, "BTC/USDT:USDT": RuntimeError("down")})
    assert await orch._fetch_basis_legs() == (None, None, None, None)


# ------------------------------------------------------ pair cointegration


class _Cache:
    """UniverseReturnsCache stand-in exposing only what _pair_series reads."""

    def __init__(self, series: dict[str, tuple[float, ...]], fetched_at: float = 100.0) -> None:
        self._series = series
        self.fetched_at = fetched_at

    def close_series(self, symbol: str):
        return self._series.get(symbol)


class _CfgPair:
    class strategy_portfolio:  # noqa: N801 - mirrors the settings attribute path
        max_allocation_shift_per_step = 0.10
        basis_perp_symbol = ""
        mean_reversion_pair = ["A/USDT", "B/USDT"]
        mean_reversion_window = 30


def _pair_orch(series: dict[str, tuple[float, ...]], fetched_at: float = 100.0) -> Orchestrator:
    orch = _orchestrator(_Storage())
    orch._cfg = _CfgPair()
    orch._universe_returns = _Cache(series, fetched_at)
    orch._pair_cointegration = None
    return orch


def _cointegrated(n: int = 200) -> dict[str, tuple[float, ...]]:
    # B is A plus a small stationary wobble: cointegrated by construction.
    a = [100.0 + i * 0.1 for i in range(n)]
    b = [x * 0.5 + (0.3 if i % 2 else -0.3) for i, x in enumerate(a)]
    return {"A/USDT": tuple(a), "B/USDT": tuple(b)}


def test_pair_series_abstains_when_unconfigured():
    orch = _orchestrator(_Storage())
    orch._cfg = _Cfg()
    orch._cfg.strategy_portfolio.mean_reversion_pair = []
    orch._universe_returns = _Cache({})
    orch._pair_cointegration = None
    assert orch._pair_series() == (None, None, None)


def test_pair_series_abstains_before_the_first_refresh():
    assert _pair_orch(_cointegrated(), fetched_at=0.0)._pair_series() == (None, None, None)


def test_pair_series_abstains_when_a_leg_is_missing():
    series = _cointegrated()
    del series["B/USDT"]
    assert _pair_orch(series)._pair_series() == (None, None, None)


def test_pair_series_abstains_on_unequal_history():
    # One leg listed later, or a gap on one venue. Truncating to the shorter
    # would silently align two different date ranges.
    series = _cointegrated()
    series["B/USDT"] = series["B/USDT"][:-5]
    assert _pair_orch(series)._pair_series() == (None, None, None)


def test_pair_series_returns_legs_and_hedge_ratio_when_cointegrated():
    a, b, ratio = _pair_orch(_cointegrated())._pair_series()
    assert a is not None and b is not None
    assert len(a) == len(b)
    assert ratio is not None


def test_cointegration_is_memoised_against_the_snapshot():
    # One OLS per data refresh, not one per tick.
    orch = _pair_orch(_cointegrated())
    orch._pair_series()
    first = orch._pair_cointegration
    orch._pair_series()
    assert orch._pair_cointegration is first


def test_a_new_snapshot_retests_cointegration():
    # Pair relationships decohere; assuming the test result forever is the
    # characteristic way this family loses money.
    orch = _pair_orch(_cointegrated())
    orch._pair_series()
    stamp_before = orch._pair_cointegration[0]
    orch._universe_returns.fetched_at = stamp_before + 1.0
    orch._pair_series()
    assert orch._pair_cointegration[0] == stamp_before + 1.0


def test_a_failing_cointegration_test_abstains_and_is_not_retried_per_tick():
    orch = _pair_orch(_cointegrated())
    import src.engine.orchestrator as mod

    calls = []

    def _boom(*_a, **_k):
        calls.append(1)
        raise RuntimeError("statsmodels exploded")

    original = mod.check_cointegration
    mod.check_cointegration = _boom
    try:
        assert orch._pair_series() == (None, None, None)
        assert orch._pair_series() == (None, None, None)
    finally:
        mod.check_cointegration = original
    assert len(calls) == 1


# --------------------------------------------------- quote memoisation


class _CfgQuotes:
    class strategy_portfolio:  # noqa: N801 - mirrors the settings attribute path
        max_allocation_shift_per_step = 0.10
        basis_perp_symbol = "BTC/USDT:USDT"
        mean_reversion_pair = []
        mean_reversion_window = 30
        basis_days_to_convergence = 1.0


async def test_the_same_quote_is_not_requested_twice_in_one_tick():
    # _fetch_venue_prices needs BTC/USDT on binance for the cross-exchange
    # family; _fetch_basis_legs needed the identical quote for the basis
    # spot leg. That was two round-trips per tick per timeframe against a
    # rate limit shared with the order path.
    fetcher = _SymbolFetcher(
        {"BTC/USDT": 100.0, "BTC/USDT:USDT": 101.0}
    )
    orch = _orchestrator(_Storage())
    orch._cfg = _CfgQuotes()
    orch._fetcher = fetcher

    await orch._fetch_venue_prices()
    await orch._fetch_basis_legs()

    assert fetcher.symbols.count("BTC/USDT") == 1


async def test_a_cached_quote_keeps_its_original_observation_time():
    # This is what makes the cache safe rather than merely cheap: the skew
    # guard measures the real age of the data, so a quote that ever did go
    # stale abstains the family instead of being served as fresh.
    fetcher = _SymbolFetcher({"BTC/USDT": 100.0})
    orch = _orchestrator(_Storage())
    orch._cfg = _CfgQuotes()
    orch._fetcher = fetcher

    first = await orch._quote("BTC/USDT", "binance")
    second = await orch._quote("BTC/USDT", "binance")
    assert first is not None and second is not None
    assert second[1] == first[1]


async def test_an_expired_quote_is_refetched():
    fetcher = _SymbolFetcher({"BTC/USDT": 100.0})
    orch = _orchestrator(_Storage())
    orch._cfg = _CfgQuotes()
    orch._fetcher = fetcher

    await orch._quote("BTC/USDT", "binance")
    # Age the entry past the TTL.
    price, ts = orch._quote_cache[("BTC/USDT", "binance")]
    orch._quote_cache[("BTC/USDT", "binance")] = (price, ts - 60.0)
    await orch._quote("BTC/USDT", "binance")

    assert fetcher.symbols.count("BTC/USDT") == 2


async def test_different_venues_are_cached_separately():
    # Same symbol, two venues, two distinct prices — collapsing them would
    # make the cross-exchange basis identically zero.
    orch = _orchestrator(_Storage())
    orch._cfg = _CfgQuotes()
    orch._fetcher = _Fetcher({"binance": 100.0, "okx": 101.0})

    prices, _ = await orch._fetch_venue_prices()
    assert prices == {"binance": 100.0, "okx": 101.0}


async def test_a_failed_quote_is_not_cached():
    # Caching a failure would suppress the retry on the next tick.
    fetcher = _SymbolFetcher({"BTC/USDT": RuntimeError("503")})
    orch = _orchestrator(_Storage())
    orch._cfg = _CfgQuotes()
    orch._fetcher = fetcher

    assert await orch._quote("BTC/USDT", "binance") is None
    assert await orch._quote("BTC/USDT", "binance") is None
    assert fetcher.symbols.count("BTC/USDT") == 2


async def test_the_quote_cache_is_bounded_by_symbol_and_venue():
    # Keys are (symbol, venue), so the cache cannot grow with uptime.
    orch = _orchestrator(_Storage())
    orch._cfg = _CfgQuotes()
    orch._fetcher = _SymbolFetcher({"BTC/USDT": 100.0, "BTC/USDT:USDT": 101.0})

    for _ in range(50):
        orch._quote_cache.clear()
        await orch._fetch_basis_legs()
    assert len(orch._quote_cache) <= 2


# ------------------------------------------------- demand-driven assembly


class _CountingStorage(_Storage):
    def __init__(self, **kw) -> None:
        super().__init__(**kw)
        self.bar_calls = 0
        self.intel_calls = 0

    async def fetch_bars(self, symbol, timeframe, since_ts=0, limit=0):
        self.bar_calls += 1
        return await super().fetch_bars(symbol, timeframe, since_ts, limit)

    async def fetch_intelligence_features(self, symbol, timeframe, since_ts=0, limit=100_000):
        self.intel_calls += 1
        return await super().fetch_intelligence_features(symbol, timeframe, since_ts, limit)


async def test_the_default_configuration_costs_no_io():
    # Only signal_engine_v1 is registered by default and its builder consumes
    # nothing, so assembling inputs must touch neither the database nor the
    # exchange. This runs in front of order routing on every tick.
    from src.engine.strategy_portfolio import required_inputs

    storage = _CountingStorage(bars=[_Bar(i) for i in range(120)])
    fetcher = _SymbolFetcher({"BTC/USDT": 100.0, "BTC/USDT:USDT": 101.0})
    orch = _orchestrator(storage)
    orch._cfg = _CfgQuotes()
    orch._fetcher = fetcher

    await orch._build_portfolio_inputs(
        Timeframe.INTRADAY, required_inputs(["signal_engine_v1"])
    )

    assert storage.bar_calls == 0
    assert storage.intel_calls == 0
    assert fetcher.symbols == []


async def test_only_the_declared_feed_is_fetched():
    from src.engine.strategy_portfolio import required_inputs

    storage = _CountingStorage(bars=[_Bar(i) for i in range(120)])
    orch = _orchestrator(storage)
    orch._cfg = _CfgQuotes()
    orch._fetcher = _SymbolFetcher({"BTC/USDT": 100.0})

    inputs = await orch._build_portfolio_inputs(
        Timeframe.INTRADAY, required_inputs(["breakout_volume_v1"])
    )

    assert storage.bar_calls == 1
    assert storage.intel_calls == 0
    assert inputs.closes is not None
    assert inputs.venue_prices == {}


async def test_omitting_needs_still_fetches_everything():
    # Backwards compatible for any caller with no view of what is registered.
    storage = _CountingStorage(bars=[_Bar(i) for i in range(120)])
    orch = _orchestrator(storage)
    orch._cfg = _CfgQuotes()
    orch._fetcher = _SymbolFetcher({"BTC/USDT": 100.0, "BTC/USDT:USDT": 101.0})

    await orch._build_portfolio_inputs(Timeframe.INTRADAY)

    assert storage.bar_calls == 1
    assert storage.intel_calls == 1


class _SnapshotCache:
    """Universe cache stub that records refresh order against _pair_series."""

    def __init__(self, series: dict[str, tuple[float, ...]]) -> None:
        self._series = series
        self.fetched_at = 0.0
        self.refreshes = 0

    async def trailing_returns(self):
        self.refreshes += 1
        self.fetched_at = 100.0
        return {k: 0.1 for k in self._series}

    def close_series(self, symbol: str):
        # Only readable once a refresh has populated the snapshot — which is
        # exactly the dependency the needs gate has to express.
        return self._series.get(symbol) if self.fetched_at > 0.0 else None


class _CfgPairOnly:
    class strategy_portfolio:  # noqa: N801 - mirrors the settings attribute path
        max_allocation_shift_per_step = 0.10
        basis_perp_symbol = ""
        basis_days_to_convergence = 1.0
        mean_reversion_pair = ["A/USDT", "B/USDT"]
        mean_reversion_window = 30


def _pair_series_fixture() -> dict[str, tuple[float, ...]]:
    a = [100.0 + i * 0.1 for i in range(200)]
    b = [x * 0.5 + (0.3 if i % 2 else -0.3) for i, x in enumerate(a)]
    return {"A/USDT": tuple(a), "B/USDT": tuple(b)}


async def test_the_pair_family_alone_still_refreshes_the_shared_snapshot():
    # Gating the refresh on UNIVERSE alone left a book with only mean
    # reversion enabled reading a cache nothing ever filled — abstaining
    # forever for a reason no config change could fix.
    from src.engine.strategy_portfolio import required_inputs

    cache = _SnapshotCache(_pair_series_fixture())
    orch = _orchestrator(_Storage())
    orch._cfg = _CfgPairOnly()
    orch._universe_returns = cache
    orch._pair_cointegration = None

    inputs = await orch._build_portfolio_inputs(
        Timeframe.INTRADAY, required_inputs(["mean_reversion_pairs_v1"])
    )

    assert cache.refreshes == 1
    assert inputs.pair_closes_a is not None
    assert inputs.pair_hedge_ratio is not None


async def test_the_pair_reads_the_snapshot_on_the_very_first_tick():
    # _pair_series memoises cointegration against the snapshot timestamp, so
    # it must run after the refresh, not before it.
    from src.engine.strategy_portfolio import required_inputs

    cache = _SnapshotCache(_pair_series_fixture())
    orch = _orchestrator(_Storage())
    orch._cfg = _CfgPairOnly()
    orch._universe_returns = cache
    orch._pair_cointegration = None

    inputs = await orch._build_portfolio_inputs(
        Timeframe.INTRADAY, required_inputs(["mean_reversion_pairs_v1"])
    )
    # Would be None if _pair_series ran against an unpopulated cache.
    assert inputs.pair_closes_b is not None


async def test_pair_only_does_not_expose_universe_returns():
    from src.engine.strategy_portfolio import required_inputs

    cache = _SnapshotCache(_pair_series_fixture())
    orch = _orchestrator(_Storage())
    orch._cfg = _CfgPairOnly()
    orch._universe_returns = cache
    orch._pair_cointegration = None

    inputs = await orch._build_portfolio_inputs(
        Timeframe.INTRADAY, required_inputs(["mean_reversion_pairs_v1"])
    )
    assert inputs.universe_returns == {}


async def test_neither_family_leaves_the_snapshot_untouched():
    from src.engine.strategy_portfolio import required_inputs

    cache = _SnapshotCache(_pair_series_fixture())
    orch = _orchestrator(_Storage())
    orch._cfg = _CfgPairOnly()
    orch._universe_returns = cache
    orch._pair_cointegration = None

    await orch._build_portfolio_inputs(
        Timeframe.INTRADAY, required_inputs(["signal_engine_v1"])
    )
    assert cache.refreshes == 0


# ------------------------------------------------- agreement staleness


async def test_a_failed_evaluation_clears_last_tick_scalar(monkeypatch):
    # The submission path reads _last_portfolio_agreement unconditionally, so
    # a leftover value would shrink a NEW trade using peer opinions from an
    # older bar — indefinitely, if evaluation kept failing.
    def _boom():
        raise RuntimeError("registry exploded")

    monkeypatch.setattr(orch_mod, "get_default_registry", _boom)
    monkeypatch.setattr(orch_mod, "get_portfolio_runner", lambda: StrategyPortfolioRunner())

    orch = _orchestrator(_Storage())
    orch._cfg = _Cfg()
    orch._last_portfolio_agreement["15m"] = 0.4

    assert await orch._evaluate_strategy_portfolio(Timeframe.INTRADAY) is None
    assert "15m" not in orch._last_portfolio_agreement


async def test_an_empty_registry_clears_last_tick_scalar(monkeypatch):
    _wire(monkeypatch, StrategyRegistry())
    orch = _orchestrator(_Storage())
    orch._cfg = _Cfg()
    orch._last_portfolio_agreement["15m"] = 0.4

    assert await orch._evaluate_strategy_portfolio(Timeframe.INTRADAY) is None
    assert "15m" not in orch._last_portfolio_agreement


async def test_a_cleared_scalar_reads_as_no_reduction(monkeypatch):
    # Failing open: an absent scalar must mean 1.0 at the submission site.
    _wire(monkeypatch, StrategyRegistry())
    orch = _orchestrator(_Storage())
    orch._cfg = _Cfg()
    orch._last_portfolio_agreement["15m"] = 0.4

    await orch._evaluate_strategy_portfolio(Timeframe.INTRADAY)

    assert orch._last_portfolio_agreement.get("15m", 1.0) == 1.0


async def test_one_timeframe_failing_does_not_clear_another(monkeypatch):
    _wire(monkeypatch, StrategyRegistry())
    orch = _orchestrator(_Storage())
    orch._cfg = _Cfg()
    orch._last_portfolio_agreement["15m"] = 0.4
    orch._last_portfolio_agreement["4h"] = 0.7

    await orch._evaluate_strategy_portfolio(Timeframe.INTRADAY)

    assert "15m" not in orch._last_portfolio_agreement
    assert orch._last_portfolio_agreement["4h"] == 0.7


# ------------------------------------------------- pair snapshot staleness


def test_a_stale_snapshot_abstains_the_pair():
    # The universe cache serves stale data through an outage on purpose — a
    # 30-day trailing return survives that. A spread z-score does not: on
    # hours-old closes it can signal a divergence that has already closed.
    import time as _time

    orch = _pair_orch(_cointegrated())
    orch._universe_returns.fetched_at = _time.monotonic() - (5 * 3600)

    assert orch._pair_series() == (None, None, None)


def test_a_fresh_snapshot_still_serves_the_pair():
    import time as _time

    orch = _pair_orch(_cointegrated())
    orch._universe_returns.fetched_at = _time.monotonic() - 60.0

    a, b, ratio = orch._pair_series()
    assert a is not None and b is not None and ratio is not None


def test_the_bound_does_not_bite_during_normal_backoff():
    # Cache TTL is an hour and its failure backoff caps at 15 minutes, so a
    # degraded-but-working feed must still feed the pair.
    import time as _time

    orch = _pair_orch(_cointegrated())
    orch._universe_returns.fetched_at = _time.monotonic() - (3600 + 900)

    a, _b, _r = orch._pair_series()
    assert a is not None


# ------------------------------------------------- exchange filters


class _PrecisionFetcher(_SymbolFetcher):
    def __init__(self, precision: object) -> None:
        super().__init__({})
        self._precision = precision

    async def fetch_symbol_precision(self, symbol: str, exchange_id: str = "binance"):
        if isinstance(self._precision, Exception):
            raise self._precision
        return self._precision


async def test_startup_loads_the_real_exchange_filters():
    # fetch_symbol_precision was built, tested, and had no production caller,
    # so min_amount/min_cost ran at 0.0 and sub-minimum orders were never
    # rejected by the sizing path.
    orch = _orchestrator(_Storage())
    orch._fetcher = _PrecisionFetcher(
        {"amount_precision": 3.0, "min_amount": 0.001, "min_cost": 10.0}
    )

    await orch._load_symbol_precision()

    assert orch._symbol_precision["min_cost"] == 10.0
    assert orch._symbol_precision["amount_precision"] == 3.0


async def test_a_failed_precision_fetch_falls_back_to_todays_defaults():
    # Must reproduce existing behaviour exactly, so the change can only ever
    # tighten sizing and never break startup.
    orch = _orchestrator(_Storage())
    orch._fetcher = _PrecisionFetcher(RuntimeError("markets unavailable"))

    await orch._load_symbol_precision()

    assert orch._symbol_precision == {
        "amount_precision": 8.0,
        "min_amount": 0.0,
        "min_cost": 0.0,
    }


async def test_a_partial_precision_response_keeps_defaults_for_missing_keys():
    orch = _orchestrator(_Storage())
    orch._fetcher = _PrecisionFetcher({"min_cost": 5.0})

    await orch._load_symbol_precision()

    assert orch._symbol_precision["min_cost"] == 5.0
    assert orch._symbol_precision["amount_precision"] == 8.0
