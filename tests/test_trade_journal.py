"""Tests for src/diagnostics/trade_journal.py"""

from __future__ import annotations

import pytest

from src.data.storage import TradeRecord
from src.diagnostics.trade_journal import (
    JournalEntry,
    build_entry,
    build_journal,
    summarise_journal,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _trade(
    id: str = "t1",
    symbol: str = "BTC/USDT",
    timeframe: str = "1h",
    direction: int = 1,
    entry_price: float = 50_000.0,
    exit_price: float | None = 51_000.0,
    pnl_usd: float | None = 100.0,
    fee_usd: float = 5.0,
    regime_at_entry: int = 1,
    exit_reason: str | None = "take_profit",
    raw_signal: float = 0.8,
    meta_label_prob: float = 0.7,
) -> TradeRecord:
    return TradeRecord(
        id=id,
        symbol=symbol,
        timeframe=timeframe,
        trading_mode="paper",
        execution_mode="paper",
        direction=direction,
        entry_price=entry_price,
        exit_price=exit_price,
        quantity=0.002,
        notional_usd=100.0,
        entry_ts=1_700_000_000,
        exit_ts=1_700_003_600,
        pnl_usd=pnl_usd,
        pnl_pct=pnl_usd / 100.0 if pnl_usd is not None else None,
        fee_usd=fee_usd,
        kelly_fraction=0.25,
        regime_at_entry=regime_at_entry,
        meta_label_prob=meta_label_prob,
        exit_reason=exit_reason,
        approved_by="system",
        raw_signal=raw_signal,
    )


# ---------------------------------------------------------------------------
# build_entry
# ---------------------------------------------------------------------------


def test_build_entry_closed_trade():
    e = build_entry(_trade())
    assert e is not None
    assert isinstance(e, JournalEntry)


def test_build_entry_open_trade_returns_none():
    t = _trade(exit_price=None, pnl_usd=None)
    e = build_entry(t)
    assert e is None


def test_build_entry_pnl_usd_none_returns_none():
    t = _trade(pnl_usd=None)
    e = build_entry(t)
    assert e is None


def test_build_entry_trade_id():
    e = build_entry(_trade(id="abc-123"))
    assert e is not None
    assert e.trade_id == "abc-123"


def test_build_entry_direction_long():
    e = build_entry(_trade(direction=1))
    assert e is not None
    assert e.direction == 1


def test_build_entry_direction_short():
    e = build_entry(_trade(direction=-1))
    assert e is not None
    assert e.direction == -1


def test_build_entry_regime_mapping():
    for regime_int, expected in [(0, "ranging"), (1, "trending"), (2, "volatile")]:
        e = build_entry(_trade(regime_at_entry=regime_int))
        assert e is not None
        assert e.regime == expected


def test_build_entry_unknown_regime():
    e = build_entry(_trade(regime_at_entry=99))
    assert e is not None
    assert e.regime.startswith("regime_")


def test_build_entry_planned_exit():
    e = build_entry(_trade(exit_reason="take_profit"))
    assert e is not None
    assert e.is_planned_exit is True
    assert e.is_forced_exit is False


def test_build_entry_forced_exit():
    e = build_entry(_trade(exit_reason="stop_loss"))
    assert e is not None
    assert e.is_forced_exit is True
    assert e.is_planned_exit is False


def test_build_entry_other_exit():
    e = build_entry(_trade(exit_reason="unknown_reason"))
    assert e is not None
    assert e.is_planned_exit is False
    assert e.is_forced_exit is False


def test_build_entry_pnl_preserved():
    e = build_entry(_trade(pnl_usd=250.0))
    assert e is not None
    assert e.pnl_usd == pytest.approx(250.0)


def test_build_entry_fee_preserved():
    e = build_entry(_trade(fee_usd=12.5))
    assert e is not None
    assert e.fee_usd == pytest.approx(12.5)


def test_build_entry_gross_pnl():
    e = build_entry(_trade(pnl_usd=100.0, fee_usd=10.0))
    assert e is not None
    assert e.gross_pnl_usd == pytest.approx(110.0)


def test_build_entry_signal_quality():
    e = build_entry(_trade(raw_signal=0.75))
    assert e is not None
    assert e.signal_quality == pytest.approx(0.75)


def test_build_entry_model_confidence():
    e = build_entry(_trade(meta_label_prob=0.65))
    assert e is not None
    assert e.model_confidence == pytest.approx(0.65)


def test_build_entry_frozen():
    e = build_entry(_trade())
    assert e is not None
    with pytest.raises((AttributeError, TypeError)):
        e.pnl_usd = 999.0  # type: ignore[misc]


def test_build_entry_narrative_keys():
    e = build_entry(_trade())
    assert e is not None
    assert "regime" in e.narrative
    assert "direction" in e.narrative
    assert "pnl_sign" in e.narrative
    assert "exit_type" in e.narrative


def test_build_entry_to_dict_keys():
    e = build_entry(_trade())
    assert e is not None
    d = e.to_dict()
    for key in (
        "trade_id",
        "symbol",
        "pnl_usd",
        "fee_usd",
        "regime",
        "exit_reason",
        "is_planned_exit",
        "is_forced_exit",
    ):
        assert key in d


# ---------------------------------------------------------------------------
# build_journal
# ---------------------------------------------------------------------------


def test_build_journal_empty():
    assert build_journal([]) == []


def test_build_journal_skips_open_trades():
    trades = [_trade(), _trade(id="t2", exit_price=None, pnl_usd=None)]
    entries = build_journal(trades)
    assert len(entries) == 1


def test_build_journal_returns_all_closed():
    trades = [_trade(id=f"t{i}") for i in range(5)]
    entries = build_journal(trades)
    assert len(entries) == 5


# ---------------------------------------------------------------------------
# summarise_journal
# ---------------------------------------------------------------------------


def test_summarise_empty():
    s = summarise_journal([])
    assert s.n_trades == 0
    assert s.win_rate == 0.0


def test_summarise_n_trades():
    entries = build_journal([_trade(id=f"t{i}") for i in range(3)])
    s = summarise_journal(entries)
    assert s.n_trades == 3


def test_summarise_win_rate():
    trades = [_trade(id="t1", pnl_usd=100.0), _trade(id="t2", pnl_usd=-50.0)]
    s = summarise_journal(build_journal(trades))
    assert s.win_rate == pytest.approx(0.5)


def test_summarise_total_pnl():
    trades = [_trade(id="t1", pnl_usd=100.0), _trade(id="t2", pnl_usd=200.0)]
    s = summarise_journal(build_journal(trades))
    assert s.total_pnl_usd == pytest.approx(300.0)


def test_summarise_planned_exits():
    trades = [
        _trade(id="t1", exit_reason="take_profit"),
        _trade(id="t2", exit_reason="stop_loss"),
    ]
    s = summarise_journal(build_journal(trades))
    assert s.n_planned_exits == 1
    assert s.n_forced_exits == 1


def test_summarise_by_regime():
    trades = [
        _trade(id="t1", regime_at_entry=0, pnl_usd=50.0),
        _trade(id="t2", regime_at_entry=1, pnl_usd=100.0),
        _trade(id="t3", regime_at_entry=0, pnl_usd=-20.0),
    ]
    s = summarise_journal(build_journal(trades))
    assert "ranging" in s.by_regime
    assert s.by_regime["ranging"]["n"] == 2


def test_summarise_to_dict_keys():
    s = summarise_journal(build_journal([_trade()]))
    d = s.to_dict()
    for key in ("n_trades", "win_rate", "total_pnl_usd", "by_regime", "by_exit_reason"):
        assert key in d
