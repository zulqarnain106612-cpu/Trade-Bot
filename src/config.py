"""
Central configuration — all runtime-tunable parameters live here.
Loaded from .env and overridable via dashboard API at runtime.
"""
from __future__ import annotations
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field
from typing import Literal
import threading

class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Exchange
    binance_api_key:    str = ""
    binance_api_secret: str = ""
    okx_api_key:        str = ""
    okx_api_secret:     str = ""
    okx_passphrase:     str = ""

    # Trading mode
    trading_mode: Literal["paper", "live"] = "paper"

    # Active exchanges
    active_exchanges: list[str] = Field(default=["binance"])

    # Active timeframes
    active_timeframes: list[str] = Field(default=["intraday"])

    # Execution mode
    execution_mode: Literal["automatic", "restricted", "manual"] = "restricted"

    # Restricted mode notional limit (USD) — trades above this need approval
    restricted_notional_limit: float = 50.0

    # Approval timeout seconds — auto-skip after this
    approval_timeout_secs: int = 60

    # Risk
    max_capital_per_trade_pct: float = 0.01   # 1% default — Kelly will size down further
    daily_drawdown_halt_pct:   float = 0.02   # halt at 2% daily loss
    consecutive_loss_halt:     int   = 3       # halt after 3 consecutive losses
    max_position_pct:          float = 0.05   # hard cap 5% capital per position

    # Symbols to trade
    symbols: list[str] = Field(default=["BTC/USDT", "ETH/USDT"])

    # Model paths
    model_dir: str = "./models"
    db_path:   str = "./data/tradebot.db"
    wal_path:  str = "./data/journal.wal"

    # Dashboard
    dashboard_port: int = 8000
    frontend_port:  int = 5173
    log_level:      str = "INFO"

    # Paper trading initial capital (USD)
    paper_capital: float = 1000.0

_settings_lock = threading.Lock()
_settings: Settings | None = None

def get_settings() -> Settings:
    global _settings
    if _settings is None:
        with _settings_lock:
            if _settings is None:
                _settings = Settings()
    return _settings

def update_settings(**kwargs) -> Settings:
    """Update settings at runtime — called by dashboard API."""
    global _settings
    with _settings_lock:
        current = get_settings().model_dump()
        current.update(kwargs)
        _settings = Settings.model_validate(current)
    return _settings

