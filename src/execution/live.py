"""
Live trading executor — real money order placement via ccxt.

Mirrors PaperExecutor's interface exactly so the orchestrator can
swap executors by trading_mode without any logic changes.

Safety layers:
  - Requires TRADING_MODE=live in environment (enforced by config)
  - All risk gates evaluated before any order is placed
  - Every order confirmed via exchange before position is recorded
  - Partial fills tracked; position only opened on confirmed fill
  - Full audit trail in storage identical to paper executor

Fee model: actual exchange fees reported in order response.
Slippage: actual fill price from order response (not request price).

Authority:
  - ccxt unified API — market orders, fetch_order
  - Chan (2013) Ch.6 — live execution infrastructure
"""

from __future__ import annotations

import asyncio
import contextlib
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Final

import ccxt.async_support as ccxt
import structlog

from src.config import ExecutionMode, TradingMode, get_settings, runtime_config
from src.data.fetcher import MarketDataFetcher
from src.data.storage import AnyStorageBackend, BlendAudit, EquityRecord, TradeRecord
from src.diagnostics.attribution import AttributedFill, get_attribution_tracker
from src.diagnostics.disaster_recovery import (
    Discrepancy,
    DiscrepancyType,
    PositionSnapshot,
    is_state_consistent,
    reconcile,
)
from src.execution.base import AbstractExecutor
from src.execution.idempotency import (
    DuplicateOrderError,
    IdempotencyRegistry,
    derive_idempotency_key,
)
from src.execution.order_manager import OrderManager
from src.execution.order_throttler import OrderThrottler
from src.risk.gates import DrawdownTracker
from src.risk.kelly import KellyResult

# Bounded in-memory registry of recent order FSM states, for the
# GET /orders/{order_id}/status reconciliation endpoint. This is
# intentionally NOT a durable store -- it survives only as long as the
# process does and is capped in size, since it exists purely to let an
# operator inspect a recently-placed order's lifecycle (e.g. after a
# timeout) without re-querying the exchange. Durable trade history lives
# in StorageBackend's trades table; this is a short-term debugging aid.
_ORDER_FSM_REGISTRY_MAX_SIZE: Final[int] = 200


log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

_LIVE_FEE_FALLBACK: Final[float] = 0.001  # fallback if exchange fee missing

# v8 reconciliation tolerances. Spot balances carry dust and fills round, so
# an exact comparison would flag a correctly-tracked book on every restart.
# The relative term scales with the position actually held; the absolute term
# is the floor below which a balance is not a position at all.
_RECONCILE_DUST_QTY: Final[float] = 1e-8
_RECONCILE_TOLERANCE_PCT: Final[float] = 0.01  # 1% of the larger side
_ORDER_CONFIRM_POLLS: Final[int] = 10  # max polls for order confirmation
_ORDER_CONFIRM_INTERVAL: Final[float] = 0.5  # seconds between polls


# ---------------------------------------------------------------------------
# Live position dataclass
# ---------------------------------------------------------------------------


@dataclass
class LivePosition:
    """Single confirmed open live position."""

    trade_id: str
    exchange_order_id: str
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
    strategy_id: str = field(default="signal_engine_v1")
    unrealized_pnl: float = field(default=0.0)
    current_price: float = field(default=0.0)
    peak_unrealized_pct: float = field(default=0.0)

    def mark(self, price: float) -> float:
        self.current_price = price
        if self.direction == 1:
            self.unrealized_pnl = (price - self.entry_price) * self.quantity
        else:
            self.unrealized_pnl = (self.entry_price - price) * self.quantity
        if self.notional_usd > 0:
            pct = self.unrealized_pnl / self.notional_usd * 100.0
            if pct > self.peak_unrealized_pct:
                self.peak_unrealized_pct = pct
        return self.unrealized_pnl


# ---------------------------------------------------------------------------
# Approval request — identical shape to paper executor
# ---------------------------------------------------------------------------


@dataclass
class ApprovalRequest:
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
    created_at: float
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
# LiveExecutor
# ---------------------------------------------------------------------------


class LiveExecutor(AbstractExecutor):
    """
    Live trading executor — places real orders on Binance via ccxt.

    Interface is identical to PaperExecutor so the orchestrator
    swaps executors with zero logic changes.

    Usage::

        executor = LiveExecutor(storage, fetcher)
        await executor.initialize()
        result = await executor.submit_signal(...)
        await executor.close_position(trade_id, exit_price, reason)
        await executor.shutdown()
    """

    def __init__(
        self,
        storage: AnyStorageBackend,
        fetcher: MarketDataFetcher,
        starting_capital: float | None = None,
    ) -> None:
        cfg = get_settings()
        if cfg.trading_mode != TradingMode.LIVE:
            raise RuntimeError(
                "LiveExecutor instantiated without TRADING_MODE=live. "
                "Set TRADING_MODE=live in .env to enable live trading."
            )
        self._storage = storage
        self._fetcher = fetcher
        self._cfg = cfg
        self._risk_cfg = cfg.risk
        self._starting_capital: float = starting_capital or cfg.starting_capital_usd
        self._cash: float = self._starting_capital
        self._peak_equity: float = self._starting_capital
        self._positions: dict[str, LivePosition] = {}
        self._approval_queue: dict[str, ApprovalRequest] = {}
        self._lock: asyncio.Lock = asyncio.Lock()
        # Semaphore(1): ensures only one order is placed+recorded at a time.
        # Prevents concurrent signals from both passing the cash pre-check
        # against the same balance and double-spending (VUL-009).
        self._trade_semaphore: asyncio.Semaphore = asyncio.Semaphore(1)
        self._drawdown_tracker = DrawdownTracker(self._starting_capital)
        self._order_manager: OrderManager = OrderManager()  # GAP-004
        self._throttle_cfg = cfg.order_throttle
        self._throttler: OrderThrottler = OrderThrottler(
            rate=self._throttle_cfg.rate,
            burst=self._throttle_cfg.burst,
        )
        # GAP-004 follow-up (found during audit, 2026-06-25): the FSM
        # returned by place_order_with_fsm() was previously a local variable
        # in _place_market_order(), discarded as soon as the function
        # returned -- the docstring claimed "state persistence for manual
        # reconciliation" but nothing was actually persisted, and
        # GET /orders/{order_id}/status had no registry to read from.
        self._order_fsm_registry: OrderedDict[str, Any] = OrderedDict()
        self._initialized: bool = False
        # v8 disaster recovery: discrepancies found when initialize() compared
        # local state against exchange truth. Non-empty means the process is
        # not sure what it owns, so submit_signal() refuses to open anything
        # new until an operator clears it -- explicit-only, like the strategy
        # kill switch and the capital-preservation floor.
        self._recovery_discrepancies: list[Discrepancy] = []
        self._log = log.bind(component="live_executor")

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def initialize(self) -> None:
        """Restore equity state from storage if prior live session exists."""
        latest = await self._storage.latest_equity(TradingMode.LIVE.value)
        if latest is not None:
            self._cash = latest.cash_usd
            self._peak_equity = latest.peak_equity_usd
            self._drawdown_tracker = DrawdownTracker(self._starting_capital)
            self._drawdown_tracker.update(latest.equity_usd)
            self._log.info(
                "live.initialized_from_storage",
                equity_usd=latest.equity_usd,
                cash_usd=latest.cash_usd,
            )
        else:
            self._log.info("live.initialized_fresh", starting_capital=self._starting_capital)
        await self._reconcile_with_exchange()
        self._initialized = True

    async def _reconcile_with_exchange(self) -> None:
        """
        Compare local positions against exchange truth on startup (v8).

        initialize() restores equity from storage but not positions, so after
        a crash self._positions is empty while the exchange may still hold
        real exposure. Trading on that assumption would size as though the
        book were flat.

        Any discrepancy is recorded and blocks new entries until an operator
        acknowledges it. The reconciliation itself never places or cancels an
        order — the module is deliberately advisory, and guessing how to
        resolve an unexplained position is exactly the wrong instinct here.

        A snapshot that could not be obtained is treated as unresolved, not
        as "no discrepancies": failing to look is not the same as looking and
        finding nothing.

        Scoped to the symbols this executor is responsible for -- whatever the
        local book holds, plus the configured primary symbol so a crashed
        position in it is still caught when the local book came back empty.
        An unrelated asset elsewhere in the account is not evidence that this
        bot's book is wrong.

        Comparison uses a relative tolerance because spot balances carry dust
        and fills round. The consequence is deliberate: a manual balance in a
        traded symbol does block startup, because the executor genuinely
        cannot distinguish it from an untracked position of its own.
        """
        symbols = sorted({p.symbol for p in self._positions.values()} | {self._cfg.primary_symbol})
        try:
            holdings = await self._fetcher.fetch_exchange_holdings(symbols)
        except Exception as exc:
            holdings = None
            self._log.error("live.reconcile_fetch_failed", error=str(exc), exc_info=True)

        if holdings is None:
            self._recovery_discrepancies = [
                Discrepancy(
                    symbol="*",
                    discrepancy_type=DiscrepancyType.MISSING_LOCALLY,
                    local_quantity=0.0,
                    reference_quantity=0.0,
                )
            ]
            self._log.critical("live.reconcile_snapshot_unavailable", entries_blocked=True)
            return

        # Net per symbol: several timeframes can hold the same symbol, and
        # PositionSnapshot is one row per symbol.
        netted: dict[str, float] = {}
        for position in self._positions.values():
            signed = position.quantity if position.direction == 1 else -position.quantity
            netted[position.symbol] = netted.get(position.symbol, 0.0) + signed

        local = [PositionSnapshot(symbol=sym, quantity=qty) for sym, qty in netted.items()]
        exchange = [
            PositionSnapshot(symbol=sym, quantity=qty)
            for sym, qty in holdings.items()
            if abs(qty) > _RECONCILE_DUST_QTY
        ]
        tolerance = max(
            _RECONCILE_DUST_QTY,
            _RECONCILE_TOLERANCE_PCT
            * max((abs(q) for q in [*netted.values(), *holdings.values()]), default=0.0),
        )
        self._recovery_discrepancies = reconcile(local, exchange, quantity_tolerance=tolerance)

        if is_state_consistent(self._recovery_discrepancies):
            self._log.info("live.reconcile_consistent", symbols=symbols)
            return
        self._log.critical(
            "live.reconcile_discrepancies",
            count=len(self._recovery_discrepancies),
            discrepancies=[
                {
                    "symbol": d.symbol,
                    "type": d.discrepancy_type.value,
                    "local": d.local_quantity,
                    "exchange": d.reference_quantity,
                }
                for d in self._recovery_discrepancies
            ],
            entries_blocked=True,
        )

    @property
    def recovery_discrepancies(self) -> list[Discrepancy]:
        """Unresolved startup reconciliation discrepancies (empty = clean)."""
        return list(self._recovery_discrepancies)

    def acknowledge_recovery(self, operator: str) -> int:
        """
        Clear the reconciliation block after an operator has resolved it.

        Explicit-only and never automatic: the discrepancies are cleared
        because a human says the book is now understood, not because time
        passed or a later snapshot happened to agree.

        Returns the number of discrepancies cleared.
        """
        cleared = len(self._recovery_discrepancies)
        self._recovery_discrepancies = []
        self._log.warning("live.recovery_acknowledged", operator=operator, cleared=cleared)
        return cleared

    async def shutdown(self) -> None:
        """Persist equity and log open positions on shutdown."""
        await self._snapshot_equity()
        if self._positions:
            self._log.warning(
                "live.shutdown_with_open_positions",
                count=len(self._positions),
                trade_ids=list(self._positions.keys()),
            )
        self._log.info("live.shutdown", open_positions=len(self._positions))

    # ------------------------------------------------------------------
    # Signal routing — mirrors PaperExecutor.submit_signal exactly
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
        strategy_id: str = "signal_engine_v1",
        blend_audit: BlendAudit | None = None,
    ) -> tuple[str | None, str]:
        """
        Route signal through execution mode and place live order if approved.

        Returns (trade_id, outcome).
        outcome: 'opened' | 'queued' | 'skipped' | 'rejected'
        """
        self._require_initialized()
        # v8: refuse new exposure while startup reconciliation is unresolved.
        # Sizing assumes self._positions is the whole book; if the exchange
        # disagrees, every gate downstream is reasoning about the wrong book.
        # Exits are unaffected -- this blocks submit_signal only, so an
        # operator can still flatten while the discrepancy is being resolved.
        if self._recovery_discrepancies:
            self._log.error(
                "live.blocked_unreconciled_state",
                symbol=symbol,
                discrepancies=len(self._recovery_discrepancies),
            )
            return None, "rejected"

        # H-15: read runtime_config (live async-settable) not self._cfg.execution_mode
        # (frozen Settings). Without this, POST /execution-mode has no effect.
        mode = await runtime_config.get_execution_mode()

        if mode == ExecutionMode.AUTOMATIC:
            return await self._submit_signal_auto(
                symbol,
                timeframe,
                direction,
                kelly_result,
                regime_state,
                meta_label_prob,
                raw_signal,
                approved_by="auto",
                strategy_id=strategy_id,
                blend_audit=blend_audit,
            )

        if mode == ExecutionMode.RESTRICTED:
            if kelly_result.notional_usd <= self._risk_cfg.notional_limit_usd:
                return await self._submit_signal_auto(
                    symbol,
                    timeframe,
                    direction,
                    kelly_result,
                    regime_state,
                    meta_label_prob,
                    raw_signal,
                    approved_by="auto_below_limit",
                    strategy_id=strategy_id,
                    blend_audit=blend_audit,
                )
            return await self._submit_signal_with_approval(
                symbol,
                timeframe,
                direction,
                kelly_result,
                regime_state,
                meta_label_prob,
                raw_signal,
                timeout_s=self._risk_cfg.approval_timeout_s,
                denied_outcome="skipped",
                strategy_id=strategy_id,
                blend_audit=blend_audit,
            )

        if mode == ExecutionMode.MANUAL:
            return await self._submit_signal_with_approval(
                symbol,
                timeframe,
                direction,
                kelly_result,
                regime_state,
                meta_label_prob,
                raw_signal,
                timeout_s=None,
                denied_outcome="rejected",
                strategy_id=strategy_id,
                blend_audit=blend_audit,
            )

        raise RuntimeError(f"Unknown execution mode: {mode!r}")  # pragma: no cover

    async def _submit_signal_auto(
        self,
        symbol: str,
        timeframe: str,
        direction: int,
        kelly_result: KellyResult,
        regime_state: int,
        meta_label_prob: float,
        raw_signal: float,
        approved_by: str,
        strategy_id: str = "signal_engine_v1",
        blend_audit: BlendAudit | None = None,
    ) -> tuple[str | None, str]:
        trade_id = await self._place_and_record(
            symbol,
            timeframe,
            direction,
            kelly_result,
            regime_state,
            meta_label_prob,
            raw_signal,
            approved_by=approved_by,
            strategy_id=strategy_id,
            blend_audit=blend_audit,
        )
        return trade_id, "opened" if trade_id else "rejected"

    async def _submit_signal_with_approval(
        self,
        symbol: str,
        timeframe: str,
        direction: int,
        kelly_result: KellyResult,
        regime_state: int,
        meta_label_prob: float,
        raw_signal: float,
        timeout_s: float | None,
        denied_outcome: str,
        strategy_id: str = "signal_engine_v1",
        blend_audit: BlendAudit | None = None,
    ) -> tuple[str | None, str]:
        req_id = await self._enqueue_approval(
            symbol,
            timeframe,
            direction,
            kelly_result,
            regime_state,
            meta_label_prob,
            raw_signal,
        )
        approved, operator = await self._await_approval(req_id, timeout_s=timeout_s)
        if not approved:
            return None, denied_outcome
        return await self._submit_signal_auto(
            symbol,
            timeframe,
            direction,
            kelly_result,
            regime_state,
            meta_label_prob,
            raw_signal,
            approved_by=operator,
            strategy_id=strategy_id,
            blend_audit=blend_audit,
        )

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
        Close a live position by placing a market close order.

        C-07: Acquire _trade_semaphore to serialise concurrent close+open
        operations. Without this, a close that restores cash can race with
        a concurrent entry that already passed the cash pre-check, causing
        effective double-spending of the restored balance.
        """
        async with self._trade_semaphore:
            async with self._lock:
                if trade_id not in self._positions:
                    raise KeyError(f"No open live position trade_id={trade_id!r}")
                pos = self._positions[trade_id]

            # Place closing order (opposite side)
            close_side = "sell" if pos.direction == 1 else "buy"
            try:
                order = await self._place_market_order(
                    symbol=pos.symbol,
                    side=close_side,
                    quantity=pos.quantity,
                    is_exit=True,
                    # Pinned to the position, not to a time bucket: closing
                    # trade_id X is the same intent whenever it is retried, so
                    # a retry that straddled a bucket boundary must not be
                    # able to submit a second closing order.
                    purpose="close",
                    intent_id=trade_id,
                    strategy_id=pos.strategy_id,
                )
            except (ccxt.NetworkError, ccxt.ExchangeError) as exc:
                self._log.error(
                    "live.close_order_failed",
                    trade_id=trade_id,
                    symbol=pos.symbol,
                    error=str(exc),
                    exc_info=True,
                )
                raise

            actual_exit_price = float(order.get("average") or order.get("price") or exit_price)
            # `or` collapses a reported fill of 0.0 into the fallback, so an
            # order that filled nothing -- rejected, or cancelled immediately
            # -- read as fully filled and the position was deleted from
            # internal state while the exposure was still live on the
            # exchange. Missing (None) is the only case the fallback is for.
            _filled_raw = order.get("filled")
            filled_qty = float(pos.quantity) if _filled_raw is None else float(_filled_raw)

            if filled_qty <= 0.0:
                self._log.error(
                    "live.close_order_zero_fill",
                    trade_id=trade_id,
                    symbol=pos.symbol,
                    order_id=str(order.get("id", "")),
                    action="position left open — exposure is still live on the exchange",
                )
                raise RuntimeError(
                    f"Close order for {pos.symbol} ({trade_id}) filled 0; position not closed."
                )

            if filled_qty < pos.quantity * 0.999:
                # Booking the whole position closed on a partial fill leaves
                # untracked residual exposure. Splitting the position is a
                # larger change than this path can safely make mid-close, so
                # the discrepancy is surfaced rather than buried.
                self._log.critical(
                    "live.close_order_partial_fill_UNTRACKED_RESIDUAL",
                    trade_id=trade_id,
                    symbol=pos.symbol,
                    requested_qty=pos.quantity,
                    filled_qty=filled_qty,
                    residual_qty=pos.quantity - filled_qty,
                    action="position booked closed at the filled size; residual is untracked",
                )
            exchange_fee = self._extract_fee(order, actual_exit_price, filled_qty)

            exit_ts = int(datetime.now(tz=UTC).timestamp() * 1000)

            if pos.direction == 1:
                gross_pnl = (actual_exit_price - pos.entry_price) * filled_qty
            else:
                gross_pnl = (pos.entry_price - actual_exit_price) * filled_qty

            # Both legs. pos.fee_usd is the fee the exchange charged on entry;
            # leaving it out overstated every recorded trade by half its
            # round-trip cost, and that number drives compute_win_loss_stats
            # into Kelly, the Sharpe and Sortino behind capital allocation,
            # and the out-of-sample bar the live gate checks.
            total_fee = pos.fee_usd + exchange_fee
            net_pnl = gross_pnl - total_fee
            pnl_pct = net_pnl / pos.notional_usd if pos.notional_usd > 0 else 0.0

            async with self._lock:
                self._positions.pop(trade_id, None)
                # Not net_pnl: the entry fee already left the balance at open
                # (self._cash -= notional + entry_fee), so charging it again
                # here would double-count it and drain equity over time.
                self._cash += pos.notional_usd + gross_pnl - exchange_fee
                equity = self._equity_usd()
                self._peak_equity = max(self._peak_equity, equity)
                self._drawdown_tracker.update(equity)
                # SCAN3-001/SCAN3-002: capture snapshot values inside lock
                snap_unrealized = sum(p.unrealized_pnl for p in self._positions.values())
                snap_cash = self._cash
                snap_equity = equity
                snap_daily_pnl = self._drawdown_tracker.daily_pnl_usd
                snap_daily_pct = self._drawdown_tracker.daily_pnl_pct
                snap_dd_pct = self._drawdown_tracker.drawdown_from_peak_pct
                snap_peak = self._peak_equity

            await self._storage.update_trade_exit(
                trade_id=trade_id,
                exit_price=actual_exit_price,
                exit_ts=exit_ts,
                pnl_usd=round(net_pnl, 8),
                pnl_pct=round(pnl_pct, 8),
                exit_reason=exit_reason,
                # Both legs, matching pnl_usd above.
                fee_usd=round(total_fee, 8),
            )
            await self._snapshot_equity_with_values(
                equity=snap_equity,
                cash=snap_cash,
                unrealized=snap_unrealized,
                daily_pnl=snap_daily_pnl,
                daily_pct=snap_daily_pct,
                dd_pct=snap_dd_pct,
                peak_equity=snap_peak,
            )

            get_attribution_tracker().record(
                AttributedFill(
                    strategy_id=pos.strategy_id,
                    pnl_usd=net_pnl,
                    entry_ts=pos.entry_ts,
                    exit_ts=int(datetime.now(tz=UTC).timestamp() * 1000),
                )
            )
            self._log.info(
                "live.position_closed",
                trade_id=trade_id,
                symbol=pos.symbol,
                exit_price=round(actual_exit_price, 4),
                net_pnl=round(net_pnl, 4),
                exit_reason=exit_reason,
            )
            return net_pnl

    async def mark_to_market(self, prices: dict[str, float]) -> float:
        """Update unrealized PnL for all open positions."""
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
            # C-04: capture snapshot values INSIDE the lock before releasing
            snap_equity = equity
            snap_cash = self._cash
            snap_unrealized = total_unrealized
            snap_daily_pnl = self._drawdown_tracker.daily_pnl_usd
            snap_daily_pct = self._drawdown_tracker.daily_pnl_pct
            snap_dd_pct = self._drawdown_tracker.drawdown_from_peak_pct
            snap_peak = self._peak_equity

        await self._snapshot_equity_with_values(
            equity=snap_equity,
            cash=snap_cash,
            unrealized=snap_unrealized,
            daily_pnl=snap_daily_pnl,
            daily_pct=snap_daily_pct,
            dd_pct=snap_dd_pct,
            peak_equity=snap_peak,
        )
        return total_unrealized

    # ------------------------------------------------------------------
    # Approval queue
    # ------------------------------------------------------------------

    async def resolve_approval(
        self,
        request_id: str,
        approved: bool,
        operator: str,
    ) -> bool:
        """
        Mark an approval request as resolved and wake the waiting coroutine via Event.
        Eliminates the 250ms busy-poll lock loop (VUL-024).
        """
        async with self._lock:
            req = self._approval_queue.get(request_id)
            if req is None or req.resolved:
                return False
            req.resolved = True
            req.approved = approved
            req.operator = operator
            req._event.set()
        self._log.info(
            "live.approval_resolved",
            request_id=request_id,
            approved=approved,
            operator=operator,
        )
        return True

    def _pending_approvals_unsafe(self) -> list[dict[str, object]]:
        """
        Return unresolved approvals, pruning stale resolved entries.

        L-03: renamed to _pending_approvals_unsafe to signal no locking.
        External callers should use pending_approvals() which delegates to
        the safe async variant. Only call this from within a held lock context.
        """
        cutoff = time.monotonic() - 3600.0
        to_prune = [
            rid
            for rid, req in self._approval_queue.items()
            if req.resolved and req.created_at < cutoff
        ]
        for rid in to_prune:
            self._approval_queue.pop(rid, None)
        return [req.to_dict() for req in self._approval_queue.values() if not req.resolved]

    def pending_approvals(self) -> list[dict[str, object]]:
        """Public interface — delegates to unsafe variant (sync callers only)."""
        return self._pending_approvals_unsafe()

    async def open_positions_safe(self) -> list[dict[str, object]]:
        """Thread-safe snapshot of open positions for WS heartbeat (VUL-035)."""
        async with self._lock:
            return list(self._open_positions_snapshot())

    async def pending_approvals_safe(self) -> list[dict[str, object]]:
        """Thread-safe snapshot of pending approvals for WS heartbeat (VUL-035)."""
        async with self._lock:
            cutoff = time.monotonic() - 3600.0
            to_prune = [
                rid
                for rid, req in self._approval_queue.items()
                if req.resolved and req.created_at < cutoff
            ]
            for rid in to_prune:
                self._approval_queue.pop(rid, None)
            return [req.to_dict() for req in self._approval_queue.values() if not req.resolved]

    def _open_positions_snapshot(self) -> list[dict[str, object]]:
        """Build positions list — must be called with self._lock held."""
        return [
            {
                "trade_id": p.trade_id,
                "exchange_order_id": p.exchange_order_id,
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
                "peak_unrealized_pct": round(p.peak_unrealized_pct, 3),
                "regime_at_entry": p.regime_at_entry,
                "strategy_id": p.strategy_id,
                "entry_ts": p.entry_ts,
            }
            for p in self._positions.values()
        ]

    # ------------------------------------------------------------------
    # State queries
    # ------------------------------------------------------------------

    def open_positions(self) -> list[dict[str, object]]:
        """Unsynchronized snapshot — safe only when no concurrent mutation expected."""
        return self._open_positions_snapshot()

    @property
    def cash_usd(self) -> float:
        return self._cash

    @property
    def idempotency(self) -> IdempotencyRegistry:
        # Delegated to the order manager rather than duplicated: the manager
        # owns the submission path, and a second registry here would be a
        # second source of truth about what has already been sent.
        return self._order_manager.idempotency

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
        self._log.info("live.daily_equity_reset", equity_usd=round(equity, 2))
        return equity

    async def get_risk_snapshot(self) -> tuple[float, float, float]:
        """C-08: Atomically return (equity_usd, starting_equity_usd, daily_pnl_usd)."""
        async with self._lock:
            equity = self._equity_usd()
            start_eq = self._drawdown_tracker.daily_start_equity
            daily_pnl = self._drawdown_tracker.daily_pnl_usd
        return equity, start_eq, daily_pnl

    def position_count(self) -> int:
        return len(self._positions)

    async def get_consecutive_losses(self, symbol: str) -> int:
        return await self._storage.count_consecutive_losses(symbol, TradingMode.LIVE.value)

    async def get_daily_pnl(self, symbol: str) -> float:
        return await self._storage.daily_pnl(symbol, TradingMode.LIVE.value)

    # ------------------------------------------------------------------
    # Internal order placement
    # ------------------------------------------------------------------

    async def _place_and_record(
        self,
        symbol: str,
        timeframe: str,
        direction: int,
        kelly_result: KellyResult,
        regime_state: int,
        meta_label_prob: float,
        raw_signal: float,
        approved_by: str,
        strategy_id: str = "signal_engine_v1",
        blend_audit: BlendAudit | None = None,
    ) -> str | None:
        """Place market order, confirm fill, record position.

        Serialized by _trade_semaphore so concurrent signals cannot both
        pass the cash pre-check against the same balance (VUL-009).
        """
        async with self._trade_semaphore:
            # Pre-check and reserve cash BEFORE placing the order (fix #9)
            notional_estimate = kelly_result.notional_usd
            fee_estimate = notional_estimate * _LIVE_FEE_FALLBACK
            async with self._lock:
                if self._cash < notional_estimate + fee_estimate:
                    self._log.warning(
                        "live.insufficient_cash_pre_check",
                        cash=round(self._cash, 2),
                        needed=round(notional_estimate + fee_estimate, 2),
                    )
                    return None
                # Reserve the estimated amount to prevent concurrent over-spend
                self._cash -= notional_estimate + fee_estimate

            side = "buy" if direction == 1 else "sell"
            try:
                # No intent_id: an entry has no pre-existing identity to pin
                # to, and minting a uuid here would defeat the purpose (a
                # fresh uuid per attempt makes every duplicate look unique).
                # The time bucket is the identity instead -- the same signal
                # re-issued within the bucket collides and is refused, while
                # a genuinely new signal for the same symbol later does not.
                order = await self._place_market_order(
                    symbol,
                    side,
                    kelly_result.quantity,
                    purpose="entry",
                    strategy_id=strategy_id,
                )
            except (ccxt.NetworkError, ccxt.ExchangeError) as exc:
                # Restore reserved cash on order failure
                async with self._lock:
                    self._cash += notional_estimate + fee_estimate
                self._log.error(
                    "live.entry_order_failed",
                    symbol=symbol,
                    side=side,
                    error=str(exc),
                    exc_info=True,
                )
                return None

            actual_price = float(order.get("average") or order.get("price") or 0.0)
            # Same trap as the close leg: `or` turns a reported fill of 0.0
            # into the requested size, so an order that filled nothing was
            # recorded as a full position. Only a missing value falls back.
            _filled_raw = order.get("filled")
            filled_qty = float(kelly_result.quantity) if _filled_raw is None else float(_filled_raw)
            exchange_order_id = str(order.get("id", ""))
            entry_fee = self._extract_fee(order, actual_price, filled_qty)

            if actual_price <= 0.0 or filled_qty <= 0.0:
                # Restore reserved cash — fill data unusable
                async with self._lock:
                    self._cash += notional_estimate + fee_estimate
                self._log.error(
                    "live.bad_fill",
                    symbol=symbol,
                    actual_price=actual_price,
                    filled_qty=filled_qty,
                    order_id=exchange_order_id,
                )
                return None

            notional = actual_price * filled_qty
            entry_ts = int(datetime.now(tz=UTC).timestamp() * 1000)
            trade_id = str(uuid.uuid4())

            async with self._lock:
                # Reconcile: replace the estimated reserve with actual cost
                self._cash += notional_estimate + fee_estimate  # undo estimate
                cash_insufficient = self._cash < notional + entry_fee
                if not cash_insufficient:
                    self._cash -= notional + entry_fee

            if cash_insufficient:
                self._log.critical(
                    "live.post_reconcile_cash_insufficient_UNTRACKED_POSITION",
                    exchange_order_id=exchange_order_id,
                    symbol=symbol,
                    side=side,
                    actual_price=actual_price,
                    filled_qty=filled_qty,
                    action="order filled but position not recorded — attempting emergency flatten",
                )
                # The exchange already filled this order; leaving it untracked
                # means real, unhedged exposure with no internal record. Rather
                # than abandon it, immediately submit an opposite-side market
                # order to flatten the position before returning.
                flatten_side = "sell" if side == "buy" else "buy"
                try:
                    flatten_order = await self._place_market_order(
                        symbol,
                        flatten_side,
                        filled_qty,
                        is_exit=True,
                        # Keyed to the entry order being undone, so this
                        # flatten cannot collide with the entry above (same
                        # symbol and quantity, opposite intent) and cannot
                        # fire twice for the same stranded fill.
                        purpose="emergency_flatten",
                        intent_id=exchange_order_id,
                        strategy_id=strategy_id,
                    )
                    flatten_price = float(
                        flatten_order.get("average") or flatten_order.get("price") or actual_price
                    )
                    flatten_fee = self._extract_fee(flatten_order, flatten_price, filled_qty)
                    # self._cash was already reset to its pre-trade value above
                    # (the estimate was undone and never re-debited). The real
                    # cash effect of entering then immediately flattening is the
                    # signed price move between the two fills (relative to the
                    # entry side) minus both legs' fees — without applying it,
                    # the internal ledger would silently drift away from the
                    # true exchange balance every time this path fires, letting
                    # future trades size against phantom cash.
                    signed_pnl = (flatten_price - actual_price) * filled_qty
                    if side == "sell":
                        signed_pnl = -signed_pnl
                    cash_adjustment = signed_pnl - entry_fee - flatten_fee
                    async with self._lock:
                        self._cash += cash_adjustment
                    self._log.error(
                        "live.untracked_position_flattened",
                        exchange_order_id=exchange_order_id,
                        symbol=symbol,
                        flatten_side=flatten_side,
                        filled_qty=filled_qty,
                        cash_adjustment=round(cash_adjustment, 4),
                    )
                except (ccxt.NetworkError, ccxt.ExchangeError) as exc:
                    self._log.critical(
                        "live.untracked_position_flatten_failed",
                        exchange_order_id=exchange_order_id,
                        symbol=symbol,
                        error=str(exc),
                        action="MANUAL_CLOSE_REQUIRED",
                        exc_info=True,
                    )
                return None

            async with self._lock:
                pos = LivePosition(
                    trade_id=trade_id,
                    exchange_order_id=exchange_order_id,
                    symbol=symbol,
                    timeframe=timeframe,
                    direction=direction,
                    entry_price=actual_price,
                    quantity=filled_qty,
                    notional_usd=notional,
                    entry_ts=entry_ts,
                    kelly_fraction=kelly_result.adjusted_fraction,
                    regime_at_entry=regime_state,
                    meta_label_prob=meta_label_prob,
                    raw_signal=raw_signal,
                    approved_by=approved_by,
                    execution_mode=self._cfg.execution_mode.value,
                    fee_usd=entry_fee,
                    strategy_id=strategy_id,
                    current_price=actual_price,
                )
                self._positions[trade_id] = pos

            trade_record = TradeRecord(
                id=trade_id,
                symbol=symbol,
                timeframe=timeframe,
                trading_mode=TradingMode.LIVE.value,
                execution_mode=self._cfg.execution_mode.value,
                direction=direction,
                entry_price=actual_price,
                exit_price=None,
                quantity=filled_qty,
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
                pre_blend_p_long=blend_audit.pre_blend_p_long if blend_audit else None,
                ensemble_p_long=blend_audit.ensemble_p_long if blend_audit else None,
                ensemble_blend_weight=blend_audit.blend_weight if blend_audit else None,
            )
            await self._storage.insert_trade(trade_record)
            await self._snapshot_equity()

            self._log.info(
                "live.position_opened",
                trade_id=trade_id,
                exchange_order_id=exchange_order_id,
                symbol=symbol,
                direction="long" if direction == 1 else "short",
                entry_price=round(actual_price, 4),
                quantity=filled_qty,
                notional_usd=round(notional, 2),
                approved_by=approved_by,
            )
            return trade_id

    async def _await_throttle_token(self, exchange_id: str, *, is_exit: bool = False) -> None:
        """
        Hold the order until the token bucket allows it, or refuse it.

        Exchanges answer request-weight bursts with HTTP 429 and then a
        temporary IP ban, which would strand any open position with no way to
        exit — so a short wait is strictly better than firing the order.
        Past ``max_wait_s`` the entry price the signal was sized against is
        stale, and filling anyway would mean trading a price the risk layer
        never approved, so the order is refused instead.

        ``is_exit`` orders are never refused and never delayed. Refusing an
        exit leaves real, unhedged exposure open — strictly worse than any
        rate-limit consequence, and unlike an entry there is no "skip it and
        wait for the next signal" fallback. The token is still consumed so
        the entry budget shrinks to account for the request, and a would-be
        rejection is logged rather than raised.

        Raises ccxt.ExchangeError when a non-exit order's required wait
        exceeds max_wait_s.
        """
        if not self._throttle_cfg.enabled:
            return

        result = self._throttler.acquire(exchange_id)
        if result.allowed:
            return

        if is_exit:
            self._log.warning(
                "live.exit_order_bypassed_throttle",
                exchange=exchange_id,
                wait_s=round(result.wait_s, 3),
                reason="exit orders are never refused — unhedged exposure beats a 429",
            )
            return

        if result.wait_s > self._throttle_cfg.max_wait_s:
            self._log.error(
                "live.order_throttled_reject",
                exchange=exchange_id,
                wait_s=round(result.wait_s, 3),
                max_wait_s=self._throttle_cfg.max_wait_s,
            )
            raise ccxt.ExchangeError(
                f"Order rate limit for {exchange_id}: {result.wait_s:.3f}s backlog "
                f"exceeds max_wait_s={self._throttle_cfg.max_wait_s}s — order refused "
                f"rather than filled at a stale price."
            )

        self._log.warning(
            "live.order_throttled_wait",
            exchange=exchange_id,
            wait_s=round(result.wait_s, 3),
        )
        # +1ms: wait_s refills exactly one token, so sleeping the bare amount
        # can land a float-epsilon short and fail the retry for no reason.
        await asyncio.sleep(result.wait_s + 0.001)
        # One retry: the sleep covers the refill of exactly one token, and the
        # trade semaphore serialises order placement, so a second rejection
        # here means the clock moved against us rather than a real backlog.
        retry = self._throttler.acquire(exchange_id)
        if not retry.allowed:
            raise ccxt.ExchangeError(
                f"Order rate limit for {exchange_id}: token still unavailable after "
                f"waiting {result.wait_s:.3f}s — order refused."
            )

    async def _place_market_order(
        self,
        symbol: str,
        side: str,
        quantity: float,
        *,
        is_exit: bool = False,
        purpose: str,
        intent_id: str | None = None,
        strategy_id: str = "live_executor",
    ) -> dict[str, Any]:
        """
        Place a market order and wait for fill confirmation.

        ``is_exit`` marks an order that closes or flattens existing exposure;
        it bypasses order-rate refusal (see _await_throttle_token).

        ``purpose`` and ``intent_id`` define the order's identity for LAW3
        de-duplication -- see :func:`derive_idempotency_key`. Both are keyword
        only and ``purpose`` is required, so a new call site cannot inherit a
        default that silently collides with an unrelated order.

        Now uses OrderFSM via OrderManager for:
          - State machine driven confirmation
          - Partial fill aggregation
          - Timeout escalation
          - Network error recovery
          - State persistence for manual reconciliation

        Returns the confirmed order dict.

        Raises ccxt.ExchangeError if order does not fill within timeout.
        """
        exchange = self._fetcher.get_order_exchange()

        idempotency_key = derive_idempotency_key(
            strategy_id=strategy_id,
            symbol=symbol,
            side=side,
            quantity=quantity,
            purpose=purpose,
            intent_id=intent_id,
        )

        await self._await_throttle_token(getattr(exchange, "id", "binance"), is_exit=is_exit)

        try:
            fsm, confirmed_order = await self._order_manager.place_order_with_fsm(
                exchange=exchange,
                symbol=symbol,
                side=side,
                quantity=quantity,
                idempotency_key=idempotency_key,
            )

            self._log.info(
                "live.order_placed_fsm",
                order_id=fsm.state.order_id,
                idempotency_key=idempotency_key,
                symbol=symbol,
                side=side,
                quantity=quantity,
                filled_qty=fsm.state.filled_qty,
                avg_price=round(fsm.state.average_fill_price or 0, 4),
                attempts=fsm.state.retry_count,
            )

            self._register_order_fsm(fsm)
            return confirmed_order

        except DuplicateOrderError as exc:
            # A retry, reconnect or reconciliation pass reached the same order
            # intent twice. This is the guard working, not a fault: refuse the
            # second submission rather than doubling live exposure.
            self._log.warning(
                "live.duplicate_order_suppressed",
                symbol=symbol,
                side=side,
                quantity=quantity,
                purpose=purpose,
                idempotency_key=idempotency_key,
                prior_order_id=exc.record.order_id,
                prior_state=exc.record.state.value,
            )
            raise ccxt.ExchangeError(
                f"Duplicate order suppressed for {symbol} ({purpose}): idempotency key "
                f"{idempotency_key} already submitted as order {exc.record.order_id!r}."
            ) from exc
        except TimeoutError as exc:
            self._log.error(
                "live.order_confirmation_timeout",
                symbol=symbol,
                side=side,
                quantity=quantity,
                idempotency_key=idempotency_key,
                action="manual_reconciliation_required",
                exc_info=True,
            )
            raise ccxt.ExchangeError(
                f"Order for {symbol} did not confirm as filled within timeout — "
                f"check exchange manually. See logs for order_id."
            ) from exc
        except ccxt.ExchangeError:
            # Already logged by OrderManager
            raise

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
            "live.approval_queued",
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
        Wait for approval resolution using asyncio.Event — zero poll overhead.

        Replaces the 250ms busy-poll loop that acquired self._lock 4x/second,
        starving concurrent mark_to_market and gate evaluation (VUL-024).
        """
        async with self._lock:
            req = self._approval_queue.get(request_id)
        if req is None:
            self._log.error("live.approval_missing_in_queue", request_id=request_id)
            return False, ""

        try:
            await asyncio.wait_for(req._event.wait(), timeout=timeout_s)
        except TimeoutError:
            async with self._lock:
                timed_out = self._approval_queue.pop(request_id, None)
                if timed_out is not None:
                    timed_out.resolved = True
                    timed_out.approved = False
                    timed_out.operator = "auto_timeout"
            self._log.warning("live.approval_timeout", request_id=request_id)
            return False, "auto_timeout"

        async with self._lock:
            resolved = self._approval_queue.pop(request_id, None)
        if resolved is None:
            return False, ""
        return resolved.approved, resolved.operator

    def _equity_usd(self) -> float:
        return self._cash + sum(p.unrealized_pnl for p in self._positions.values())

    async def _snapshot_equity(self) -> None:
        """Write current equity state to storage.

        UI-003: re-reads self._positions/self._cash under self._lock so an
        `await` between a caller's own lock release and this call cannot
        let a concurrent mark_to_market/close_position mutate state
        in between and produce an internally-inconsistent equity row --
        same fix as PaperExecutor._snapshot_equity (execution/paper.py).
        """
        async with self._lock:
            equity = self._equity_usd()
            unrealized = sum(p.unrealized_pnl for p in self._positions.values())
            cash = self._cash
            peak_equity = self._peak_equity
            daily_pnl = self._drawdown_tracker.daily_pnl_usd
            daily_pct = self._drawdown_tracker.daily_pnl_pct
            dd_pct = self._drawdown_tracker.drawdown_from_peak_pct
        await self._snapshot_equity_with_values(
            equity=equity,
            cash=cash,
            unrealized=unrealized,
            daily_pnl=daily_pnl,
            daily_pct=daily_pct,
            dd_pct=dd_pct,
            peak_equity=peak_equity,
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

        SCAN3-001/SCAN3-002: Called with values captured inside the lock so the
        record is internally consistent and cannot race with mark_to_market().
        """
        record = EquityRecord(
            ts=int(datetime.now(tz=UTC).timestamp() * 1000),
            trading_mode=TradingMode.LIVE.value,
            equity_usd=round(equity, 8),
            cash_usd=round(cash, 8),
            unrealized_pnl=round(unrealized, 8),
            daily_pnl_usd=round(daily_pnl, 8),
            daily_pnl_pct=round(daily_pct, 8),
            peak_equity_usd=round(peak_equity, 8),
            drawdown_pct=round(dd_pct, 8),
        )
        await self._storage.insert_equity(record)

    def _register_order_fsm(self, fsm: Any) -> None:
        """
        Store a completed/terminal order's FSM state for later reconciliation
        lookups via get_order_fsm_state(). Bounded to
        _ORDER_FSM_REGISTRY_MAX_SIZE entries (oldest evicted first) -- this
        is a short-term debugging aid, not durable storage (see the comment
        on _ORDER_FSM_REGISTRY_MAX_SIZE above).
        """
        order_id = fsm.state.order_id
        self._order_fsm_registry[order_id] = fsm.state
        self._order_fsm_registry.move_to_end(order_id)
        while len(self._order_fsm_registry) > _ORDER_FSM_REGISTRY_MAX_SIZE:
            self._order_fsm_registry.popitem(last=False)

    async def get_order_fsm_state(self, order_id: str) -> Any | None:
        """
        Look up a recent order's FSM state snapshot, for the
        GET /orders/{order_id}/status reconciliation endpoint.

        Returns None if the order_id isn't in the bounded registry --
        either it was never placed by this process, or it has aged out
        (see _ORDER_FSM_REGISTRY_MAX_SIZE).
        """
        return self._order_fsm_registry.get(order_id)

    def _extract_fee(self, order: dict, price: float, qty: float) -> float:
        """Extract exchange fee from a ccxt order dict.

        ccxt returns fees as a list of dicts with 'cost' (amount) and
        'currency' keys.  We sum costs that appear to be in the quote
        currency (USDT/USDC/BUSD/USD) and fall back to the flat-rate
        estimate when the field is absent or unusable.

        Ref: ccxt unified order structure — order['fees'] (list) or
             order['fee'] (single dict, older exchange responses).
        """
        fees: list[dict] = order.get("fees") or []
        if not fees:
            single = order.get("fee")
            if isinstance(single, dict):
                fees = [single]
        total = 0.0
        for f in fees:
            cost = f.get("cost")
            currency = str(f.get("currency", "")).upper()
            if cost is not None and currency in {"USDT", "USDC", "BUSD", "USD", ""}:
                with contextlib.suppress(TypeError, ValueError):
                    total += float(cost)
        if total > 0.0:
            return total
        # Fallback: estimate from notional
        return price * qty * _LIVE_FEE_FALLBACK

    def _require_initialized(self) -> None:
        if not self._initialized:
            raise RuntimeError(
                "LiveExecutor not initialized — call await executor.initialize() first."
            )
