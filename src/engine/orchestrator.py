"""
Orchestrator — manages all active signal engines, approval queue,
execution routing, and position lifecycle.
"""
from __future__ import annotations
import asyncio
from datetime import datetime, timezone
from typing import Callable, Awaitable, Any
import structlog

from src.config            import get_settings, Settings
from src.data.fetcher      import OHLCVFetcher
from src.data.storage      import Database
from src.risk.gates        import RiskGate
from src.execution.paper   import PaperExecutor
from src.execution.live    import LiveExecutor
from src.engine.signal_engine import SignalEngine

log = structlog.get_logger()

class ApprovalRequest:
    def __init__(self, signal: dict, timeout: int):
        self.signal   = signal
        self.timeout  = timeout
        self._event   = asyncio.Event()
        self._approved: bool | None = None

    async def wait(self) -> bool:
        try:
            await asyncio.wait_for(self._event.wait(), timeout=self.timeout)
            return self._approved if self._approved is not None else False
        except asyncio.TimeoutError:
            log.info("approval timed out — skipping", symbol=self.signal["symbol"])
            return False

    def resolve(self, approved: bool):
        self._approved = approved
        self._event.set()


class Orchestrator:
    def __init__(self, db: Database, broadcast: Callable[[dict], Awaitable[None]]):
        self._db         = db
        self._broadcast  = broadcast
        self._engines:   dict[str, SignalEngine] = {}
        self._paper_exec: PaperExecutor | None   = None
        self._live_exec:  LiveExecutor | None    = None
        self._risk_gate:  RiskGate | None        = None
        self._approval_queue: dict[str, ApprovalRequest] = {}
        self._running    = False

    async def start(self, cfg: Settings):
        self._cfg      = cfg
        self._risk_gate = RiskGate(
            daily_drawdown_halt_pct=cfg.daily_drawdown_halt_pct,
            consecutive_loss_halt=cfg.consecutive_loss_halt,
            max_position_pct=cfg.max_position_pct,
        )
        self._paper_exec = PaperExecutor(initial_capital=cfg.paper_capital)
        self._risk_gate.start_session(cfg.paper_capital)

        if cfg.trading_mode == "live":
            self._live_exec = LiveExecutor(
                exchange_id=cfg.active_exchanges[0],
                api_key=cfg.binance_api_key,
                api_secret=cfg.binance_api_secret,
                paper_mode=False,
            )

        self._running = True
        await self._rebuild_engines(cfg)

    async def _rebuild_engines(self, cfg: Settings):
        """Start/stop engines based on current active_exchanges x active_timeframes x symbols."""
        for engine in self._engines.values():
            engine.stop()
        self._engines.clear()

        for exchange_id in cfg.active_exchanges:
            api_key    = cfg.binance_api_key    if exchange_id == "binance" else cfg.okx_api_key
            api_secret = cfg.binance_api_secret if exchange_id == "binance" else cfg.okx_api_secret
            passphrase = ""                      if exchange_id == "binance" else cfg.okx_passphrase

            fetcher = OHLCVFetcher(exchange_id, api_key, api_secret, passphrase)

            for symbol in cfg.symbols:
                for timeframe in cfg.active_timeframes:
                    key = f"{exchange_id}:{symbol}:{timeframe}"
                    engine = SignalEngine(
                        exchange_id=exchange_id,
                        symbol=symbol,
                        timeframe=timeframe,
                        fetcher=fetcher,
                        risk_gate=self._risk_gate,
                        on_signal=self._on_signal,
                    )
                    self._engines[key] = engine
                    asyncio.create_task(engine.run(), name=key)
                    log.info("engine started", key=key)

    async def _on_signal(self, signal: dict):
        cfg = get_settings()

        # Store signal
        signal_id = await self._db.insert_signal(
            ts=signal["ts"], exchange=signal["exchange"], symbol=signal["symbol"],
            timeframe=signal["timeframe"], direction=signal["direction"],
            confidence=signal["confidence"], meta_score=signal["meta_score"],
            regime=signal["regime"], kelly_frac=signal["kelly_frac"],
            notional=signal["notional"], status="pending",
        )
        signal["signal_id"] = signal_id

        # Broadcast to dashboard
        await self._broadcast({"type": "signal", "data": signal})

        # Execution mode routing
        mode = cfg.execution_mode

        if mode == "automatic":
            await self._execute(signal, signal_id)

        elif mode == "manual":
            req = ApprovalRequest(signal, cfg.approval_timeout_secs)
            self._approval_queue[str(signal_id)] = req
            await self._broadcast({"type": "approval_request", "data": {**signal, "signal_id": signal_id}})
            approved = await req.wait()
            self._approval_queue.pop(str(signal_id), None)
            if approved:
                await self._execute(signal, signal_id)

        elif mode == "restricted":
            if signal["notional"] <= cfg.restricted_notional_limit:
                await self._execute(signal, signal_id)
            else:
                req = ApprovalRequest(signal, cfg.approval_timeout_secs)
                self._approval_queue[str(signal_id)] = req
                await self._broadcast({"type": "approval_request", "data": {**signal, "signal_id": signal_id}})
                approved = await req.wait()
                self._approval_queue.pop(str(signal_id), None)
                if approved:
                    await self._execute(signal, signal_id)

    async def _execute(self, signal: dict, signal_id: int):
        cfg   = get_settings()
        price = signal["price"]
        qty   = signal["notional"] / price if price > 0 else 0.0
        sym   = signal["symbol"]
        ts    = datetime.now(timezone.utc).isoformat()

        if cfg.trading_mode == "paper" or self._live_exec is None:
            fill = self._paper_exec.open_position(
                symbol=sym, direction=signal["direction"],
                qty=qty, price=price, trade_id=signal_id,
            )
        else:
            order = await self._live_exec.place_order(
                symbol=sym, side="buy" if signal["direction"] == "long" else "sell",
                qty=qty, price=price,
            )
            fill = float(order.get("price", price))

        trade_id = await self._db.insert_trade(
            signal_id=signal_id, ts_open=ts, exchange=signal["exchange"],
            symbol=sym, timeframe=signal["timeframe"], direction=signal["direction"],
            entry_price=fill, qty=qty, notional=signal["notional"],
            status="open", mode=cfg.trading_mode,
        )
        await self._broadcast({"type": "trade_opened", "data": {
            "trade_id": trade_id, "symbol": sym,
            "direction": signal["direction"], "fill": fill,
            "qty": round(qty, 6), "notional": round(signal["notional"], 2),
        }})

    async def resolve_approval(self, signal_id: str, approved: bool):
        req = self._approval_queue.get(signal_id)
        if req:
            req.resolve(approved)

    async def reconfigure(self, **kwargs):
        """Apply runtime config changes and rebuild engines if topology changes."""
        from src.config import update_settings
        cfg = update_settings(**kwargs)
        topology_keys = {"active_exchanges", "active_timeframes", "symbols"}
        if topology_keys.intersection(kwargs.keys()):
            await self._rebuild_engines(cfg)
        if self._risk_gate:
            risk_keys = {"daily_drawdown_halt_pct", "consecutive_loss_halt", "max_position_pct"}
            self._risk_gate.update_params(**{k: v for k, v in kwargs.items() if k in risk_keys})

    def status(self) -> dict[str, Any]:
        cfg = get_settings()
        equity = self._paper_exec.equity if self._paper_exec else 0.0
        return {
            "running":          self._running,
            "trading_mode":     cfg.trading_mode,
            "execution_mode":   cfg.execution_mode,
            "active_timeframes": cfg.active_timeframes,
            "active_exchanges": cfg.active_exchanges,
            "symbols":          cfg.symbols,
            "equity":           round(equity, 2),
            "halted":           self._risk_gate.session.halted if self._risk_gate else False,
            "halt_reason":      self._risk_gate.session.halt_reason if self._risk_gate else "",
            "session_pnl_pct":  round(self._risk_gate.session.daily_pnl_pct * 100, 3) if self._risk_gate else 0.0,
            "engines_active":   len(self._engines),
            "pending_approvals": len(self._approval_queue),
        }

    def open_positions(self) -> list[dict[str, Any]]:
        if self._paper_exec:
            return self._paper_exec.open_positions()
        return []

    async def resume(self):
        if self._risk_gate:
            self._risk_gate.resume()

