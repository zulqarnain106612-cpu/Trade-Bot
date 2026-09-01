"""TimescaleBackend query paths driven by a fake asyncpg pool.

tests/test_timescale_storage.py needs the local TimescaleDB container and
skips wholesale without it (which is the case in CI), leaving the row-mapping
and filter-building code unexercised. These tests substitute the pool, so the
SQL construction, parameter numbering and record mapping all run in-process.
"""

from __future__ import annotations

import asyncio

import pytest

from src.data.storage import EquityRecord, ModelMetricsRecord, RegimeSnapshotRecord
from src.data.timescale_storage import TimescaleBackend


class _Conn:
    def __init__(self, *, fetchrow=None, fetch=None, fetchval=None, status="UPDATE 1") -> None:
        self._fetchrow = fetchrow
        self._fetch = fetch if fetch is not None else []
        self._fetchval = fetchval
        self._status = status
        self.calls: list[tuple[str, tuple]] = []

    async def fetchrow(self, query, *params):
        self.calls.append((query, params))
        return self._fetchrow

    async def fetch(self, query, *params):
        self.calls.append((query, params))
        return self._fetch

    async def fetchval(self, query, *params):
        self.calls.append((query, params))
        return self._fetchval

    async def execute(self, query, *params):
        self.calls.append((query, params))
        return self._status


class _Acquire:
    def __init__(self, conn: _Conn) -> None:
        self._conn = conn

    async def __aenter__(self) -> _Conn:
        return self._conn

    async def __aexit__(self, *exc) -> bool:
        return False


class _Pool:
    def __init__(self, conn: _Conn) -> None:
        self._conn = conn

    def acquire(self) -> _Acquire:
        return _Acquire(self._conn)


def _backend(conn: _Conn) -> TimescaleBackend:
    backend = TimescaleBackend.__new__(TimescaleBackend)
    backend._pool = _Pool(conn)
    backend._require_pool = lambda: backend._pool
    import structlog

    backend._log = structlog.get_logger("test")
    return backend


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Trades
# ---------------------------------------------------------------------------


def test_closing_a_trade_that_is_not_open_is_an_error():
    conn = _Conn(status="UPDATE 0")
    backend = _backend(conn)

    with pytest.raises(ValueError, match="No open trade found"):
        _run(
            backend.update_trade_exit(
                trade_id="t-1",
                exit_price=50_000.0,
                exit_ts=1_700_000_000_000,
                pnl_usd=10.0,
                pnl_pct=0.01,
                exit_reason="take_profit",
                fee_usd=0.5,
            )
        )


def test_the_ensemble_fields_are_patched_onto_an_existing_trade():
    conn = _Conn()
    backend = _backend(conn)

    _run(backend.update_trade_ensemble_fields("t-1", 0.61, 0.3))

    query, params = conn.calls[0]
    assert "UPDATE trades SET ensemble_point_estimate" in query
    assert params == (0.61, 0.3, "t-1")


def test_fetch_trades_numbers_every_filter_it_is_given():
    conn = _Conn(fetch=[])
    backend = _backend(conn)

    _run(
        backend.fetch_trades(
            symbol="BTC/USDT",
            trading_mode="paper",
            since_ts=1_700_000_000_000,
            open_only=True,
            limit=10,
            offset=5,
        )
    )

    query, params = conn.calls[0]
    assert "symbol=$1" in query
    assert "trading_mode=$2" in query
    assert "entry_ts>=$3" in query
    assert "exit_ts IS NULL" in query  # a literal, never a bound parameter
    assert params[:3] == ("BTC/USDT", "paper", 1_700_000_000_000)


def test_fetch_trades_without_filters_builds_no_where_clause():
    conn = _Conn(fetch=[])
    backend = _backend(conn)

    _run(backend.fetch_trades())

    query, _ = conn.calls[0]
    assert "WHERE" not in query


def test_the_loss_streak_stops_at_the_first_non_losing_trade():
    conn = _Conn(
        fetch=[
            {"pnl_usd": -5.0},
            {"pnl_usd": -2.0},
            {"pnl_usd": 3.0},  # breaks the streak
            {"pnl_usd": -9.0},
        ]
    )
    backend = _backend(conn)

    assert _run(backend.count_consecutive_losses("BTC/USDT", "paper")) == 2


def test_a_trade_with_no_pnl_recorded_ends_the_streak():
    conn = _Conn(fetch=[{"pnl_usd": -5.0}, {"pnl_usd": None}])
    backend = _backend(conn)

    assert _run(backend.count_consecutive_losses("BTC/USDT", "paper")) == 1


# ---------------------------------------------------------------------------
# Regime snapshots
# ---------------------------------------------------------------------------


_REGIME_ROW = {
    "symbol": "BTC/USDT",
    "timeframe": "15m",
    "ts": 1_700_000_000_000,
    "regime_state": 1,
    "prob_ranging": 0.2,
    "prob_trending": 0.7,
    "prob_volatile": 0.1,
    "changepoint_probability": 0.05,
    "agreement_score": 0.9,
}


def test_latest_regime_maps_the_row_onto_a_record():
    backend = _backend(_Conn(fetchrow=_REGIME_ROW))

    snap = _run(backend.latest_regime("BTC/USDT", "15m"))

    assert isinstance(snap, RegimeSnapshotRecord)
    assert snap.regime_state == 1
    assert snap.agreement_score == 0.9


def test_latest_regime_is_none_when_nothing_is_stored():
    assert _run(_backend(_Conn(fetchrow=None)).latest_regime("BTC/USDT", "15m")) is None


def test_regime_snapshot_before_maps_the_row_onto_a_record():
    backend = _backend(_Conn(fetchrow=_REGIME_ROW))

    snap = _run(backend.regime_snapshot_before("BTC/USDT", "15m", 1_700_000_100_000))

    assert snap.prob_trending == 0.7


def test_regime_snapshot_before_is_none_when_nothing_precedes_the_timestamp():
    backend = _backend(_Conn(fetchrow=None))

    assert _run(backend.regime_snapshot_before("BTC/USDT", "15m", 1)) is None


# ---------------------------------------------------------------------------
# Model metrics / live gate
# ---------------------------------------------------------------------------


def _metrics_row(**overrides) -> dict:
    base = {
        "model_name": "direction",
        "timeframe": "15m",
        "version": "v1",
        "oos_sharpe": 1.5,
        "max_drawdown": 0.08,
        "n_trades": 400,
        "accuracy": 0.6,
        "precision_score": 0.6,
        "recall_score": 0.6,
        "f1_score": 0.6,
        "live_gate_pass": True,
    }
    return base | overrides


def test_latest_model_metrics_maps_the_row_onto_a_record():
    backend = _backend(_Conn(fetchrow=_metrics_row()))

    metrics = _run(backend.latest_model_metrics("direction", "15m"))

    assert isinstance(metrics, ModelMetricsRecord)
    assert metrics.oos_sharpe == 1.5
    assert metrics.live_gate_pass is True


def test_latest_model_metrics_is_none_when_the_model_has_never_been_trained():
    assert _run(_backend(_Conn(fetchrow=None)).latest_model_metrics("direction", "15m")) is None


def test_the_live_gate_needs_both_models_to_pass():
    backend = _backend(_Conn(fetchrow=_metrics_row()))
    assert _run(backend.live_gate_passes("15m")) is True

    backend = _backend(_Conn(fetchrow=_metrics_row(live_gate_pass=False)))
    assert _run(backend.live_gate_passes("15m")) is False


def test_the_live_gate_fails_closed_when_a_model_has_no_metrics():
    assert _run(_backend(_Conn(fetchrow=None)).live_gate_passes("15m")) is False


# ---------------------------------------------------------------------------
# Equity curve
# ---------------------------------------------------------------------------


_EQUITY_ROW = {
    "ts": 1_700_000_000_000,
    "trading_mode": "paper",
    "equity_usd": 10_500.0,
    "cash_usd": 9_000.0,
    "unrealized_pnl": 500.0,
    "daily_pnl_usd": 50.0,
    "daily_pnl_pct": 0.005,
    "peak_equity_usd": 11_000.0,
    "drawdown_pct": 0.045,
}


def test_the_equity_curve_query_is_time_bounded_when_a_start_is_given():
    conn = _Conn(fetch=[_EQUITY_ROW])
    backend = _backend(conn)

    curve = _run(backend.fetch_equity_curve("paper", since_ts=1_700_000_000_000, limit=5))

    query, params = conn.calls[0]
    assert "ts>=$2" in query
    assert params == ("paper", 1_700_000_000_000, 5)
    assert curve[0].equity_usd == 10_500.0


def test_the_equity_curve_query_drops_the_time_bound_when_none_is_given():
    conn = _Conn(fetch=[_EQUITY_ROW])
    backend = _backend(conn)

    _run(backend.fetch_equity_curve("paper", limit=5))

    query, params = conn.calls[0]
    assert "ts>=" not in query
    assert params == ("paper", 5)


def test_latest_equity_maps_the_row_onto_a_record():
    backend = _backend(_Conn(fetchrow=_EQUITY_ROW))

    record = _run(backend.latest_equity("paper"))

    assert isinstance(record, EquityRecord)
    assert record.drawdown_pct == 0.045


def test_latest_equity_is_none_before_the_first_snapshot():
    assert _run(_backend(_Conn(fetchrow=None)).latest_equity("paper")) is None


# ---------------------------------------------------------------------------
# Symbol validation and health check
# ---------------------------------------------------------------------------


def test_a_malformed_symbol_is_rejected_before_any_query_runs():
    conn = _Conn()
    backend = _backend(conn)

    with pytest.raises(ValueError, match="Invalid symbol format"):
        _run(backend.validate_symbol("DROP TABLE bars"))

    assert conn.calls == []


def test_health_check_counts_every_allowlisted_table():
    from src.data.timescale_storage import _ALLOWED_TABLES

    backend = _backend(_Conn(fetchval=7))

    counts = _run(backend.health_check())

    assert set(counts) == set(_ALLOWED_TABLES)
    assert all(v == 7 for v in counts.values())


def test_health_check_refuses_a_table_name_it_cannot_vouch_for(monkeypatch):
    from src.data import timescale_storage as mod

    monkeypatch.setattr(mod, "_ALLOWED_TABLES", ("bars; DROP TABLE trades",))
    backend = _backend(_Conn(fetchval=1))

    with pytest.raises(RuntimeError, match="unsafe characters"):
        _run(backend.health_check())
