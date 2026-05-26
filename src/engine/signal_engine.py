"""
Signal engine — orchestrates feature pipeline, regime detection,
primary + meta model prediction, Kelly sizing, and risk gating
for a single (exchange, symbol, timeframe) combination.
"""
from __future__ import annotations
import asyncio
import numpy as np
import pandas as pd
from datetime import datetime, timezone
from typing import Callable, Awaitable
import structlog

from src.features.pipeline  import build_features
from src.regime.detector     import RegimeDetector
from src.models.trainer      import ModelTrainer
from src.risk.kelly          import KellySizer
from src.risk.gates          import RiskGate
from src.data.fetcher        import OHLCVFetcher, TIMEFRAME_MAP, MIN_BARS
from src.config              import get_settings

log = structlog.get_logger()

META_THRESHOLD   = 0.55   # meta-label confidence must exceed this to trade
PRIMARY_THRESHOLD = 0.55  # primary confidence must exceed this to trade


class SignalEngine:
    def __init__(
        self,
        exchange_id: str,
        symbol:      str,
        timeframe:   str,
        fetcher:     OHLCVFetcher,
        risk_gate:   RiskGate,
        on_signal:   Callable[[dict], Awaitable[None]],
    ):
        self._exchange_id = exchange_id
        self._symbol      = symbol
        self._timeframe   = timeframe
        self._fetcher     = fetcher
        self._risk_gate   = risk_gate
        self._on_signal   = on_signal

        self._regime    = RegimeDetector(model_path=f"./models/hmm_{exchange_id}_{symbol.replace('/','_')}_{timeframe}.pkl")
        self._trainer   = ModelTrainer(model_dir="./models")
        self._kelly     = KellySizer(lookback=50)
        self._history:  pd.DataFrame = pd.DataFrame()
        self._trained   = False
        self._active    = True

    async def initialize(self) -> bool:
        """Fetch history, fit/load models. Returns True if ready to trade."""
        cfg = get_settings()
        log.info("initializing signal engine", exchange=self._exchange_id,
                 symbol=self._symbol, timeframe=self._timeframe)

        ccxt_tf = TIMEFRAME_MAP[self._timeframe]
        self._history = await self._fetcher.fetch_full_history(
            self._symbol, ccxt_tf, days={"scalping": 90, "intraday": 365, "swing": 730}[self._timeframe]
        )

        if len(self._history) < MIN_BARS[self._timeframe]:
            log.warning("insufficient history", bars=len(self._history), required=MIN_BARS[self._timeframe])
            return False

        # Regime model
        if not self._regime.load():
            self._regime.fit(self._history)

        # Primary + meta models
        if not self._trainer.load(self._timeframe):
            features = build_features(self._history, self._timeframe)
            metrics  = self._trainer.train(features, self._history["close"], self._timeframe)
            if metrics.get("status") != "trained":
                log.warning("training failed", metrics=metrics)
                return False
            log.info("training complete", **{k: round(v, 4) if isinstance(v, float) else v
                                             for k, v in metrics.items()})

        self._trained = True
        return True

    async def run(self):
        """Stream real-time bars and emit signals."""
        if not self._trained:
            ok = await self.initialize()
            if not ok:
                log.error("engine failed to initialize — not running", symbol=self._symbol)
                return

        ccxt_tf = TIMEFRAME_MAP[self._timeframe]
        log.info("engine running", symbol=self._symbol, timeframe=self._timeframe)

        async for bar in self._fetcher.stream_ticks(self._symbol, ccxt_tf):
            if not self._active:
                break

            # Append bar to history
            new_row = pd.DataFrame([{
                "open": bar["open"], "high": bar["high"],
                "low": bar["low"], "close": bar["close"], "volume": bar["volume"],
            }], index=[bar["ts"]])
            self._history = pd.concat([self._history, new_row]).tail(2000)

            await self._evaluate()

    async def _evaluate(self):
        cfg = get_settings()
        try:
            features = build_features(self._history.tail(200), self._timeframe)
            if features.empty:
                return

            regime = self._regime.current_regime(self._history.tail(200))
            row    = features.iloc[-1].values

            direction, primary_conf, meta_conf = self._trainer.predict(row)

            if primary_conf < PRIMARY_THRESHOLD:
                return
            if meta_conf < META_THRESHOLD:
                return

            kelly_frac = self._kelly.fraction()
            capital    = self._risk_gate.session.current_equity
            price      = float(self._history["close"].iloc[-1])
            notional   = capital * kelly_frac

            approved, reason = self._risk_gate.check(notional, capital, regime)
            if not approved:
                log.debug("risk gate rejected", reason=reason, symbol=self._symbol)
                return

            signal = {
                "ts":           datetime.now(timezone.utc).isoformat(),
                "exchange":     self._exchange_id,
                "symbol":       self._symbol,
                "timeframe":    self._timeframe,
                "direction":    "long" if direction == 1 else "short",
                "confidence":   round(primary_conf, 4),
                "meta_score":   round(meta_conf, 4),
                "regime":       regime,
                "kelly_frac":   round(kelly_frac, 4),
                "notional":     round(notional, 2),
                "price":        price,
                "approved":     True,
            }
            log.info("signal generated", **{k: v for k, v in signal.items() if k != "ts"})
            await self._on_signal(signal)

        except Exception as e:
            log.error("evaluation error", error=str(e), symbol=self._symbol)

    def record_result(self, pnl_pct: float):
        self._kelly.record(pnl_pct)

    def stop(self):
        self._active = False

