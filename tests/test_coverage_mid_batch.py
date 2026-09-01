"""Residual branches in post-trade analytics, the trade journal, RiskGate's
ADWIN wiring and the diagnostics instrumentation hooks.

The optional dependency here is `river` (not installed in CI, so RiskGate
always sees the disabled-ADWIN path); it is stubbed so the enabled path runs.
The instrumentation install-failure arms are driven by substituting the
threading/multiprocessing modules the functions look at, never by mutating
the real ones.
"""

from __future__ import annotations

import sys
import types

import pytest

from src.execution.router import RouteResult


# ---------------------------------------------------------------------------
# PostTradeAnalytics persistence
# ---------------------------------------------------------------------------


def _route_result(**overrides) -> RouteResult:
    base = dict(
        venue="binance",
        algo="IOC",
        order_id="o1",
        filled_qty=0.1,
        avg_price=50_100.0,
        fee_usd=1.5,
        slippage_bps=20.0,
        success=True,
    )
    return RouteResult(**(base | overrides))


class _RecordingStore:
    def __init__(self) -> None:
        self.rows: list[dict] = []

    def write_feature_log(self, symbol: str, features: dict) -> None:
        self.rows.append({"symbol": symbol, "features": features})


class TestPostTradePersistence:
    def test_a_fill_is_written_to_the_store(self) -> None:
        from src.execution.post_trade import PostTradeAnalytics

        store = _RecordingStore()
        fill = PostTradeAnalytics(store=store).record(
            _route_result(), "BTC/USDT", "buy", 3, 50_000.0, 0.1
        )

        assert len(store.rows) == 1
        assert store.rows[0]["symbol"] == "BTC/USDT"
        features = store.rows[0]["features"]
        assert features["horizon_idx"] == 3.0
        assert features["avg_fill_price"] == pytest.approx(50_100.0)
        assert features["execution_quality_score"] == pytest.approx(fill.execution_quality_score)

    def test_a_failing_store_does_not_break_the_fill(self) -> None:
        from src.execution.post_trade import PostTradeAnalytics

        class _Broken:
            def write_feature_log(self, **_kw):
                raise RuntimeError("duckdb is gone")

        analytics = PostTradeAnalytics(store=_Broken())
        fill = analytics.record(_route_result(), "BTC/USDT", "buy", 0, 50_000.0, 0.1)

        assert fill.symbol == "BTC/USDT"
        assert len(analytics._fill_history) == 1

    def test_venue_summary_is_a_copy(self) -> None:
        from src.execution.post_trade import PostTradeAnalytics

        analytics = PostTradeAnalytics()
        analytics.record(_route_result(), "BTC/USDT", "buy", 0, 50_000.0, 0.1)

        summary = analytics.venue_summary()
        summary.clear()
        assert "binance" in analytics._venue_stats


# ---------------------------------------------------------------------------
# Trade journal helpers
# ---------------------------------------------------------------------------


class TestTradeJournalHelpers:
    def _trade(self, **overrides):
        from src.diagnostics.trade_journal import _regime_label

        assert _regime_label is not None
        return types.SimpleNamespace(**overrides)

    def test_regime_label_is_unknown_when_the_trade_has_no_regime(self) -> None:
        from src.diagnostics.trade_journal import _regime_label

        assert _regime_label(self._trade(regime_at_entry=None)) == "unknown"

    @pytest.mark.parametrize(
        ("regime", "expected"),
        [(0, "ranging"), (1, "trending"), (2, "volatile"), (7, "regime_7")],
    )
    def test_regime_label_maps_known_regimes(self, regime: int, expected: str) -> None:
        from src.diagnostics.trade_journal import _regime_label

        assert _regime_label(self._trade(regime_at_entry=regime)) == expected

    @pytest.mark.parametrize(
        "expected_price",
        [None, 0.0, -10.0],
    )
    def test_slippage_is_none_without_a_usable_expected_price(self, expected_price) -> None:
        from src.diagnostics.trade_journal import _slippage

        assert _slippage(expected_price, 50_000.0, 1, 1_000.0) is None

    def test_slippage_is_none_when_the_actual_price_is_not_positive(self) -> None:
        from src.diagnostics.trade_journal import _slippage

        assert _slippage(50_000.0, 0.0, 1, 1_000.0) is None

    def test_buying_above_the_expected_price_is_adverse_slippage(self) -> None:
        from src.diagnostics.trade_journal import _slippage

        stats = _slippage(50_000.0, 50_500.0, 1, 10_000.0)

        assert stats is not None
        assert stats.slippage_pct == pytest.approx(1.0)
        assert stats.slippage_usd == pytest.approx(100.0)

    def test_the_sign_flips_for_a_short(self) -> None:
        from src.diagnostics.trade_journal import _slippage

        stats = _slippage(50_000.0, 50_500.0, -1, 10_000.0)

        assert stats is not None
        assert stats.slippage_pct == pytest.approx(-1.0)  # sold higher = favourable


# ---------------------------------------------------------------------------
# RiskGate ADWIN wiring
# ---------------------------------------------------------------------------


class _FakeADWIN:
    """Stand-in for river.drift.ADWIN: flags drift once it sees a big jump."""

    def __init__(self, delta: float = 0.002) -> None:
        self.delta = delta
        self.seen: list[float] = []
        self.drift_detected = False

    def update(self, value: float) -> None:
        self.seen.append(value)
        self.drift_detected = value > 10.0


@pytest.fixture
def river_installed(monkeypatch):
    river = types.ModuleType("river")
    drift = types.ModuleType("river.drift")
    drift.ADWIN = _FakeADWIN
    river.drift = drift
    monkeypatch.setitem(sys.modules, "river", river)
    monkeypatch.setitem(sys.modules, "river.drift", drift)
    return drift


class TestRiskGateDrift:
    def test_one_adwin_detector_is_built_per_horizon(self, river_installed) -> None:
        from src.risk.gate import RiskGate

        gate = RiskGate(n_horizons=4, adwin_delta=0.01)

        assert len(gate._adwin) == 4
        assert all(isinstance(a, _FakeADWIN) for a in gate._adwin)
        assert gate._adwin[0].delta == 0.01

    def test_check_drift_feeds_the_detector_and_reports_no_drift(self, river_installed) -> None:
        from src.risk.gate import RiskGate

        gate = RiskGate(n_horizons=2)

        assert gate.check_drift(1, 0.5) is False
        assert gate._adwin[1].seen == [0.5]

    def test_check_drift_reports_a_detected_change(self, river_installed) -> None:
        from src.risk.gate import RiskGate

        gate = RiskGate(n_horizons=2)

        assert gate.check_drift(0, 99.0) is True

    def test_check_drift_is_a_no_op_when_river_is_absent(self) -> None:
        from src.risk.gate import RiskGate

        gate = RiskGate(n_horizons=2)
        # CI has no river, so the detectors are None and drift is never claimed
        assert gate.check_drift(0, 99.0) is False


# ---------------------------------------------------------------------------
# Instrumentation install failures
# ---------------------------------------------------------------------------


class _ImmutableMeta(type):
    def __setattr__(cls, name, value):  # noqa: D105
        raise RuntimeError("class is immutable")


class TestInstrumentationInstallFailures:
    def test_thread_wrapping_is_skipped_when_start_is_missing(self, monkeypatch) -> None:
        from src.diagnostics import instrumentation

        class _NoStart:
            pass

        monkeypatch.setattr(instrumentation, "threading", types.SimpleNamespace(Thread=_NoStart))
        instrumentation._wrap_thread_start()  # must return quietly

    def test_a_thread_class_that_refuses_assignment_is_tolerated(self, monkeypatch) -> None:
        from src.diagnostics import instrumentation

        class _Thread(metaclass=_ImmutableMeta):
            def start(self):
                return "started"

        monkeypatch.setattr(instrumentation, "threading", types.SimpleNamespace(Thread=_Thread))
        instrumentation._wrap_thread_start()  # logs and moves on

    def test_multiprocess_wrapping_is_skipped_when_process_is_missing(self, monkeypatch) -> None:
        from src.diagnostics import instrumentation

        module = types.ModuleType("multiprocessing")
        monkeypatch.setitem(sys.modules, "multiprocessing", module)
        instrumentation._wrap_multiprocess_start()

    def test_a_process_class_that_refuses_assignment_is_tolerated(self, monkeypatch) -> None:
        from src.diagnostics import instrumentation

        class _Process(metaclass=_ImmutableMeta):
            def start(self):
                return "started"

        module = types.ModuleType("multiprocessing")
        module.Process = _Process
        monkeypatch.setitem(sys.modules, "multiprocessing", module)
        instrumentation._wrap_multiprocess_start()
