"""
Pre-trade and session-level risk gates.
All gates are enforced before any order reaches the execution layer.
These cannot be disabled from the dashboard — only limits can be tuned.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import date
import structlog

log = structlog.get_logger()

@dataclass
class SessionState:
    date:              date  = field(default_factory=date.today)
    starting_equity:   float = 0.0
    current_equity:    float = 0.0
    consecutive_losses: int  = 0
    trades_today:      int   = 0
    halted:            bool  = False
    halt_reason:       str   = ""

    @property
    def daily_pnl_pct(self) -> float:
        if self.starting_equity <= 0:
            return 0.0
        return (self.current_equity - self.starting_equity) / self.starting_equity

class RiskGate:
    def __init__(
        self,
        daily_drawdown_halt_pct: float = 0.02,
        consecutive_loss_halt:   int   = 3,
        max_position_pct:        float = 0.05,
        max_notional:            float = float("inf"),
    ):
        self._dd_halt    = daily_drawdown_halt_pct
        self._consec_halt = consecutive_loss_halt
        self._max_pos    = max_position_pct
        self._max_notional = max_notional
        self._session    = SessionState()

    def start_session(self, equity: float):
        self._session = SessionState(
            date=date.today(),
            starting_equity=equity,
            current_equity=equity,
        )
        log.info("session started", equity=equity)

    def update_equity(self, equity: float):
        self._session.current_equity = equity

    def record_trade_result(self, pnl: float):
        self._session.trades_today += 1
        self._session.current_equity += pnl
        if pnl < 0:
            self._session.consecutive_losses += 1
        else:
            self._session.consecutive_losses = 0

        # Check halt conditions
        if self._session.daily_pnl_pct <= -self._dd_halt:
            self._halt(f"daily drawdown {self._session.daily_pnl_pct:.2%} exceeded {self._dd_halt:.2%}")
        elif self._session.consecutive_losses >= self._consec_halt:
            self._halt(f"{self._session.consecutive_losses} consecutive losses")

    def _halt(self, reason: str):
        if not self._session.halted:
            self._session.halted    = True
            self._session.halt_reason = reason
            log.warning("TRADING HALTED", reason=reason)

    def resume(self):
        """Manual resume — requires explicit dashboard action."""
        self._session.halted      = False
        self._session.halt_reason = ""
        self._session.consecutive_losses = 0
        log.info("trading resumed manually")

    def check(self, notional: float, capital: float, regime: str) -> tuple[bool, str]:
        """
        Returns (approved, reason).
        approved=False means the trade must not execute.
        """
        if self._session.halted:
            return False, f"halted: {self._session.halt_reason}"

        if regime == "volatile":
            return False, "regime=volatile: no new positions"

        if notional / (capital + 1e-9) > self._max_pos:
            return False, f"position size {notional/capital:.2%} > max {self._max_pos:.2%}"

        if notional > self._max_notional:
            return False, f"notional {notional:.2f} > limit {self._max_notional:.2f}"

        return True, "approved"

    @property
    def session(self) -> SessionState:
        return self._session

    def update_params(self, **kwargs):
        for k, v in kwargs.items():
            if hasattr(self, f"_{k}"):
                setattr(self, f"_{k}", v)
                log.info("risk param updated", param=k, value=v)

