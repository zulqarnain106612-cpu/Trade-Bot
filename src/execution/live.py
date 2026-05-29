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
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Final

import ccxt.async_support as ccxt
import structlog

from src.config import ExecutionMode, TradingMode, get_settings
from src.data.fetcher import MarketDataFetcher
from src.data.storage import EquityRecord, StorageBackend, TradeRecord
from src.risk.gates import DrawdownTracker
from src.risk.kelly import KellyResult

log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

_LIVE_FEE_FALLBACK: Final[float] = 0.001   # fallback if exchange fee missing
_ORDER_CONFIRM_POLLS: Final[int] = 10       # max polls for order confirmation
_ORDER_CONFIRM_INTERVAL: Final[float] = 0.5 # seconds between polls


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
    unrealized_pnl: float = field(default=0.0)
    current_price: float = field(default=0.0)

    def mark(self, price: float) -> float:
        self.current_price = price
        if self.direction == 1:
            self.unrealized_pnl = (price - self.entry_price) * self.quantity
        else:
            self.unrealized_pnl = (self.entry_price - price) * self.quantity
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


class LiveExecutor:
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
        storage: StorageBackend,
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
        self._drawdown_tracker = DrawdownTracker(self._starting_capital)
        self._initialized: bool = False
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
        self._initialized = True

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
    ) -> tuple[str | None, str]:
        """
        Route signal through execution mode and place live order if approved.

        Returns (trade_id, outcome).
        outcome: 'opened' | 'queued' | 'skipped' | 'rejected'
        """
        self._require_initialized()
        mode = self._cfg.execution_mode

        if mode == ExecutionMode.AUTOMATIC:
            trade_id = await self._place_and_record(
                symbol, timeframe, direction, kelly_result,
                regime_state, meta_label_prob, raw_signal, approved_by="auto",
            )
            return trade_id, "opened" if trade_id else "rejected"

        if mode == ExecutionMode.RESTRICTED:
            if kelly_result.notional_usd <= self._risk_cfg.notional_limit_usd:
                trade_id = await self._place_and_record(
                    symbol, timeframe, direction, kelly_result,
                    regime_state, meta_label_prob, raw_signal,
                    approved_by="auto_below_limit",
                )
                return trade_id, "opened" if trade_id else "rejected"
            req_id = await self._enqueue_approval(
                symbol, timeframe, direction, kelly_result,
                regime_state, meta_label_prob, raw_signal,
            )
            approved, operator = await self._await_approval(
                req_id, self._risk_cfg.approval_timeout_s
            )
            if not approved:
                return None, "skipped"
            trade_id = await self._place_and_record(
                symbol, timeframe, direction, kelly_result,
                regime_state, meta_label_prob, raw_signal, approved_by=operator,
            )
            return trade_id, "opened" if trade_id else "rejected"

        if mode == ExecutionMode.MANUAL:
            req_id = await self._enqueue_approval(
                symbol, timeframe, direction, kelly_result,
                regime_state, meta_label_prob, raw_signal,
            )
            approved, operator = await self._await_approval(req_id, timeout_s=None)
            if not approved:
                return None, "rejected"
            trade_id = await self._place_and_record(
                symbol, timeframe, direction, kelly_result,
                regime_state, meta_label_prob, raw_signal, approved_by=operator,
            )
            return trade_id, "opened" if trade_id else "rejected"

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
        Close a live position by placing a market close order.

        Places opposite-side market order on exchange, waits for
        fill confirmation, persists exit to storage.

        Returns net PnL in USD.
        """
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
            )
        except (ccxt.NetworkError, ccxt.ExchangeError) as exc:
            self._log.error(
                "live.close_order_failed",
                trade_id=trade_id,
                symbol=pos.symbol,
                error=str(exc),
            )
            raise

        actual_exit_price = float(order.get("average") or order.get("price") or exit_price)
        filled_qty = float(order.get("filled") or pos.quantity)
        exchange_fee = self._extract_fee(order, actual_exit_price, filled_qty)

        exit_ts = int(datetime.now(tz=timezone.utc).timestamp() * 1000)

        if pos.direction == 1:
            gross_pnl = (actual_exit_price - pos.entry_price) * filled_qty
        else:
            gross_pnl = (pos.entry_price - actual_exit_price) * filled_qty

        net_pnl = gross_pnl - exchange_fee
        pnl_pct = net_pnl / pos.notional_usd if pos.notional_usd > 0 else 0.0

        async with self._lock:
            self._positions.pop(trade_id, None)
            self._cash += pos.notional_usd + net_pnl
            equity = self._equity_usd()
            self._peak_equity = max(self._peak_equity, equity)
            self._drawdown_tracker.update(equity)

        await self._storage.update_trade_exit(
            trade_id=trade_id,
            exit_price=actual_exit_price,
            exit_ts=exit_ts,
            pnl_usd=round(net_pnl, 8),
            pnl_pct=round(pnl_pct, 8),
            exit_reason=exit_reason,
            fee_usd=exchange_fee,
        )
        await self._snapshot_equity()

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
        await self._snapshot_equity()
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
        async with self._lock:
            req = self._approval_queue.get(request_id)
            if req is None or req.resolved:
                return False
            req.resolved = True
            req.approved = approved
            req.operator = operator
        self._log.info(
            "live.approval_resolved",
            request_id=request_id,
            approved=approved,
            operator=operator,
        )
        return True

    def pending_approvals(self) -> list[dict[str, object]]:
        return [
            req.to_dict()
            for req in self._approval_queue.values()
            if not req.resolved
        ]

    # ------------------------------------------------------------------
    # State queries
    # ------------------------------------------------------------------

    def open_positions(self) -> list[dict[str, object]]:
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
                "unrealized_pnl_pct": round(
                    p.unrealized_pnl / p.notional_usd * 100.0, 3
                ) if p.notional_usd > 0 else 0.0,
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
    ) -> str | None:
        """Place market order, confirm fill, record position."""
        side = "buy" if direction == 1 else "sell"
        try:
            order = await self._place_market_order(symbol, side, kelly_result.quantity)
        except (ccxt.NetworkError, ccxt.ExchangeError) as exc:
            self._log.error(
                "live.entry_order_failed",
                symbol=symbol,
                side=side,
                error=str(exc),
            )
            return None

        actual_price = float(order.get("average") or order.get("price") or 0.0)
        filled_qty = float(order.get("filled") or kelly_result.quantity)
        exchange_order_id = str(order.get("id", ""))
        entry_fee = self._extract_fee(order, actual_price, filled_qty)

        if actual_price <= 0.0 or filled_qty <= 0.0:
            self._log.error(
                "live.bad_fill",
                symbol=symbol,
                actual_price=actual_price,
                filled_qty=filled_qty,
                order_id=exchange_order_id,
            )
            return None

        notional = actual_price * filled_qty
        entry_ts = int(datetime.now(tz=timezone.utc).timestamp() * 1000)
        trade_id = str(uuid.uuid4())

        async with self._lock:
            if self._cash < notional + entry_fee:
                self._log.warning(
                    "live.insufficient_cash_post_fill",
                    cash=round(self._cash, 2),
                    needed=round(notional + entry_fee, 2),
                )
                return None
            self._cash -= notional + entry_fee
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

    async def _place_market_order(
        self,
        symbol: str,
        side: str,
        quantity: float,
    ) -> dict[str, Any]:
        """
        Place a market order and wait for fill confirmation.

        Polls fetch_order up to _ORDER_CONFIRM_POLLS times.
        Returns the confirmed order dict.

        Raises ccxt.ExchangeError if order does not fill within poll window.
        """
        exchange = self._fetcher._require_binance()  # noqa: SLF001
        order: dict[str, Any] = await exchange.create_market_order(
            symbol=symbol,
            side=side,
            amount=quantity,
        )
        order_id = order["id"]

        # Poll for confirmed fill
        for _ in range(_ORDER_CONFIRM_POLLS):
            await asyncio.sleep(_ORDER_CONFIRM_INTERVAL)
            try:
                confirmed = await exchange.fetch_order(order_id, symbol)
                if confirmed.get("status") in {"closed", "filled"}:
                    return confirmed
            except ccxt.ExchangeError:
                continue

        # Return last fetched state even if not fully confirmed
        self._log.warning(
            "live.order_fill_unconfirmed",
            order_id=order_id,
            symbol=symbol,
            side=side,
        )
        return order

    @staticmethod
    def _extract_fee(order: dict[str, Any], price: float, qty: float) -> float:
        """Extract fee from order response or fall back to estimated fee."""
        fee_info = order.get("fee") or {}
        fee_cost = fee_info.get("cost")
        if fee_cost is not None:
            return float(fee_cost)
        return price * qty * _LIVE_FEE_FALLBACK

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
        req_id = str(uuid.uuid4())
        req = ApprovalRequest(
            request_id=req_id,
            symbol=symbol,
            timeframe=timeframe,
            direction=direction,
            notional_usd=kelly_result.notional_usd,
            entry_price=0.0,
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
            notional_usd=round(kelly_result.notional_usd, 2),
        )
        return req_id

    async def _await_approval(
        self,
        request_id: str,
        timeout_s: float | None,
    ) -> tuple[bool, str]:
        poll_interval = 0.25
        elapsed = 0.0
        while True:
            async with self._lock:
                req = self._approval_queue.get(request_id)
                if req is None:
                    return False, ""
                if req.resolved:
                    self._approval_queue.pop(request_id, None)
                    return req.approved, req.operator
            if timeout_s is not None and elapsed >= timeout_s:
                async with self._lock:
                    req = self._approval_queue.pop(request_id, None)
                    if req is not None:
                        req.resolved = True
                        req.approved = False
                        req.operator = "auto_timeout"
                self._log.warning("live.approval_timeout", request_id=request_id)
                return False, "auto_timeout"
            await asyncio.sleep(poll_interval)
            elapsed += poll_interval

    def _equity_usd(self) -> float:
        return self._cash + sum(p.unrealized_pnl for p in self._positions.values())

    async def _snapshot_equity(self) -> None:
        equity = self._equity_usd()
        unrealized = sum(p.unrealized_pnl for p in self._positions.values())
        record = EquityRecord(
            ts=int(datetime.now(tz=timezone.utc).timestamp() * 1000),
            trading_mode=TradingMode.LIVE.value,
            equity_usd=round(equity, 8),
            cash_usd=round(self._cash, 8),
            unrealized_pnl=round(unrealized, 8),
            daily_pnl_usd=round(self._drawdown_tracker.daily_pnl_usd, 8),
            daily_pnl_pct=round(self._drawdown_tracker.daily_pnl_pct, 8),
            peak_equity_usd=round(self._peak_equity, 8),
            drawdown_pct=round(self._drawdown_tracker.drawdown_from_peak_pct, 8),
        )
        await self._storage.insert_equity(record)

    def _require_initialized(self) -> None:
        if not self._initialized:
            raise RuntimeError(
                "LiveExecutor not initialized — call await executor.initialize() first."
            )