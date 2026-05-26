"""
Paper trading execution — simulates fills at market price with realistic slippage.
Tracks virtual portfolio in memory + persists to DB.
"""
from __future__ import annotations
from datetime import datetime, timezone
from dataclasses import dataclass, field
from typing import Literal
import structlog

log = structlog.get_logger()

SLIPPAGE_BPS = 5      # 5 basis points slippage simulation
FEE_TAKER_BPS = 7     # Binance taker fee ~0.07%

@dataclass
class PaperPosition:
    symbol:      str
    direction:   Literal["long", "short"]
    entry_price: float
    qty:         float
    notional:    float
    ts_open:     datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    trade_id:    int = 0

class PaperExecutor:
    def __init__(self, initial_capital: float = 1000.0):
        self._capital:   float = initial_capital
        self._positions: dict[str, PaperPosition] = {}
        self._equity:    float = initial_capital

    @property
    def capital(self) -> float:
        return self._capital

    @property
    def equity(self) -> float:
        return self._equity

    def open_position(
        self,
        symbol:    str,
        direction: Literal["long", "short"],
        qty:       float,
        price:     float,
        trade_id:  int = 0,
    ) -> float:
        """Simulate open. Returns actual fill price after slippage."""
        slip = price * SLIPPAGE_BPS / 10000
        fee  = price * qty * FEE_TAKER_BPS / 10000
        fill = price + (slip if direction == "long" else -slip)
        notional = fill * qty
        self._capital  -= (notional + fee)
        self._positions[symbol] = PaperPosition(
            symbol=symbol, direction=direction,
            entry_price=fill, qty=qty, notional=notional, trade_id=trade_id,
        )
        log.info("paper open", symbol=symbol, direction=direction, fill=round(fill,4),
                 qty=round(qty,6), notional=round(notional,2))
        return fill

    def close_position(
        self,
        symbol: str,
        price:  float,
        reason: str = "signal",
    ) -> tuple[float, float, float]:
        """
        Close open position.
        Returns (exit_price, pnl, pnl_pct).
        """
        if symbol not in self._positions:
            return price, 0.0, 0.0

        pos  = self._positions.pop(symbol)
        slip = price * SLIPPAGE_BPS / 10000
        fee  = price * pos.qty * FEE_TAKER_BPS / 10000
        fill = price - (slip if pos.direction == "long" else -slip)

        if pos.direction == "long":
            pnl = (fill - pos.entry_price) * pos.qty - fee
        else:
            pnl = (pos.entry_price - fill) * pos.qty - fee

        pnl_pct = pnl / pos.notional if pos.notional > 0 else 0.0
        self._capital += pos.notional + pnl
        self._equity   = self._capital
        log.info("paper close", symbol=symbol, reason=reason, fill=round(fill,4),
                 pnl=round(pnl,4), pnl_pct=f"{pnl_pct:.3%}")
        return fill, pnl, pnl_pct

    def mark_to_market(self, prices: dict[str, float]) -> float:
        """Recalculate equity using current prices."""
        unrealized = 0.0
        for sym, pos in self._positions.items():
            if sym in prices:
                if pos.direction == "long":
                    unrealized += (prices[sym] - pos.entry_price) * pos.qty
                else:
                    unrealized += (pos.entry_price - prices[sym]) * pos.qty
        self._equity = self._capital + unrealized
        return self._equity

    def open_positions(self) -> list[dict]:
        return [
            {
                "symbol":      p.symbol,
                "direction":   p.direction,
                "entry_price": p.entry_price,
                "qty":         p.qty,
                "notional":    p.notional,
                "ts_open":     p.ts_open.isoformat(),
            }
            for p in self._positions.values()
        ]

