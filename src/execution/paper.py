"""
Paper trading executor.

Simulates trade execution against live market prices without placing
real orders.  Handles all three execution modes:

  AUTOMATIC   — fire immediately when all gates pass
  RESTRICTED  — fire below notional_limit_usd; queue approval above it;
                auto-skip on approval_timeout_s
  MANUAL      — every trade queued for explicit operator approval

Position lifecycle:
  open_position()  → stores entry, deducts notional from cash
  close_position() → computes PnL, returns cash + PnL, persists trade record
  mark_to_market() → updates unrealized PnL from current price

Equity tracking:
  Equity = cash + sum(unrealized PnL of open positions)
  Snapshot written to storage on every close and every mark-to-market cycle.

Authority:
  - Chan (2013) Algorithmic Trading Ch.2 — paper trading methodology
  - López de Prado (2018) AFML Ch.10 — execution cost modelling
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Final

import structlog

from src.config import ExecutionMode, TradingMode, get_settings
from src.data.storage import EquityRecord, StorageBackend, TradeRecord
from src.execution.base import AbstractExecutor
from src.risk.gates import DrawdownTracker
from src.risk.kelly import KellyResult

log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_PAPER_FEE_PCT: Final[float] = 0.001  # 0.1% taker fee (Binance standard)
_TRADING_MODE: Final[TradingMode] = TradingMode.PAPER


# ---------------------------------------------------------------------------
# Position dataclass
# ---------------------------------------------------------------------------


@dataclass
class PaperPosition:
    """
    Single open paper position.

    direction     : 1 = long, 0 = short
    entry_price   : simulated fill price
    quantity      : asset units held
    notional_usd  : entry_price × quantity (before fees)
    unrealized_pnl: marked-to-market PnL (updated by mark_to_market)
    """

    trade_id: str
    symbol: str
    timeframe: str
    direction: int
    entry_price: float
    quantity: float
    notional_usd: float
    entry_ts: int
    kelly_fraction: float
    regime_at_entry: int
    meta_label_prob: float
    raw_signal: float
    approved_by: str
    execution_mode: str
    fee_usd: float
    unrealized_pnl: float = field(default=0.0)
    current_price: float = field(default=0.0)

    def mark(self, price: float) -> float:
        """Update unrealized PnL from current market price."""
        self.current_price = price
        if self.direction == 1:
            self.unrealized_pnl = (price - self.entry_price) * self.quantity
        else:
            self.unrealized_pnl = (self.entry_price - price) * self.quantity
        return self.unrealized_pnl


# ---------------------------------------------------------------------------
# Approval request — used in RESTRICTED and MANUAL modes
# ---------------------------------------------------------------------------


@dataclass
class ApprovalRequest:
    """
    Pending trade awaiting operator approval.

    resolved  : set to True when approved or rejected
    approved  : True = proceed, False = skip
    operator  : ID of approving operator (or 'auto_timeout')
    """

    request_id: str
    symbol: str
    timeframe: str
    direction: int
    notional_usd: float
    entry_price: float
    quantity: float
    kelly_fraction: float
    regime_state: int
    meta_label_prob: float
    raw_signal: float
    created_at: float  # time.monotonic()

    resolved: bool = field(default=False)
    approved: bool = field(default=False)
    operator: str = field(default="")
    # Event-based notification — eliminates busy-poll lock contention (VUL-024)
    _event: asyncio.Event = field(default_factory=asyncio.Event, repr=False)

    def to_dict(self) -> dict[str, object]:
        return {
            "request_id": self.request_id,
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "direction": "long" if self.direction == 1 else "short",
            "notional_usd": round(self.notional_usd, 2),
            "entry_price": round(self.entry_price, 4),
            "quantity": self.quantity,
            "kelly_fraction": round(self.kelly_fraction, 4),
            "regime_state": self.regime_state,
            "meta_label_prob": round(self.meta_label_prob, 4),
            "raw_signal": round(self.raw_signal, 4),
        }


# ---------------------------------------------------------------------------
# Paper executor
# ---------------------------------------------------------------------------


class PaperExecutor(AbstractExecutor):
    """
    Paper trading executor.

    Manages open positions, approval queues, equity tracking, and
    trade persistence.  Thread-safe via asyncio.Lock.

    Lifecycle::

        executor = PaperExecutor(storage, starting_capital=1000.0)
        await executor.initialize()

        sizing_result = compute_position_size(...)
        result = await executor.open_position(signal)

        await executor.mark_to_market({"BTC/USDT": 31000.0})
        await executor.close_position(trade_id, exit_price, reason)

        await executor.shutdown()
    """

    def __init__(
        self,
        storage: StorageBackend,
        starting_capital: float | None = None,
    ) -> None:
        cfg = get_settings()
        self._storage = storage
        self._cfg = cfg
        self._risk_cfg = cfg.risk
        self._starting_capital: float = starting_capital or cfg.starting_capital_usd
        self._cash: float = self._starting_capital
        self._peak_equity: float = self._starting_capital

        self._positions: dict[str, PaperPosition] = {}  # trade_id → position
        self._approval_queue: dict[str, ApprovalRequest] = {}  # request_id → request
        self._lock: asyncio.Lock = asyncio.Lock()
        self._drawdown_tracker = DrawdownTracker(self._starting_capital)
        self._initialized: bool = False
        self._log = log.bind(component="paper_executor")

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def initialize(self) -> None:
        """Restore equity state from storage if prior session exists."""
        latest = await self._storage.latest_equity(TradingMode.PAPER.value)
        if latest is not None:
            self._cash = latest.cash_usd
            self._peak_equity = latest.peak_equity_usd
            self._drawdown_tracker = DrawdownTracker(self._starting_capital)
            self._drawdown_tracker.update(latest.equity_usd)
            self._log.info(
                "paper.initialized_from_storage",
                equity_usd=latest.equity_usd,
                cash_usd=latest.cash_usd,
            )
        else:
            self._log.info(
                "paper.initialized_fresh",
                starting_capital=self._starting_capital,
            )
        self._initialized = True

    async def shutdown(self) -> None:
        """Persist final equity snapshot on clean shutdown."""
        await self._snapshot_equity()
        self._log.info("paper.shutdown", open_positions=len(self._positions))

    # ------------------------------------------------------------------
    # Execution mode routing
    # ------------------------------------------------------------------

    async def submit_signal(
        self,
        symbol: str,
        timeframe: str,
        direction: int,
        kelly_result: KellyResult,
        regime_state: int,
        meta_label_prob: float,
        raw_signal: float,
        current_price: float,
    ) -> tuple[str | None, str]:
        """
        Route a signal through the correct execution mode.

        Returns (trade_id, outcome) where outcome is one of:
          'opened'    — position opened immediately
          'queued'    — approval request created (RESTRICTED/MANUAL)
          'skipped'   — auto-skip on timeout (RESTRICTED only)
          'rejected'  — gate failed or operator rejected

        Parameters
        ----------
        symbol          : trading symbol
        timeframe       : bar timeframe
        direction       : 1=long, 0=short
        kelly_result    : position sizing from kelly.compute_position_size()
        regime_state    : current HMM regime (0/1/2)
        meta_label_prob : meta-label gate probability
        raw_signal      : direction model P(long)
        current_price   : latest mark price for simulated fill
        """
        self._require_initialized()
        mode = self._cfg.execution_mode

        if mode == ExecutionMode.AUTOMATIC:
            trade_id = await self._open_position_internal(
                symbol,
                timeframe,
                direction,
                kelly_result,
                regime_state,
                meta_label_prob,
                raw_signal,
                current_price,
                approved_by="auto",
            )
            return trade_id, "opened" if trade_id else "rejected"

        if mode == ExecutionMode.RESTRICTED:
            if kelly_result.notional_usd <= self._risk_cfg.notional_limit_usd:
                trade_id = await self._open_position_internal(
                    symbol,
                    timeframe,
                    direction,
                    kelly_result,
                    regime_state,
                    meta_label_prob,
                    raw_signal,
                    current_price,
                    approved_by="auto_below_limit",
                )
                return trade_id, "opened" if trade_id else "rejected"
            # Above limit — needs approval
            req_id = await self._enqueue_approval(
                symbol,
                timeframe,
                direction,
                kelly_result,
                regime_state,
                meta_label_prob,
                raw_signal,
            )
            # Wait for approval with timeout
            approved, operator = await self._await_approval(
                req_id, self._risk_cfg.approval_timeout_s
            )
            if not approved:
                return None, "skipped"
            trade_id = await self._open_position_internal(
                symbol,
                timeframe,
                direction,
                kelly_result,
                regime_state,
                meta_label_prob,
                raw_signal,
                current_price,
                approved_by=operator,
            )
            return trade_id, "opened" if trade_id else "rejected"

        if mode == ExecutionMode.MANUAL:
            req_id = await self._enqueue_approval(
                symbol,
                timeframe,
                direction,
                kelly_result,
                regime_state,
                meta_label_prob,
                raw_signal,
            )
            # Wait indefinitely (no timeout in MANUAL — operator must decide)
            approved, operator = await self._await_approval(req_id, timeout_s=None)
            if not approved:
                return None, "rejected"
            trade_id = await self._open_position_internal(
                symbol,
                timeframe,
                direction,
                kelly_result,
                regime_state,
                meta_label_prob,
                raw_signal,
                current_price,
                approved_by=operator,
            )
            return trade_id, "opened" if trade_id else "rejected"

        # Should never reach — ExecutionMode enum is exhaustive
        raise RuntimeError(f"Unknown execution mode: {mode!r}")  # pragma: no cover

    # ------------------------------------------------------------------
    # Position management
    # ------------------------------------------------------------------

    async def close_position(
        self,
        trade_id: str,
        exit_price: float,
        exit_reason: str,
    ) -> float:
        """
        Close an open paper position.

        Computes PnL (net of simulated fees), returns cash to pool,
        persists exit to storage, updates equity snapshot.

        Parameters
        ----------
        trade_id    : UUID from open_position
        exit_price  : simulated fill price at exit
        exit_reason : 'profit_target' | 'stop_loss' | 'time_exit' | 'manual'

        Returns
        -------
        Net PnL in USD.

        Raises
        ------
        KeyError : if trade_id not found in open positions.
        """
        async with self._lock:
            if trade_id not in self._positions:
                raise KeyError(f"No open paper position with trade_id={trade_id!r}")

            pos = self._positions.pop(trade_id)
            exit_ts = int(datetime.now(tz=timezone.utc).timestamp() * 1000)

            if pos.direction == 1:  # long
                gross_pnl = (exit_price - pos.entry_price) * pos.quantity
            else:  # short
                gross_pnl = (pos.entry_price - exit_price) * pos.quantity

            exit_fee = exit_price * pos.quantity * _PAPER_FEE_PCT
            total_fee = pos.fee_usd + exit_fee
            net_pnl = gross_pnl - exit_fee
            pnl_pct = net_pnl / pos.notional_usd if pos.notional_usd > 0 else 0.0

            self._cash += pos.notional_usd + net_pnl
            equity = self._equity_usd()
            self._peak_equity = max(self._peak_equity, equity)
            self._drawdown_tracker.update(equity)

            # SCAN2-006: capture snapshot values inside the lock so the equity record
            # reflects a single atomic point in time and cannot race with mark_to_market.
            snap_unrealized = sum(p.unrealized_pnl for p in self._positions.values())
            snap_cash = self._cash
            snap_equity = equity
            snap_daily_pnl = self._drawdown_tracker.daily_pnl_usd
            snap_daily_pct = self._drawdown_tracker.daily_pnl_pct
            snap_dd_pct = self._drawdown_tracker.drawdown_from_peak_pct
            snap_peak = self._peak_equity

        # Persist exit to storage (outside lock to avoid blocking)
        await self._storage.update_trade_exit(
            trade_id=trade_id,
            exit_price=exit_price,
            exit_ts=exit_ts,
            pnl_usd=round(net_pnl, 8),
            pnl_pct=round(pnl_pct, 8),
            exit_reason=exit_reason,
            fee_usd=exit_fee,
        )
        # Write the pre-captured atomic snapshot (not a live re-read)
        await self._snapshot_equity_with_values(
            equity=snap_equity,
            cash=snap_cash,
            unrealized=snap_unrealized,
            daily_pnl=snap_daily_pnl,
            daily_pct=snap_daily_pct,
            dd_pct=snap_dd_pct,
            peak_equity=snap_peak,
        )

        self._log.info(
            "paper.position_closed",
            trade_id=trade_id,
            symbol=pos.symbol,
            direction=pos.direction,
            entry_price=round(pos.entry_price, 4),
            exit_price=round(exit_price, 4),
            quantity=pos.quantity,
            net_pnl=round(net_pnl, 4),
            pnl_pct=round(pnl_pct * 100, 3),
            exit_reason=exit_reason,
            total_fee=round(total_fee, 4),
        )
        return net_pnl

    async def mark_to_market(
        self,
        prices: dict[str, float],
    ) -> float:
        """
        Update unrealized PnL for all open positions.

        Parameters
        ----------
        prices : symbol → current market price mapping

        Returns
        -------
        Total unrealized PnL across all open positions.
        """
        async with self._lock:
            total_unrealized = 0.0
            for pos in self._positions.values():
                price = prices.get(pos.symbol)
                if price is not None and price > 0.0:
                    pos.mark(price)
                total_unrealized += pos.unrealized_pnl

            equity = self._cash + total_unrealized
            self._peak_equity = max(self._peak_equity, equity)
            self._drawdown_tracker.update(equity)

        await self._snapshot_equity()
        return total_unrealized

    # ------------------------------------------------------------------
    # Approval queue — used by API to resolve RESTRICTED/MANUAL requests
    # ------------------------------------------------------------------

    async def resolve_approval(
        self,
        request_id: str,
        approved: bool,
        operator: str,
    ) -> bool:
        """
        Resolve a pending approval request.
        Sets asyncio.Event to wake _await_approval immediately (VUL-024).
        """
        async with self._lock:
            req = self._approval_queue.get(request_id)
            if req is None or req.resolved:
                return False
            req.resolved = True
            req.approved = approved
            req.operator = operator
            req._event.set()  # noqa: SLF001

        self._log.info(
            "paper.approval_resolved",
            request_id=request_id,
            approved=approved,
            operator=operator,
        )
        return True

    def pending_approvals(self) -> list[dict[str, object]]:
        """Return all unresolved approval requests as dicts for the API."""
        return [req.to_dict() for req in self._approval_queue.values() if not req.resolved]

    async def open_positions_safe(self) -> list[dict[str, object]]:
        """
        Lock-safe snapshot for WS heartbeat (VUL-035).

        SCAN2-014: builds the snapshot directly under the lock rather than
        delegating to open_positions() (which holds no lock internally) —
        eliminates the latent deadlock risk if open_positions() ever acquires
        the lock itself, and makes the locking semantics unambiguous.
        """
        async with self._lock:
            return [
                {
                    "trade_id": p.trade_id,
                    "symbol": p.symbol,
                    "timeframe": p.timeframe,
                    "direction": "long" if p.direction == 1 else "short",
                    "entry_price": round(p.entry_price, 4),
                    "current_price": round(p.current_price, 4),
                    "quantity": p.quantity,
                    "notional_usd": round(p.notional_usd, 2),
                    "unrealized_pnl": round(p.unrealized_pnl, 4),
                    "unrealized_pnl_pct": round(p.unrealized_pnl / p.notional_usd * 100.0, 3)
                    if p.notional_usd > 0
                    else 0.0,
                    "regime_at_entry": p.regime_at_entry,
                    "entry_ts": p.entry_ts,
                }
                for p in self._positions.values()
            ]

    async def pending_approvals_safe(self) -> list[dict[str, object]]:
        """Lock-safe snapshot for WS heartbeat (VUL-035)."""
        async with self._lock:
            return [req.to_dict() for req in self._approval_queue.values() if not req.resolved]

    # ------------------------------------------------------------------
    # State queries
    # ------------------------------------------------------------------

    def open_positions(self) -> list[dict[str, object]]:
        """Return all open positions as dicts for the API."""
        return [
            {
                "trade_id": p.trade_id,
                "symbol": p.symbol,
                "timeframe": p.timeframe,
                "direction": "long" if p.direction == 1 else "short",
                "entry_price": round(p.entry_price, 4),
                "current_price": round(p.current_price, 4),
                "quantity": p.quantity,
                "notional_usd": round(p.notional_usd, 2),
                "unrealized_pnl": round(p.unrealized_pnl, 4),
                "unrealized_pnl_pct": round(p.unrealized_pnl / p.notional_usd * 100.0, 3)
                if p.notional_usd > 0
                else 0.0,
                "regime_at_entry": p.regime_at_entry,
                "entry_ts": p.entry_ts,
            }
            for p in self._positions.values()
        ]

    @property
    def cash_usd(self) -> float:
        return self._cash

    @property
    def equity_usd(self) -> float:
        return self._equity_usd()

    @property
    def starting_capital(self) -> float:
        return self._starting_capital

    @property
    def peak_equity(self) -> float:
        return self._peak_equity

    @property
    def drawdown_tracker(self) -> DrawdownTracker:
        return self._drawdown_tracker

    @property
    def starting_equity_usd(self) -> float:
        """NEW-009: daily_start_equity via AbstractExecutor interface."""
        return self._drawdown_tracker.daily_start_equity

    async def reset_daily_equity(self) -> float:
        """
        NEW-005: Atomically snapshot current equity and reset daily tracker.

        Acquires the executor lock so this never races with mark_to_market.
        Returns the equity value used for the reset.
        """
        async with self._lock:
            equity = self._equity_usd()
            self._drawdown_tracker.reset_daily(equity)
        self._log.info("paper.daily_equity_reset", equity_usd=round(equity, 2))
        return equity

    def position_count(self) -> int:
        return len(self._positions)

    async def get_consecutive_losses(self, symbol: str) -> int:
        """Query storage for trailing consecutive loss count."""
        return await self._storage.count_consecutive_losses(symbol, TradingMode.PAPER.value)

    async def get_daily_pnl(self, symbol: str) -> float:
        """Query storage for today's realized PnL."""
        return await self._storage.daily_pnl(symbol, TradingMode.PAPER.value)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _open_position_internal(
        self,
        symbol: str,
        timeframe: str,
        direction: int,
        kelly_result: KellyResult,
        regime_state: int,
        meta_label_prob: float,
        raw_signal: float,
        current_price: float,
        approved_by: str,
    ) -> str | None:
        """
        Open a paper position and persist trade record.

        Returns trade_id on success, None if cash is insufficient.
        """
        entry_fee = current_price * kelly_result.quantity * _PAPER_FEE_PCT
        notional = kelly_result.notional_usd

        async with self._lock:
            if self._cash < notional + entry_fee:
                self._log.warning(
                    "paper.insufficient_cash",
                    cash=round(self._cash, 2),
                    needed=round(notional + entry_fee, 2),
                )
                return None

            trade_id = str(uuid.uuid4())
            entry_ts = int(datetime.now(tz=timezone.utc).timestamp() * 1000)
            self._cash -= notional + entry_fee

            pos = PaperPosition(
                trade_id=trade_id,
                symbol=symbol,
                timeframe=timeframe,
                direction=direction,
                entry_price=current_price,
                quantity=kelly_result.quantity,
                notional_usd=notional,
                entry_ts=entry_ts,
                kelly_fraction=kelly_result.adjusted_fraction,
                regime_at_entry=regime_state,
                meta_label_prob=meta_label_prob,
                raw_signal=raw_signal,
                approved_by=approved_by,
                execution_mode=self._cfg.execution_mode.value,
                fee_usd=entry_fee,
                current_price=current_price,
            )
            self._positions[trade_id] = pos

        # Persist entry record (exit fields are None until close)
        trade_record = TradeRecord(
            id=trade_id,
            symbol=symbol,
            timeframe=timeframe,
            trading_mode=TradingMode.PAPER.value,
            execution_mode=self._cfg.execution_mode.value,
            direction=direction,
            entry_price=current_price,
            exit_price=None,
            quantity=kelly_result.quantity,
            notional_usd=notional,
            entry_ts=entry_ts,
            exit_ts=None,
            pnl_usd=None,
            pnl_pct=None,
            fee_usd=entry_fee,
            kelly_fraction=kelly_result.adjusted_fraction,
            regime_at_entry=regime_state,
            meta_label_prob=meta_label_prob,
            exit_reason=None,
            approved_by=approved_by,
            raw_signal=raw_signal,
        )
        await self._storage.insert_trade(trade_record)
        await self._snapshot_equity()

        self._log.info(
            "paper.position_opened",
            trade_id=trade_id,
            symbol=symbol,
            direction="long" if direction == 1 else "short",
            entry_price=round(current_price, 4),
            quantity=kelly_result.quantity,
            notional_usd=round(notional, 2),
            kelly_fraction=round(kelly_result.adjusted_fraction, 4),
            approved_by=approved_by,
        )
        return trade_id

    async def _enqueue_approval(
        self,
        symbol: str,
        timeframe: str,
        direction: int,
        kelly_result: KellyResult,
        regime_state: int,
        meta_label_prob: float,
        raw_signal: float,
    ) -> str:
        """Create an ApprovalRequest and add to the queue."""
        req_id = str(uuid.uuid4())
        req = ApprovalRequest(
            request_id=req_id,
            symbol=symbol,
            timeframe=timeframe,
            direction=direction,
            notional_usd=kelly_result.notional_usd,
            entry_price=0.0,  # filled at execution time
            quantity=kelly_result.quantity,
            kelly_fraction=kelly_result.adjusted_fraction,
            regime_state=regime_state,
            meta_label_prob=meta_label_prob,
            raw_signal=raw_signal,
            created_at=time.monotonic(),
        )
        async with self._lock:
            self._approval_queue[req_id] = req

        self._log.info(
            "paper.approval_queued",
            request_id=req_id,
            symbol=symbol,
            direction=direction,
            notional_usd=round(kelly_result.notional_usd, 2),
        )
        return req_id

    async def _await_approval(
        self,
        request_id: str,
        timeout_s: float | None,
    ) -> tuple[bool, str]:
        """
        Wait for approval using asyncio.Event — replaces 250ms busy-poll (VUL-024).
        Returns (approved, operator_id).
        """
        async with self._lock:
            req = self._approval_queue.get(request_id)
        if req is None:
            return False, ""

        try:
            await asyncio.wait_for(req._event.wait(), timeout=timeout_s)  # noqa: SLF001
        except asyncio.TimeoutError:
            async with self._lock:
                timed_out = self._approval_queue.pop(request_id, None)
                if timed_out is not None:
                    timed_out.resolved = True
                    timed_out.approved = False
                    timed_out.operator = "auto_timeout"
            self._log.warning(
                "paper.approval_timeout",
                request_id=request_id,
                timeout_s=timeout_s,
            )
            return False, "auto_timeout"

        async with self._lock:
            resolved = self._approval_queue.pop(request_id, None)
        if resolved is None:
            return False, ""
        return resolved.approved, resolved.operator

    def _equity_usd(self) -> float:
        """Total equity = cash + sum of unrealized PnL."""
        return self._cash + sum(p.unrealized_pnl for p in self._positions.values())

    async def _snapshot_equity(self) -> None:
        """Write current equity state to storage (re-reads live state — not lock-safe)."""
        equity = self._equity_usd()
        unrealized = sum(p.unrealized_pnl for p in self._positions.values())
        daily_pnl = self._drawdown_tracker.daily_pnl_usd
        daily_pct = self._drawdown_tracker.daily_pnl_pct
        dd_pct = self._drawdown_tracker.drawdown_from_peak_pct
        await self._snapshot_equity_with_values(
            equity=equity,
            cash=self._cash,
            unrealized=unrealized,
            daily_pnl=daily_pnl,
            daily_pct=daily_pct,
            dd_pct=dd_pct,
            peak_equity=self._peak_equity,
        )

    async def _snapshot_equity_with_values(
        self,
        equity: float,
        cash: float,
        unrealized: float,
        daily_pnl: float,
        daily_pct: float,
        dd_pct: float,
        peak_equity: float,
    ) -> None:
        """
        Write a pre-computed equity snapshot to storage.

        SCAN2-006: Called with values captured inside the lock so the record
        is internally consistent and cannot race with concurrent mark_to_market.
        """
        record = EquityRecord(
            ts=int(datetime.now(tz=timezone.utc).timestamp() * 1000),
            trading_mode=TradingMode.PAPER.value,
            equity_usd=round(equity, 8),
            cash_usd=round(cash, 8),
            unrealized_pnl=round(unrealized, 8),
            daily_pnl_usd=round(daily_pnl, 8),
            daily_pnl_pct=round(daily_pct, 8),
            peak_equity_usd=round(peak_equity, 8),
            drawdown_pct=round(dd_pct, 8),
        )
        await self._storage.insert_equity(record)

    def _require_initialized(self) -> None:
        if not self._initialized:
            raise RuntimeError(
                "PaperExecutor not initialized — call await executor.initialize() first."
            )
