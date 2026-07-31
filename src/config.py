"""
Production configuration for the algorithmic trading bot.

Authority sources:
  - Pydantic-Settings v2 docs (https://docs.pydantic.dev/latest/concepts/pydantic_settings/)
  - López de Prado (2018) AFML — risk parameter foundations
  - Kelly (1956) A New Interpretation of Information Rate — position sizing constants
"""

from __future__ import annotations

import asyncio
import os
import threading
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Final, Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


# ---------------------------------------------------------------------------
# Enumerations — runtime-switchable modes
# ---------------------------------------------------------------------------


class TradingMode(StrEnum):
    """Paper vs live gate — requires TRADING_MODE=live in .env to unlock live."""

    PAPER = "paper"
    LIVE = "live"


class ExecutionMode(StrEnum):
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


class Timeframe(StrEnum):
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
    def resolve_urls(self) -> BinanceSettings:
        # VF-003: Only inject production URLs when the operator has NOT explicitly
        # set BINANCE_BASE_URL / BINANCE_WS_URL in the environment.  Previously the
        # validator silently overwrote any env-supplied URL when testnet=False, making
        # operator overrides (e.g. custom proxy URLs) impossible.
        if not self.testnet:
            if "BINANCE_BASE_URL" not in os.environ:
                self.base_url = "https://api.binance.com"
            if "BINANCE_WS_URL" not in os.environ:
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

    # v10 capital preservation floor (src/risk/capital_preservation_floor.py):
    # peak-equity drawdown that halts trading permanently until an explicit,
    # out-of-band re_authorize() call -- unlike daily_drawdown_halt_pct above,
    # this never auto-clears at UTC midnight or on equity recovery.
    capital_preservation_max_drawdown_pct: float = Field(default=0.30, gt=0.0, lt=1.0)

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

    # GAP-001 — Almgren-Chriss slippage / market-impact model
    # (Almgren & Chriss 2001, "Optimal Execution of Portfolio Transactions").
    # slippage_default_spread_bps : fallback half-spread assumption when the
    #                                live order book is unavailable.
    # slippage_impact_coeff_bps   : impact coefficient applied to
    #                                sqrt(qty / adv_20d); this is a live,
    #                                actively-used constant (src/risk/slippage.py
    #                                -> gates.py's cost veto), not dormant --
    #                                src/tuning/bootstrap.py registers it and
    #                                src/tuning/backtest_harness.py's
    #                                run_slippage_coeff_backtest recalibrates it
    #                                from realized fill cost; the scheduler
    #                                (src/tuning/scheduler.py) runs this
    #                                recalibration automatically when
    #                                SELF_TUNING_ENABLED=true, still gated by
    #                                shadow_mode like every other parameter.
    # slippage_veto_margin_bps    : extra safety margin required on top of
    #                                spread+impact before a signal is allowed
    #                                through gate 0 (never let est. cost == edge).
    slippage_default_spread_bps: float = Field(default=2.0, ge=0.0, le=500.0)
    slippage_impact_coeff_bps: float = Field(default=10.0, ge=0.0, le=2000.0)
    slippage_veto_margin_bps: float = Field(default=1.0, ge=0.0, le=500.0)

    # ensemble_blend_weight -- how much of the XGBoost direction model's
    # p_long gets replaced by the diversified prediction ensemble's own
    # implied probability (src/intelligence/ensemble_predictor.py, wired in
    # via src/engine/signal_engine.py). 0.0 = ensemble has no effect
    # (XGBoost-only, today's behavior); 1.0 = ensemble fully replaces
    # XGBoost. Deliberately conservative by default -- the ensemble is a
    # newly-wired live component and this weight is registered as a
    # self-tuning parameter (src/tuning/bootstrap.py) so it can only move
    # via the same propose/evaluate/gate/shadow-mode machinery every other
    # tunable parameter goes through, never a direct edit to this default.
    ensemble_blend_weight: float = Field(default=0.15, ge=0.0, le=1.0)

    # GARCH vol normalization threshold: the per-bar GARCH vol level at which
    # the garch_component in _compute_risk_score saturates to 1.0.
    # 0.02 = 2% per bar (~31.6% annualized at daily bars) — representative
    # of a 1-sigma stress day in major crypto. Lower values make the engine
    # more sensitive to elevated GARCH vol; higher values require larger
    # shocks to affect the risk score.
    garch_vol_threshold: float = Field(default=0.02, gt=0.0, le=0.50)

    # GAP-013 -- automated position-exit controls (stop-loss / take-profit /
    # time-based exit). These are STARTUP DEFAULTS ONLY, loaded once into
    # RuntimeConfig at process start. Unlike the hard limits above, the
    # enabled/disabled toggle and the threshold VALUES are intentionally
    # runtime-mutable via RuntimeConfig + POST /risk-controls -- turning an
    # exit control on/off changes risk exposure in the safer direction when
    # enabling, and a human operator may legitimately need to adjust
    # thresholds without a redeploy. The fields here only seed the initial
    # state; see RuntimeConfig below for the live, toggleable values.
    stop_loss_enabled_default: bool = Field(default=True)
    stop_loss_pct_default: float = Field(
        default=2.0,
        ge=0.1,
        le=50.0,
        description="Close position when unrealized loss reaches this pct of notional",
    )
    take_profit_enabled_default: bool = Field(default=True)
    take_profit_pct_default: float = Field(
        default=4.0,
        ge=0.1,
        le=200.0,
        description="Close position when unrealized gain reaches this pct of notional",
    )
    max_holding_period_s_default: float = Field(
        default=86400.0,
        ge=60.0,
        description="Force time-based exit after this many seconds in position",
    )
    trailing_stop_enabled_default: bool = Field(default=False)
    trailing_stop_pct_default: float = Field(
        default=1.5,
        ge=0.1,
        le=50.0,
        description=(
            "Close when unrealized PnL drops more than this pct from its per-position peak. "
            "Only active when trailing_stop_enabled is True."
        ),
    )
    position_monitor_interval_s: float = Field(
        default=5.0,
        ge=1.0,
        le=300.0,
        description="How often the orchestrator exit-check loop re-evaluates open positions",
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

    # GAP-002: posterior entropy gate — quantifies regime confidence.
    # entropy_threshold     : normalized entropy (0-1) above which position
    #                         size starts scaling down. Below this, full size.
    # entropy_scalar_floor  : minimum position scalar at entropy=1.0 (max
    #                         uncertainty / near-uniform posterior). Matches
    #                         OPEN_TASKS.md TASK-002 spec floor of 0.5.
    entropy_threshold: float = Field(default=0.5, ge=0.0, le=1.0)
    entropy_scalar_floor: float = Field(default=0.5, ge=0.0, le=1.0)

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
# Feature engineering constants — AFML Ch.3-5
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
    # GARCH(1,1) conditional volatility window — Bollerslev (1986).
    # Walk-forward refit window; must be >= 50 (minimum MLE observations).
    # 100 bars stays within the 200-bar frac-diff burn-in so it adds no
    # new min-required-rows constraint to build_feature_matrix().
    garch_window: int = Field(default=100, ge=50)

    # Triple-barrier labeling — AFML Ch.3
    triple_barrier_pt_multiplier: float = Field(default=2.0, ge=0.1)
    triple_barrier_sl_multiplier: float = Field(default=1.0, ge=0.1)
    triple_barrier_max_holding_bars: int = Field(default=60, ge=1)

    # CPCV — AFML Ch.7
    cpcv_n_splits: int = Field(default=10, ge=4)
    cpcv_n_test_splits: int = Field(default=2, ge=1)
    # UI-005: must be >= triple_barrier_max_holding_bars (validated below) —
    # AFML Ch.7 requires the purge window to span the full label horizon,
    # since a training sample's triple-barrier label is computed from up to
    # that many future bars. A purge gap shorter than the label horizon
    # lets training labels overlap with (leak from) the test fold.
    purge_gap_bars: int = Field(default=60, ge=0)
    embargo_pct: float = Field(default=0.01, ge=0.0, le=0.5)

    # UI-015: multi-timeframe trend confirmation (Schwager 1993) — see
    # src.strategies.filters.mtf_trend_aligned. Off by default: enabling
    # it changes which signals pass the strategy-filter stack (an
    # additional veto), which is exactly the kind of live-signal-path
    # behavior change that should be an explicit operator opt-in, not a
    # default-on change bundled into a bug-fix/enhancement pass.
    mtf_confirmation_enabled: bool = Field(
        default=False,
        description=(
            "Require the next-higher timeframe's EWM trend to agree with "
            "the proposed direction (Schwager 1993) before a signal passes "
            "the strategy filter stack. Off by default -- an operator opt-in."
        ),
    )

    @model_validator(mode="after")
    def validate_purge_gap_covers_label_horizon(self) -> FeatureSettings:
        if self.purge_gap_bars < self.triple_barrier_max_holding_bars:
            raise ValueError(
                f"purge_gap_bars ({self.purge_gap_bars}) must be >= "
                f"triple_barrier_max_holding_bars ({self.triple_barrier_max_holding_bars}) — "
                "AFML Ch.7: the purge window must span the full label horizon or "
                "training labels can leak information from the test fold."
            )
        return self


# ---------------------------------------------------------------------------
# Storage settings
# ---------------------------------------------------------------------------


class StorageSettings(BaseSettings):
    """Storage backend selection + file path configuration."""

    model_config = SettingsConfigDict(env_prefix="STORAGE_", env_file=".env", extra="ignore")

    # GAP-006: "sqlite" (embedded, default for tests/dev) or "timescale"
    # (local TimescaleDB container — see scripts/timescaledb.sh).
    backend: str = Field(default="sqlite", pattern="^(sqlite|timescale)$")
    timescale_dsn: str = Field(
        default="postgresql://tradebot:tradebot-local@127.0.0.1:5433/tradebot",  # pragma: allowlist secret
        description="asyncpg DSN for the local TimescaleDB container (localhost-only)",
    )
    db_path: Path = Field(default=Path("data/trade_bot.db"))
    model_dir: Path = Field(default=Path("models/artifacts"))
    log_dir: Path = Field(default=Path("logs"))
    bar_cache_days: int = Field(default=90, ge=1)

    # Directory creation intentionally removed from this validator (VUL-031).
    # Having a Pydantic validator create filesystem directories is a side-effect
    # that runs on every Settings instantiation, including during tests, which
    # pollutes the host filesystem with unexpected directories.
    # Directories are now created once at application startup by
    # StorageBackend.initialize() or the explicit setup_storage_directories() helper.


# ---------------------------------------------------------------------------
# API / server settings
# ---------------------------------------------------------------------------


class APISettings(BaseSettings):
    """FastAPI server configuration."""

    model_config = SettingsConfigDict(env_prefix="API_", env_file=".env", extra="ignore")

    host: str = Field(
        default="127.0.0.1",
        description=(
            "Bind address. Default 127.0.0.1 (loopback only). "
            "For external access use a TLS-terminating reverse proxy — "
            "never expose directly with 0.0.0.0 in production."
        ),
    )
    port: int = Field(default=8000, ge=1024, le=65535)
    reload: bool = Field(default=False)
    cors_origins: list[str] = Field(default=["http://localhost:5173"])
    ws_heartbeat_s: float = Field(default=5.0, ge=1.0)

    @field_validator("cors_origins")
    @classmethod
    def validate_cors_origins(cls, v: list[str]) -> list[str]:
        # VF-005: Reject wildcard "*" — allows any origin, defeating CORS protection.
        for origin in v:
            if origin.strip() == "*":
                raise ValueError(
                    "CORS wildcard '*' is forbidden — specify explicit allowed origins."
                )
        return v


# ---------------------------------------------------------------------------
# Root settings — composes all sub-settings
# ---------------------------------------------------------------------------


class IntelligenceSettings(BaseSettings):
    """
    On-chain intelligence provider credentials and tuning.

    GAP-015: These settings gate the real provider implementations in
    src/intelligence/client.py.  With empty keys the client logs a
    warning and uses safe fallback values so the rest of the pipeline
    keeps running (fail-open at the intelligence layer, fail-closed at
    the main risk gates — the opposite of a safety-critical gate).

    Required env vars (set in .env, never committed):
      INTELLIGENCE_GLASSNODE_API_KEY   — Professional tier required for
                                         exchange-flow and large-tx metrics
      INTELLIGENCE_CRYPTOQUANT_API_KEY — Optional; funding rate falls back
                                         to Binance perpetual futures via
                                         ccxt (free, already a dependency)
    """

    model_config = SettingsConfigDict(env_prefix="INTELLIGENCE_", env_file=".env", extra="ignore")

    glassnode_api_key: str = Field(default="", description="Glassnode API key")
    cryptoquant_api_key: str = Field(default="", description="CryptoQuant API key (optional)")
    # OCI-001: free-tier on-chain providers (no paid plan required)
    arkham_api_key: str = Field(default="", description="Arkham Intel API key (free)")
    dune_api_key: str = Field(default="", description="Dune Analytics API key (free tier)")
    coinglass_api_key: str = Field(default="", description="Coinglass API key (free tier)")
    arkham_cache_ttl_s: int = Field(default=60, ge=10, description="Arkham cache TTL seconds")
    defillama_cache_ttl_s: int = Field(
        default=300, ge=30, description="DeFiLlama cache TTL seconds"
    )
    dune_cache_ttl_s: int = Field(default=3600, ge=60, description="Dune cache TTL seconds")
    coinglass_cache_ttl_s: int = Field(default=30, ge=10, description="Coinglass cache TTL seconds")
    glassnode_base_url: str = Field(
        default="https://api.glassnode.com/v1/metrics",
        description="Glassnode REST base URL",
    )
    cache_ttl_onchain_seconds: int = Field(
        default=3600, ge=60, description="Cache TTL for on-chain metrics (min 60s)"
    )
    cache_ttl_exchange_seconds: int = Field(
        default=300, ge=30, description="Cache TTL for exchange-flow metrics (min 30s)"
    )
    glassnode_rate_limit_seconds: float = Field(
        default=1.0, ge=0.1, description="Min seconds between Glassnode API calls"
    )
    # Binance perpetual symbol for funding-rate fetch via ccxt (no key needed)
    funding_rate_perp_symbol: str = Field(
        default="BTC/USDT:USDT",
        description="ccxt perpetual futures symbol for funding rate (e.g. BTC/USDT:USDT)",
    )


class SelfTuningSettings(BaseSettings):
    """
    Self-tuning subsystem kill switch and cadence limits.

    See docs/SELF_TUNING_DESIGN.md. `enabled` is a live master switch:
    setting it True starts src.tuning.scheduler.AutoTuningScheduler from
    the API lifespan (src/api/main.py), which registers
    hmm.entropy_threshold / hmm.entropy_scalar_floor
    (src/tuning/bootstrap.py) and runs real propose/evaluate/gate cycles
    against them on a wall-clock interval. `shadow_mode` (below, default
    True) is what keeps this side-effect-free by default -- an accepted
    challenger is logged as WOULD_PROMOTE but never written to
    VersionedConfigStore until an operator explicitly sets
    SELF_TUNING_SHADOW_MODE=false.
    """

    model_config = SettingsConfigDict(env_prefix="SELF_TUNING_", env_file=".env", extra="ignore")

    enabled: bool = Field(
        default=False,
        description="Master kill switch. Must be explicitly enabled, same pattern as TRADING_MODE=live.",
    )
    min_trades_between_attempts: int = Field(
        default=200,
        ge=1,
        description="Minimum closed trades between tuning attempts for the same parameter.",
    )
    min_hours_between_attempts: float = Field(
        default=24.0,
        ge=1.0,
        description="Minimum wall-clock hours between tuning attempts for the same parameter.",
    )
    probation_trades: int = Field(
        default=50,
        ge=1,
        description="Closed trades to watch after promotion before the watchdog clears probation.",
    )
    probation_hours: float = Field(
        default=72.0,
        ge=1.0,
        description="Wall-clock hours to watch after promotion before the watchdog clears probation.",
    )
    audit_log_path: Path = Field(default=Path("logs/self_tuning_audit.jsonl"))
    version_store_path: Path = Field(default=Path("logs/self_tuning_versions.jsonl"))
    proposer_strategy: Literal["random_walk", "bayesian"] = Field(
        default="random_walk",
        description=(
            "Which src/tuning/proposer.py strategy TuningRunner uses to pick "
            "challenger values. 'random_walk' (default) is the original "
            "memoryless bounded-step proposer. 'bayesian' uses "
            "src/tuning/bayesian_proposer.py (Optuna TPE) to condition each "
            "proposal on this parameter's own audit-log evaluation history. "
            "Purely a search-strategy choice -- gate/evaluator/watchdog "
            "safety machinery is identical either way."
        ),
    )
    proposer_step_pct: float = Field(
        default=0.1,
        gt=0.0,
        le=1.0,
        description="random_walk only: max step size as a fraction of [floor, ceiling].",
    )
    shadow_mode: bool = Field(
        default=True,
        description=(
            "Phase 7: when True (default), an accepted challenger is logged as "
            "WOULD_PROMOTE but never written to VersionedConfigStore. Flipping to "
            "False requires an explicit .env edit + restart -- the same ceremony "
            "as TRADING_MODE=live -- and must only be done after the Phase 4 "
            "shadow soak and Phase 5 watchdog are in place per "
            "docs/SELF_TUNING_IMPLEMENTATION_PLAN.md."
        ),
    )


class OrderThrottleSettings(BaseSettings):
    """
    Outgoing-order rate limiting (src/execution/order_throttler.py).

    Exchanges enforce request-weight budgets (Binance: 1200 weight/min ≈ 20
    req/s) and answer bursts with HTTP 429 followed by a temporary IP ban.
    A ban mid-position is worse than a few hundred milliseconds of delay, so
    the live executor waits for a token when the wait is short and refuses
    the order outright when the backlog is long enough that the fill would be
    stale anyway.
    """

    model_config = SettingsConfigDict(env_prefix="ORDER_THROTTLE_", env_file=".env", extra="ignore")

    enabled: bool = Field(
        default=True,
        description="Apply the token-bucket limiter to live order placement.",
    )
    rate: float = Field(
        default=8.0,
        gt=0.0,
        description="Sustained order rate (orders/second) per exchange. Below Binance's ~20/s ceiling.",
    )
    burst: int = Field(
        default=16,
        ge=1,
        description="Token-bucket capacity — how many orders may be placed back-to-back.",
    )
    max_wait_s: float = Field(
        default=2.0,
        ge=0.0,
        description=(
            "Longest the executor will wait for a token before rejecting the order. "
            "Beyond this the intended entry price is stale, so failing fast beats "
            "filling on a price the signal never saw."
        ),
    )


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
    intelligence: IntelligenceSettings = Field(default_factory=IntelligenceSettings)
    self_tuning: SelfTuningSettings = Field(default_factory=SelfTuningSettings)
    order_throttle: OrderThrottleSettings = Field(default_factory=OrderThrottleSettings)

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
    def enforce_live_gate(self) -> Settings:
        """
        Prevent live trading from being enabled purely via code path.
        Requires the environment variable TRADING_MODE=live to be explicitly set.

        VF-006: Previously compared os.environ value without .strip().lower(), so
        "LIVE", " live", or "Live" would bypass the gate.  Now normalised.
        """
        env_mode = os.environ.get("TRADING_MODE", "paper").strip().lower()
        if self.trading_mode == TradingMode.LIVE and env_mode != "live":
            raise ValueError(
                "Live trading requires TRADING_MODE=live in environment — "
                "cannot be activated programmatically."
            )
        return self

    @model_validator(mode="after")
    def validate_primary_timeframe_in_active(self) -> Settings:
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


# ---------------------------------------------------------------------------
# RuntimeConfig — mutable runtime flags, separate from cached Settings
#
# Problem (VUL-028): invalidate_settings_cache() was called in the production
# /execution-mode endpoint, which re-instantiates ALL settings (including
# exchange keys, risk thresholds) on every concurrent request during the
# cache-clear window, creating a torn-state race.
#
# Solution: mutable runtime state lives here, protected by a threading.Lock.
# get_settings() stays cached and immutable. Code that needs the current
# execution_mode reads runtime_config.execution_mode instead.
# ---------------------------------------------------------------------------

# SCAN2-015: replaced threading.Lock with asyncio.Lock.
# All callers are async FastAPI handlers in the same event loop thread;
# threading.Lock.acquire() blocks the thread (and the entire event loop)
# on contention. asyncio.Lock suspends only the current coroutine.
#
# Because Python properties cannot be async, the interface is explicit
# async getter/setter methods. Call sites updated accordingly.


class RuntimeConfig:
    """
    Process-wide mutable runtime flags.

    All reads and writes are serialised by an asyncio.Lock so concurrent
    coroutines (WS heartbeat + REST status + mode-change endpoint) never
    observe partially-updated state, and the event loop is never blocked.
    """

    def __init__(self) -> None:
        # VF-004: Lazy asyncio.Lock creation was NOT thread-safe — two coroutines
        # could both see self._lock is None simultaneously and create two separate
        # locks, silently breaking mutual exclusion.
        # Fix: guard the one-time creation with a threading.Lock (cheap; acquired
        # only once per process lifetime).  After creation, only the asyncio.Lock
        # is used, so the event loop is never blocked in steady state.
        self._init_guard: threading.Lock = threading.Lock()
        self._lock: asyncio.Lock | None = None
        cfg = get_settings()
        self._execution_mode: ExecutionMode = cfg.execution_mode

        # GAP-013 -- runtime-toggleable position-exit controls. Seeded from
        # RiskSettings defaults at startup; mutable thereafter via
        # set_risk_controls() / POST /risk-controls (operator-secret gated,
        # same pattern as set_execution_mode).
        self._stop_loss_enabled: bool = cfg.risk.stop_loss_enabled_default
        self._stop_loss_pct: float = cfg.risk.stop_loss_pct_default
        self._take_profit_enabled: bool = cfg.risk.take_profit_enabled_default
        self._take_profit_pct: float = cfg.risk.take_profit_pct_default
        self._max_holding_period_s: float = cfg.risk.max_holding_period_s_default
        self._trailing_stop_enabled: bool = cfg.risk.trailing_stop_enabled_default
        self._trailing_stop_pct: float = cfg.risk.trailing_stop_pct_default

    def _get_lock(self) -> asyncio.Lock:
        # Fast path — already initialised (no locking needed; reads are atomic in CPython).
        if self._lock is not None:
            return self._lock
        # Slow path — first call; guard with threading.Lock to prevent double-init race.
        with self._init_guard:
            if self._lock is None:
                self._lock = asyncio.Lock()
        assert self._lock is not None
        return self._lock

    async def get_execution_mode(self) -> ExecutionMode:
        async with self._get_lock():
            return self._execution_mode

    async def set_execution_mode(self, value: ExecutionMode) -> None:
        async with self._get_lock():
            self._execution_mode = value

    # ------------------------------------------------------------------
    # GAP-013 -- position-exit controls (stop-loss / take-profit / time exit)
    # ------------------------------------------------------------------

    async def get_risk_controls(self) -> dict[str, object]:
        """Snapshot of all exit-control toggles/values, for API reads and the orchestrator's exit-check loop."""
        async with self._get_lock():
            return {
                "stop_loss_enabled": self._stop_loss_enabled,
                "stop_loss_pct": self._stop_loss_pct,
                "take_profit_enabled": self._take_profit_enabled,
                "take_profit_pct": self._take_profit_pct,
                "max_holding_period_s": self._max_holding_period_s,
                "trailing_stop_enabled": self._trailing_stop_enabled,
                "trailing_stop_pct": self._trailing_stop_pct,
            }

    async def set_risk_controls(
        self,
        stop_loss_enabled: bool | None = None,
        stop_loss_pct: float | None = None,
        take_profit_enabled: bool | None = None,
        take_profit_pct: float | None = None,
        max_holding_period_s: float | None = None,
        trailing_stop_enabled: bool | None = None,
        trailing_stop_pct: float | None = None,
    ) -> dict[str, object]:
        """
        Update one or more exit-control fields atomically. Pass None for any
        field to leave it unchanged. Validation (range checks) is the
        caller's responsibility (API layer validates via Pydantic before
        calling this) -- this method only guards concurrent read/write.
        """
        async with self._get_lock():
            if stop_loss_enabled is not None:
                self._stop_loss_enabled = stop_loss_enabled
            if stop_loss_pct is not None:
                self._stop_loss_pct = stop_loss_pct
            if take_profit_enabled is not None:
                self._take_profit_enabled = take_profit_enabled
            if take_profit_pct is not None:
                self._take_profit_pct = take_profit_pct
            if max_holding_period_s is not None:
                self._max_holding_period_s = max_holding_period_s
            if trailing_stop_enabled is not None:
                self._trailing_stop_enabled = trailing_stop_enabled
            if trailing_stop_pct is not None:
                self._trailing_stop_pct = trailing_stop_pct
            return {
                "stop_loss_enabled": self._stop_loss_enabled,
                "stop_loss_pct": self._stop_loss_pct,
                "take_profit_enabled": self._take_profit_enabled,
                "take_profit_pct": self._take_profit_pct,
                "max_holding_period_s": self._max_holding_period_s,
                "trailing_stop_enabled": self._trailing_stop_enabled,
                "trailing_stop_pct": self._trailing_stop_pct,
            }

    # ------------------------------------------------------------------
    # Synchronous read — safe only when called from a single-writer context
    # (e.g. reading in a sync property before the event loop starts).
    # Do NOT call from async handlers — use get_execution_mode() instead.
    # ------------------------------------------------------------------
    @property
    def execution_mode(self) -> ExecutionMode:
        """Sync read — use only from sync (non-async) call sites."""
        return self._execution_mode


# Singleton — imported by api/main.py and execution/ modules
runtime_config: RuntimeConfig = RuntimeConfig()
