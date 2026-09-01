"""
Structured trade journal — annotates closed trades with structured metadata
for post-trade review.

For each closed TradeRecord, a JournalEntry captures:
  - Regime at entry (regime_at_entry from the record)
  - Exit reason and whether it was planned vs forced
  - P&L decomposition: signal P&L vs fee drag
  - A concise machine-readable narrative dict

build_journal() converts a list of TradeRecords to JournalEntry objects.
summarise_journal() produces aggregate statistics for a batch.

Slippage is NOT populated yet. Measuring it needs the pre-trade mid (or an
expected fill price) recorded at submission, and TradeRecord carries no such
field — see build_entry(), which sets entry_slippage and exit_slippage to
None unconditionally. Until that is persisted, ``total_slippage_usd`` is
always 0.0 and must not be read as "this trade had no slippage". The
SlippageStats dataclass and _slippage() below are the ready-to-wire halves,
kept so the schema change is the only remaining work.

All functions are pure — no I/O.

Authority:
  Almgren & Chriss (2001) "Optimal Execution of Portfolio Transactions" —
    slippage decomposition into market impact and timing components.
  Lopez de Prado (2018) AFML Ch.14 — trade audit and post-mortem framework.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.data.storage import TradeRecord


_PLANNED_EXITS = frozenset(
    {"take_profit", "trailing_stop", "signal_flip", "max_hold", "exit_signal"}
)
_FORCED_EXITS = frozenset({"stop_loss", "liquidation", "margin_call", "kill_switch", "manual"})


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SlippageStats:
    """Estimated slippage for one side (entry or exit) of a trade."""

    expected_price: float  # mid-price or signal price at decision time
    actual_price: float  # fill price
    slippage_pct: float  # (actual - expected) / expected * direction * 100
    slippage_usd: float  # slippage_pct * notional / 100


@dataclass(frozen=True)
class JournalEntry:
    """Structured post-trade journal entry for a single closed trade."""

    trade_id: str
    symbol: str
    timeframe: str
    direction: int  # +1 long, -1 short
    regime: str  # regime label at entry
    exit_reason: str
    is_planned_exit: bool  # True when exit_reason in _PLANNED_EXITS
    is_forced_exit: bool  # True when exit_reason in _FORCED_EXITS

    pnl_usd: float
    fee_usd: float
    gross_pnl_usd: float  # pnl_usd + fee_usd (before fee drag)
    fee_drag_usd: float  # = fee_usd (negative drag)

    entry_slippage: SlippageStats | None
    exit_slippage: SlippageStats | None
    total_slippage_usd: float

    signal_quality: float  # raw_signal from TradeRecord (0 if missing)
    model_confidence: float  # meta_label_prob (0 if missing)

    narrative: dict  # machine-readable summary

    def to_dict(self) -> dict:
        return {
            "trade_id": self.trade_id,
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "direction": self.direction,
            "regime": self.regime,
            "exit_reason": self.exit_reason,
            "is_planned_exit": self.is_planned_exit,
            "is_forced_exit": self.is_forced_exit,
            "pnl_usd": round(self.pnl_usd, 4),
            "fee_usd": round(self.fee_usd, 4),
            "gross_pnl_usd": round(self.gross_pnl_usd, 4),
            "fee_drag_usd": round(self.fee_drag_usd, 4),
            "entry_slippage_pct": round(self.entry_slippage.slippage_pct, 4)
            if self.entry_slippage
            else None,
            "exit_slippage_pct": round(self.exit_slippage.slippage_pct, 4)
            if self.exit_slippage
            else None,
            "total_slippage_usd": round(self.total_slippage_usd, 4),
            "signal_quality": round(self.signal_quality, 4),
            "model_confidence": round(self.model_confidence, 4),
        }


@dataclass
class JournalSummary:
    """Aggregate statistics over a set of JournalEntry objects."""

    n_trades: int = 0
    n_winners: int = 0
    n_planned_exits: int = 0
    n_forced_exits: int = 0
    total_pnl_usd: float = 0.0
    total_fee_usd: float = 0.0
    total_slippage_usd: float = 0.0
    mean_pnl_usd: float = 0.0
    median_pnl_usd: float = 0.0
    win_rate: float = 0.0
    mean_signal_quality: float = 0.0
    mean_model_confidence: float = 0.0
    by_regime: dict[str, dict] = field(default_factory=dict)
    by_exit_reason: dict[str, dict] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "n_trades": self.n_trades,
            "n_winners": self.n_winners,
            "win_rate": round(self.win_rate, 4),
            "n_planned_exits": self.n_planned_exits,
            "n_forced_exits": self.n_forced_exits,
            "total_pnl_usd": round(self.total_pnl_usd, 2),
            "total_fee_usd": round(self.total_fee_usd, 2),
            "total_slippage_usd": round(self.total_slippage_usd, 2),
            "mean_pnl_usd": round(self.mean_pnl_usd, 2),
            "median_pnl_usd": round(self.median_pnl_usd, 2),
            "mean_signal_quality": round(self.mean_signal_quality, 4),
            "mean_model_confidence": round(self.mean_model_confidence, 4),
            "by_regime": self.by_regime,
            "by_exit_reason": self.by_exit_reason,
        }


# ---------------------------------------------------------------------------
# Core functions
# ---------------------------------------------------------------------------


def _regime_label(t: TradeRecord) -> str:
    r = getattr(t, "regime_at_entry", None)
    if r is None:
        return "unknown"
    mapping = {0: "ranging", 1: "trending", 2: "volatile"}
    return mapping.get(int(r), f"regime_{r}")


def _slippage(
    expected_price: float | None,
    actual_price: float,
    direction: int,
    notional_usd: float,
) -> SlippageStats | None:
    if expected_price is None or expected_price <= 0 or actual_price <= 0:
        return None
    # For a long: positive slippage = bought higher than expected (bad)
    # For a short: positive slippage = sold lower than expected (bad)
    raw = (actual_price - expected_price) / expected_price
    slip_pct = raw * direction * 100.0
    slip_usd = slip_pct / 100.0 * notional_usd
    return SlippageStats(
        expected_price=expected_price,
        actual_price=actual_price,
        slippage_pct=slip_pct,
        slippage_usd=slip_usd,
    )


def build_entry(trade: TradeRecord) -> JournalEntry | None:
    """
    Build a JournalEntry from a single TradeRecord.

    Returns None if the trade is not closed (exit_price or pnl_usd missing).
    """
    pnl = getattr(trade, "pnl_usd", None)
    exit_price = getattr(trade, "exit_price", None)
    if pnl is None or exit_price is None:
        return None

    direction = getattr(trade, "direction", 1)
    fee = getattr(trade, "fee_usd", 0.0)
    exit_reason = getattr(trade, "exit_reason", "") or ""
    regime = _regime_label(trade)
    signal_quality = float(getattr(trade, "raw_signal", 0.0) or 0.0)
    model_confidence = float(getattr(trade, "meta_label_prob", 0.0) or 0.0)

    # Slippage — entry: we approximate expected as mid ~ entry_price itself
    # (if no separate mid stored, entry slippage is zero by definition)
    entry_slip = None  # not derivable without pre-trade mid stored separately
    exit_slip = None  # same

    gross_pnl = pnl + fee
    total_slip = (entry_slip.slippage_usd if entry_slip else 0.0) + (
        exit_slip.slippage_usd if exit_slip else 0.0
    )

    is_planned = exit_reason in _PLANNED_EXITS
    is_forced = exit_reason in _FORCED_EXITS

    narrative = {
        "regime": regime,
        "direction": "long" if direction == 1 else "short",
        "pnl_sign": "win" if pnl > 0 else "loss",
        "exit_type": "planned" if is_planned else ("forced" if is_forced else "other"),
        "fee_pct_of_pnl": round(abs(fee) / max(abs(pnl), 1e-9) * 100, 1) if abs(pnl) > 0 else None,
    }

    return JournalEntry(
        trade_id=getattr(trade, "id", ""),
        symbol=getattr(trade, "symbol", ""),
        timeframe=getattr(trade, "timeframe", ""),
        direction=direction,
        regime=regime,
        exit_reason=exit_reason,
        is_planned_exit=is_planned,
        is_forced_exit=is_forced,
        pnl_usd=float(pnl),
        fee_usd=float(fee),
        gross_pnl_usd=float(gross_pnl),
        fee_drag_usd=float(fee),
        entry_slippage=entry_slip,
        exit_slippage=exit_slip,
        total_slippage_usd=total_slip,
        signal_quality=signal_quality,
        model_confidence=model_confidence,
        narrative=narrative,
    )


def build_journal(trades: list[TradeRecord]) -> list[JournalEntry]:
    """Convert a list of TradeRecords to JournalEntry objects (closed trades only)."""
    return [e for t in trades if (e := build_entry(t)) is not None]


def summarise_journal(entries: list[JournalEntry]) -> JournalSummary:
    """Aggregate JournalEntry list into a JournalSummary."""
    s = JournalSummary()
    if not entries:
        return s

    pnls = [e.pnl_usd for e in entries]
    s.n_trades = len(entries)
    s.n_winners = sum(1 for p in pnls if p > 0)
    s.n_planned_exits = sum(1 for e in entries if e.is_planned_exit)
    s.n_forced_exits = sum(1 for e in entries if e.is_forced_exit)
    s.total_pnl_usd = sum(pnls)
    s.total_fee_usd = sum(e.fee_usd for e in entries)
    s.total_slippage_usd = sum(e.total_slippage_usd for e in entries)
    s.mean_pnl_usd = s.total_pnl_usd / len(pnls)
    s.median_pnl_usd = statistics.median(pnls)
    s.win_rate = s.n_winners / s.n_trades
    sq = [e.signal_quality for e in entries]
    s.mean_signal_quality = sum(sq) / len(sq)
    mc = [e.model_confidence for e in entries]
    s.mean_model_confidence = sum(mc) / len(mc)

    # By-regime breakdown
    regime_groups: dict[str, list[float]] = {}
    for e in entries:
        regime_groups.setdefault(e.regime, []).append(e.pnl_usd)
    s.by_regime = {
        r: {
            "n": len(ps),
            "total_pnl": round(sum(ps), 2),
            "win_rate": round(sum(1 for p in ps if p > 0) / len(ps), 4),
        }
        for r, ps in regime_groups.items()
    }

    # By-exit-reason breakdown
    exit_groups: dict[str, list[float]] = {}
    for e in entries:
        exit_groups.setdefault(e.exit_reason or "unknown", []).append(e.pnl_usd)
    s.by_exit_reason = {
        r: {
            "n": len(ps),
            "total_pnl": round(sum(ps), 2),
            "win_rate": round(sum(1 for p in ps if p > 0) / len(ps), 4),
        }
        for r, ps in exit_groups.items()
    }

    return s
