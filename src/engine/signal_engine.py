"""
Signal engine — per-timeframe signal computation pipeline.

On every tick for a given (symbol, timeframe):
  1. Gap-fill bars from exchange into storage
  2. Load recent bars, build feature vector
  3. Predict HMM regime
  4. Evaluate risk gates (regime, drawdown, consecutive losses)
  5. Run XGBoost direction classifier → P(long)
  6. Run meta-label gate → P(bet)
  7. Compute Kelly position size
  8. Check position-size gate
  9. Return SignalResult to orchestrator

The signal engine owns no state beyond its injected dependencies.
All state lives in storage or the executor.

Authority:
  - López de Prado (2018) AFML Ch.3–4 signal construction
  - Hamilton (1989) regime gate
  - Kelly (1956) position sizing
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Final

import pandas as pd
import structlog
from xgboost import XGBClassifier

from src.config import Timeframe, get_settings
from src.data.fetcher import MarketDataFetcher
from src.data.storage import StorageBackend
from src.features.pipeline import (
    build_inference_features,
)
from src.regime.detector import RegimeDetector, RegimePrediction
from src.risk.gates import (
    GateResult,
    RiskGateContext,
    evaluate_all_gates,
)
from src.risk.kelly import KellyResult, compute_position_size
from src.models.trainer import ModelTrainer

log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

# Minimum bars in storage before a signal can be generated
_MIN_BARS_FOR_SIGNAL: Final[int] = 300


# ---------------------------------------------------------------------------
# Signal result
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SignalResult:
    """
    Complete output of one signal engine tick.

    tradeable     : True when all gates pass and meta-label says bet
    direction     : 1=long, 0=short (valid only when tradeable)
    p_long        : XGBoost P(long)
    p_bet         : meta-label P(bet)
    kelly_result  : position sizing (None when not tradeable)
    regime        : current regime prediction
    gate_result   : final gate evaluation result
    skip_reason   : human-readable reason when not tradeable
    """

    tradeable: bool
    direction: int
    p_long: float
    p_bet: float
    kelly_result: KellyResult | None
    regime: RegimePrediction | None
    gate_result: GateResult | None
    skip_reason: str


# ---------------------------------------------------------------------------
# SignalEngine
# ---------------------------------------------------------------------------


class SignalEngine:
    """
    Stateless per-timeframe signal computation engine.

    One instance per (symbol, timeframe).  Injected with fitted models
    and shared infrastructure objects by the orchestrator.

    Usage::

        engine = SignalEngine(
            symbol='BTC/USDT',
            timeframe=Timeframe.INTRADAY,
            storage=storage,
            fetcher=fetcher,
            detector=detector,
            direction_model=direction_model,
            meta_model=meta_model,
            trainer=trainer,
        )
        result = await engine.tick(
            capital_usd=1000.0,
            daily_pnl_usd=-5.0,
            starting_equity_usd=1000.0,
            consecutive_loss_count=1,
            direction_gate_pass=True,
            meta_gate_pass=True,
        )
    """

    def __init__(
        self,
        symbol: str,
        timeframe: Timeframe,
        storage: StorageBackend,
        fetcher: MarketDataFetcher,
        detector: RegimeDetector,
        direction_model: XGBClassifier,
        meta_model: XGBClassifier,
        trainer: ModelTrainer,
    ) -> None:
        self._symbol = symbol
        self._timeframe = timeframe
        self._storage = storage
        self._fetcher = fetcher
        self._detector = detector
        self._direction_model = direction_model
        self._meta_model = meta_model
        self._trainer = trainer
        self._cfg = get_settings()
        self._model_lock = asyncio.Lock()  # protects model hot-swap (fix #14)
        self._log = log.bind(
            component="signal_engine",
            symbol=symbol,
            timeframe=timeframe.value,
        )

    # ------------------------------------------------------------------
    # Atomic model swap — called by orchestrator after retraining (fix #14)
    # ------------------------------------------------------------------

    async def swap_models(
        self,
        direction_model: Any,
        meta_model: Any,
        detector: Any,
    ) -> None:
        """
        Atomically replace all three model objects under the model lock.

        Prevents a tick from reading a mismatched (v2 direction, v1 meta) pair
        during a concurrent hot-swap.
        """
        async with self._model_lock:
            self._direction_model = direction_model
            self._meta_model = meta_model
            self._detector = detector
        self._log.info("signal_engine.models_swapped")

    # ------------------------------------------------------------------
    # Main tick — called by orchestrator on every bar close
    # ------------------------------------------------------------------

    async def tick(
        self,
        capital_usd: float,
        daily_pnl_usd: float,
        starting_equity_usd: float,
        consecutive_loss_count: int,
        direction_gate_pass: bool,
        meta_gate_pass: bool,
        avg_win_usd: float = 0.0,
        avg_loss_usd: float = 0.0,
        paper_trading_days: int = 0,
    ) -> SignalResult:
        """
        Run one full signal computation cycle.

        Parameters
        ----------
        capital_usd           : current equity for position sizing
        daily_pnl_usd         : today's realized PnL for drawdown gate
        starting_equity_usd   : day-start equity for drawdown % calc
        consecutive_loss_count: trailing loss streak for consecutive gate
        direction_gate_pass   : direction model passed OOS live gate
        meta_gate_pass        : meta model passed OOS live gate
        avg_win_usd           : historical avg win for Kelly ratio
        avg_loss_usd          : historical avg loss for Kelly ratio

        Returns
        -------
        SignalResult — always returned; tradeable=False when any gate blocks.
        """
        # 1. Gap fill
        try:
            await self._fetcher.gap_fill(self._symbol, self._timeframe)
        except Exception as exc:
            self._log.error("signal.gap_fill_failed", error=str(exc))
            return self._skip("gap_fill_failed")

        # 2. Load bars
        bars = await self._load_bars()
        if bars is None:
            return self._skip("insufficient_bars")

        # 3. Build inference feature vector
        vec = build_inference_features(bars)
        if vec is None:
            return self._skip("insufficient_features")

        # 4. Live OFI override from order book
        try:
            ob = await self._fetcher.fetch_orderbook(self._symbol)
            live_ofi = ob.order_flow_imbalance()
            vec = build_inference_features(bars, live_ofi=live_ofi)
            if vec is None:
                return self._skip("insufficient_features_with_ofi")
        except Exception as exc:
            self._log.debug("signal.ofi_fetch_failed", error=str(exc))
            # Continue with OHLCV-derived OFI already in vec

        # 5. Regime prediction
        try:
            history_df = (
                bars[
                    [
                        "frac_diff",
                        "realized_vol_ratio",
                        "atr_momentum",
                        "rolling_sharpe",
                        "volume_zscore",
                    ]
                ]
                if all(
                    c in bars.columns
                    for c in [
                        "frac_diff",
                        "realized_vol_ratio",
                        "atr_momentum",
                        "rolling_sharpe",
                        "volume_zscore",
                    ]
                )
                else None
            )
        except Exception:
            history_df = None

        # 6. Direction prediction — read models under lock (fix #14)
        async with self._model_lock:
            direction_model = self._direction_model
            meta_model = self._meta_model
            detector = self._detector

        regime: RegimePrediction | None = None
        if history_df is not None and detector.is_fitted():
            try:
                regime = detector.predict_current(history_df, lookback=100)
            except Exception as exc:
                self._log.error(
                    "signal.regime_failed_defaulting_volatile",
                    error=str(exc),
                )
                # Fail-safe: default to VOLATILE so regime gate blocks new positions
                # until detector recovers — never default to RANGING (least restrictive)

        from src.config import REGIME_VOLATILE as _REGIME_VOLATILE
        regime_state = regime.state if regime is not None else _REGIME_VOLATILE

        direction, p_long = self._trainer.predict_direction(direction_model, vec)

        # 7. Kelly sizing (pre-gate — needed for position-size gate)
        kelly_result = compute_position_size(
            p_long=p_long,
            direction=direction,
            capital_usd=capital_usd,
            entry_price=float(bars["close"].iloc[-1]),
            avg_win_usd=avg_win_usd,
            avg_loss_usd=avg_loss_usd,
        )

        notional = kelly_result.notional_usd if kelly_result is not None else 0.0

        # 8. Risk gate stack
        gate_ctx = RiskGateContext(
            daily_pnl_usd=daily_pnl_usd,
            starting_equity_usd=starting_equity_usd,
            consecutive_loss_count=consecutive_loss_count,
            regime_state=regime_state,
            notional_usd=notional,
            capital_usd=capital_usd,
            trading_mode=self._cfg.trading_mode,
            direction_gate_pass=direction_gate_pass,
            meta_gate_pass=meta_gate_pass,
            paper_trading_days=paper_trading_days,
        )
        gate_result = evaluate_all_gates(gate_ctx)

        if not gate_result.passed:
            return SignalResult(
                tradeable=False,
                direction=direction,
                p_long=p_long,
                p_bet=0.0,
                kelly_result=None,
                regime=regime,
                gate_result=gate_result,
                skip_reason=gate_result.status.value,
            )

        if kelly_result is None:
            return self._skip("kelly_size_zero")

        # 9. Meta-label gate
        meta_label, p_bet = self._trainer.predict_meta(meta_model, vec, p_long)

        if meta_label == 0:
            return SignalResult(
                tradeable=False,
                direction=direction,
                p_long=p_long,
                p_bet=p_bet,
                kelly_result=kelly_result,
                regime=regime,
                gate_result=gate_result,
                skip_reason="meta_label_gate_skip",
            )

        self._log.info(
            "signal.tradeable",
            direction="long" if direction == 1 else "short",
            p_long=round(p_long, 4),
            p_bet=round(p_bet, 4),
            regime_state=regime_state,
            notional_usd=round(notional, 2),
            kelly_fraction=round(kelly_result.adjusted_fraction, 4),
        )

        return SignalResult(
            tradeable=True,
            direction=direction,
            p_long=p_long,
            p_bet=p_bet,
            kelly_result=kelly_result,
            regime=regime,
            gate_result=gate_result,
            skip_reason="",
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _load_bars(self) -> pd.DataFrame | None:
        """Load recent bars from storage as a DataFrame."""
        records = await self._storage.fetch_bars(
            symbol=self._symbol,
            timeframe=self._timeframe.value,
            since_ts=0,
            limit=_MIN_BARS_FOR_SIGNAL + 200,
        )
        if len(records) < _MIN_BARS_FOR_SIGNAL:
            self._log.warning(
                "signal.insufficient_bars",
                n_bars=len(records),
                min_required=_MIN_BARS_FOR_SIGNAL,
            )
            return None

        df = pd.DataFrame(
            {
                "open": [r.open for r in records],
                "high": [r.high for r in records],
                "low": [r.low for r in records],
                "close": [r.close for r in records],
                "volume": [r.volume for r in records],
                "quote_volume": [r.quote_volume for r in records],
                "taker_buy_vol": [r.taker_buy_vol for r in records],
            },
            index=[r.ts for r in records],
        )
        return df.sort_index()

    def _skip(self, reason: str) -> SignalResult:
        self._log.debug("signal.skip", reason=reason)
        return SignalResult(
            tradeable=False,
            direction=0,
            p_long=0.5,
            p_bet=0.0,
            kelly_result=None,
            regime=None,
            gate_result=None,
            skip_reason=reason,
        )
