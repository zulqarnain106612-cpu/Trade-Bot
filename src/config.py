"""
Production configuration for the algorithmic trading bot.

Authority sources:
  - Pydantic-Settings v2 docs (https://docs.pydantic.dev/latest/concepts/pydantic_settings/)
  - López de Prado (2018) AFML — risk parameter foundations
  - Kelly (1956) A New Interpretation of Information Rate — position sizing constants
"""

from __future__ import annotations

import os
from enum import Enum
from functools import lru_cache
from pathlib import Path
from typing import Final

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


# ---------------------------------------------------------------------------
# Enumerations — runtime-switchable modes
# ---------------------------------------------------------------------------


class TradingMode(str, Enum):
    """Paper vs live gate — requires TRADING_MODE=live in .env to unlock live."""

    PAPER = "paper"
    LIVE = "live"


class ExecutionMode(str, Enum):
    """
    Dashboard-switchable execution approval model.

    AUTOMATIC   — no approvals required, bot fires freely within risk gates.
    RESTRICTED  — autonomous below notional_limit_usd, approval above it;
                  auto-skip trade on approval_timeout_s.
    MANUAL      — every trade queued for explicit operator approval.
    """

    AUTOMATIC = "automatic"
    RESTRICTED = "restricted"
    MANUAL = "manual"


class Timeframe(str, Enum):
    """Three concurrent timeframe streams — all run in paper simultaneously."""

    SCALPING = "1m"
    INTRADAY = "15m"
    SWING = "4h"


# ---------------------------------------------------------------------------
# Exchange sub-settings
# ---------------------------------------------------------------------------


class BinanceSettings(BaseSettings):
    """Binance primary exchange credentials."""

    model_config = SettingsConfigDict(env_prefix="BINANCE_", env_file=".env", extra="ignore")

    api_key: str = Field(default="", description="Binance API key")
    api_secret: str = Field(default="", description="Binance API secret")
    testnet: bool = Field(default=True, description="Use Binance testnet when True")
    base_url: str = Field(
        default="https://testnet.binance.vision",
        description="REST base URL, auto-switched by testnet flag",
    )
    ws_url: str = Field(
        default="wss://testnet.binance.vision/ws",
        description="WebSocket base URL",
    )
    rate_limit_per_minute: int = Field(default=1200, ge=1)

    @model_validator(mode="after")
    def resolve_urls(self) -> "BinanceSettings":
        if not self.testnet:
            self.base_url = "https://api.binance.com"
            self.ws_url = "wss://stream.binance.com:9443/ws"
        return self


class OKXSettings(BaseSettings):
    """OKX secondary exchange credentials."""

    model_config = SettingsConfigDict(env_prefix="OKX_", env_file=".env", extra="ignore")

    api_key: str = Field(default="", description="OKX API key")
    api_secret: str = Field(default="", description="OKX API secret")
    passphrase: str = Field(default="", description="OKX API passphrase")
    testnet: bool = Field(default=True, description="Use OKX demo trading when True")
    base_url: str = Field(
        default="https://www.okx.com",
        description="REST base URL (same host, flag controls endpoint path)",
    )
    rate_limit_per_second: int = Field(default=10, ge=1)


# ---------------------------------------------------------------------------
# Risk constants — López de Prado (2018) AFML + Kelly (1956)
# ---------------------------------------------------------------------------


class RiskSettings(BaseSettings):
    """
    Hard risk limits — never weakened at runtime.

    daily_drawdown_halt_pct  : halt trading when daily PnL drops below this %
                               of starting equity (López de Prado, Ch.3 stop-loss).
    consecutive_loss_halt    : halt after N consecutive losing trades.
    max_position_size_pct    : maximum single-position size as % of capital.
    kelly_multiplier         : fractional Kelly multiplier = 0.5 (half-Kelly).
    kelly_ceiling            : hard cap on Kelly fraction regardless of estimate.
    oos_sharpe_threshold     : out-of-sample Sharpe required before live gate opens.
    max_drawdown_threshold   : max drawdown % allowed for live gate.
    min_trades_live_gate     : minimum back-tested trades required for live gate.
    """

    model_config = SettingsConfigDict(env_prefix="RISK_", env_file=".env", extra="ignore")

    daily_drawdown_halt_pct: float = Field(default=2.0, ge=0.1, le=10.0)
    consecutive_loss_halt: int = Field(default=3, ge=1, le=20)
    max_position_size_pct: float = Field(default=5.0, ge=0.1, le=25.0)

    # Kelly (1956) — half-Kelly with ceiling per AFML Ch.10
    kelly_multiplier: float = Field(default=0.5, ge=0.01, le=1.0)
    kelly_ceiling: float = Field(default=0.25, ge=0.01, le=1.0)

    # Live gate thresholds per timeframe (AFML Ch.7 CPCV OOS validation)
    oos_sharpe_threshold: float = Field(default=1.5, ge=0.0)
    max_drawdown_threshold: float = Field(default=15.0, ge=1.0, le=100.0)
    min_trades_live_gate: int = Field(default=500, ge=1)

    # Restricted mode approval window
    notional_limit_usd: float = Field(
        default=100.0,
        ge=0.0,
        description="Auto-approve trades at or below this notional in RESTRICTED mode",
    )
    approval_timeout_s: float = Field(
        default=30.0,
        ge=1.0,
        description="Seconds to wait for manual approval before auto-skip in RESTRICTED mode",
    )


# ---------------------------------------------------------------------------
# Model hyper-parameters
# ---------------------------------------------------------------------------


class HMMSettings(BaseSettings):
    """
    GaussianHMM regime detector — Hamilton (1989) 3-state switching model.
    3 states: ranging (0), trending (1), volatile (2).
    """

    model_config = SettingsConfigDict(env_prefix="HMM_", env_file=".env", extra="ignore")

    n_components: int = Field(default=3, ge=2, le=10)
    covariance_type: str = Field(default="full")
    n_iter: int = Field(default=200, ge=10)
    tol: float = Field(default=1e-4, ge=1e-10)
    random_state: int = Field(default=42, ge=0)
    # State index mapping — volatile state triggers regime gate
    volatile_state_index: int = Field(default=2, ge=0)

    @field_validator("covariance_type")
    @classmethod
    def validate_cov_type(cls, v: str) -> str:
        valid = {"spherical", "tied", "diag", "full"}
        if v not in valid:
            raise ValueError(f"covariance_type must be one of {valid}, got {v!r}")
        return v


class XGBoostSettings(BaseSettings):
    """
    XGBoost classifier settings — shared by primary direction model
    and meta-label gate model (each instantiated separately).
    """

    model_config = SettingsConfigDict(env_prefix="XGB_", env_file=".env", extra="ignore")

    n_estimators: int = Field(default=500, ge=10)
    max_depth: int = Field(default=6, ge=1, le=20)
    learning_rate: float = Field(default=0.05, ge=1e-5, le=1.0)
    subsample: float = Field(default=0.8, ge=0.1, le=1.0)
    colsample_bytree: float = Field(default=0.8, ge=0.1, le=1.0)
    min_child_weight: int = Field(default=5, ge=1)
    reg_alpha: float = Field(default=0.1, ge=0.0)
    reg_lambda: float = Field(default=1.0, ge=0.0)
    use_label_encoder: bool = Field(default=False)
    eval_metric: str = Field(default="logloss")
    tree_method: str = Field(default="hist")
    device: str = Field(default="cpu")
    random_state: int = Field(default=42, ge=0)
    early_stopping_rounds: int = Field(default=50, ge=5)


# ---------------------------------------------------------------------------
# Feature engineering constants — AFML Ch.3–5
# ---------------------------------------------------------------------------


class FeatureSettings(BaseSettings):
    """
    Feature pipeline configuration.

    frac_diff_d          : fractional differentiation order (AFML Ch.5, d=0.4
                           balances stationarity and memory retention).
    vwap_window          : rolling window for VWAP deviation z-score.
    ofi_window           : order-flow imbalance rolling window in bars.
    realized_vol_window  : short window for realized volatility ratio.
    atr_window           : ATR momentum window.
    sharpe_window        : rolling Sharpe denominator window.
    volume_zscore_window : volume z-score rolling window.
    triple_barrier_pt    : profit-taking barrier as multiple of daily vol.
    triple_barrier_sl    : stop-loss barrier as multiple of daily vol.
    triple_barrier_t     : maximum holding period in bars (time exit).
    """

    model_config = SettingsConfigDict(env_prefix="FEATURE_", env_file=".env", extra="ignore")

    frac_diff_d: float = Field(default=0.4, ge=0.0, le=1.0)
    frac_diff_threshold: float = Field(default=1e-5, ge=1e-10)

    vwap_window: int = Field(default=20, ge=2)
    ofi_window: int = Field(default=20, ge=2)
    realized_vol_window_short: int = Field(default=10, ge=2)
    realized_vol_window_long: int = Field(default=60, ge=2)
    atr_window: int = Field(default=14, ge=2)
    sharpe_window: int = Field(default=60, ge=2)
    volume_zscore_window: int = Field(default=20, ge=2)

    # Triple-barrier labeling — AFML Ch.3
    triple_barrier_pt_multiplier: float = Field(default=2.0, ge=0.1)
    triple_barrier_sl_multiplier: float = Field(default=1.0, ge=0.1)
    triple_barrier_max_holding_bars: int = Field(default=60, ge=1)

    # CPCV — AFML Ch.7
    cpcv_n_splits: int = Field(default=10, ge=4)
    cpcv_n_test_splits: int = Field(default=2, ge=1)
    purge_gap_bars: int = Field(default=5, ge=0)
    embargo_pct: float = Field(default=0.01, ge=0.0, le=0.5)


# ---------------------------------------------------------------------------
# Storage settings
# ---------------------------------------------------------------------------


class StorageSettings(BaseSettings):
    """SQLite + file path configuration."""

    model_config = SettingsConfigDict(env_prefix="STORAGE_", env_file=".env", extra="ignore")

    db_path: Path = Field(default=Path("data/trade_bot.db"))
    model_dir: Path = Field(default=Path("models/artifacts"))
    log_dir: Path = Field(default=Path("logs"))
    bar_cache_days: int = Field(default=90, ge=1)

    @model_validator(mode="after")
    def create_directories(self) -> "StorageSettings":
        for p in (self.db_path.parent, self.model_dir, self.log_dir):
            p.mkdir(parents=True, exist_ok=True)
        return self


# ---------------------------------------------------------------------------
# API / server settings
# ---------------------------------------------------------------------------


class APISettings(BaseSettings):
    """FastAPI server configuration."""

    model_config = SettingsConfigDict(env_prefix="API_", env_file=".env", extra="ignore")

    host: str = Field(default="0.0.0.0")
    port: int = Field(default=8000, ge=1024, le=65535)
    reload: bool = Field(default=False)
    cors_origins: list[str] = Field(default=["http://localhost:5173"])
    ws_heartbeat_s: float = Field(default=5.0, ge=1.0)


# ---------------------------------------------------------------------------
# Root settings — composes all sub-settings
# ---------------------------------------------------------------------------


class Settings(BaseSettings):
    """
    Root settings object.  Single source of truth for the entire application.
    Loaded once via get_settings() and cached for the process lifetime.

    Environment variables:
      TRADING_MODE   : "paper" (default) | "live"
      EXECUTION_MODE : "automatic" | "restricted" | "manual"
      PRIMARY_SYMBOL : e.g. "BTC/USDT"
      STARTING_CAPITAL_USD : initial paper capital
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # Core runtime flags
    trading_mode: TradingMode = Field(default=TradingMode.PAPER)
    execution_mode: ExecutionMode = Field(default=ExecutionMode.MANUAL)

    # Trading universe
    primary_symbol: str = Field(default="BTC/USDT")
    active_timeframes: list[Timeframe] = Field(
        default=[Timeframe.SCALPING, Timeframe.INTRADAY, Timeframe.SWING]
    )
    primary_timeframe: Timeframe = Field(
        default=Timeframe.INTRADAY,
        description="Real-money primary timeframe (15m)",
    )

    # Capital
    starting_capital_usd: float = Field(default=1000.0, ge=1.0)
    paper_trading_days_minimum: int = Field(
        default=30,
        ge=1,
        description="Minimum paper trading days before live is permitted",
    )

    # Sub-settings (composed — not env-parsed directly here)
    binance: BinanceSettings = Field(default_factory=BinanceSettings)
    okx: OKXSettings = Field(default_factory=OKXSettings)
    risk: RiskSettings = Field(default_factory=RiskSettings)
    hmm: HMMSettings = Field(default_factory=HMMSettings)
    xgboost: XGBoostSettings = Field(default_factory=XGBoostSettings)
    features: FeatureSettings = Field(default_factory=FeatureSettings)
    storage: StorageSettings = Field(default_factory=StorageSettings)
    api: APISettings = Field(default_factory=APISettings)

    # Logging
    log_level: str = Field(default="INFO")
    log_as_json: bool = Field(default=False)

    @field_validator("log_level")
    @classmethod
    def validate_log_level(cls, v: str) -> str:
        valid = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        upper = v.upper()
        if upper not in valid:
            raise ValueError(f"log_level must be one of {valid}, got {v!r}")
        return upper

    @model_validator(mode="after")
    def enforce_live_gate(self) -> "Settings":
        """
        Prevent live trading from being enabled purely via code path.
        Requires the environment variable TRADING_MODE=live to be explicitly set.
        """
        env_mode = os.environ.get("TRADING_MODE", "paper").lower()
        if self.trading_mode == TradingMode.LIVE and env_mode != "live":
            raise ValueError(
                "Live trading requires TRADING_MODE=live in environment — "
                "cannot be activated programmatically."
            )
        return self

    @model_validator(mode="after")
    def validate_primary_timeframe_in_active(self) -> "Settings":
        if self.primary_timeframe not in self.active_timeframes:
            raise ValueError(
                f"primary_timeframe {self.primary_timeframe!r} must be in active_timeframes"
            )
        return self


# ---------------------------------------------------------------------------
# Module-level constants — derived from spec, not env
# ---------------------------------------------------------------------------

# Regime state indices (Hamilton 1989 HMM — 3 states)
REGIME_RANGING: Final[int] = 0
REGIME_TRENDING: Final[int] = 1
REGIME_VOLATILE: Final[int] = 2

# Direction label values used throughout models
LABEL_SHORT: Final[int] = 0
LABEL_LONG: Final[int] = 1
LABEL_NO_TRADE: Final[int] = -1  # triple-barrier time-exit or meta-label gate=0

# Timeframe to seconds mapping
TIMEFRAME_SECONDS: Final[dict[Timeframe, int]] = {
    Timeframe.SCALPING: 60,
    Timeframe.INTRADAY: 900,
    Timeframe.SWING: 14400,
}

# Exchange IDs used by ccxt
EXCHANGE_BINANCE: Final[str] = "binance"
EXCHANGE_OKX: Final[str] = "okx"


# ---------------------------------------------------------------------------
# Cached accessor — single instance for the process
# ---------------------------------------------------------------------------


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """
    Return the singleton Settings instance.

    lru_cache(maxsize=1) ensures pydantic-settings reads .env exactly once,
    preventing repeated I/O and guaranteeing a single source of truth.
    Call invalidate_settings_cache() in tests to reset between cases.
    """
    return Settings()


def invalidate_settings_cache() -> None:
    """Clear cached settings — intended for test isolation only."""
    get_settings.cache_clear()
