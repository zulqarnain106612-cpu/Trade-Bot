"""
Auto-tuning scheduler -- the "explicit startup step" referenced by
src/tuning/bootstrap.py's module docstring, and the automated counterpart
to scripts/run_tuning_attempt.py (which remains the manual entrypoint;
this module reuses the exact same process-wide singletons from
src/tuning/state.py so both paths share one audit trail / version store).

Started once from the API lifespan when SelfTuningSettings.enabled is
true. Every safety rail from the original design stays in force:

  - SelfTuningSettings.enabled is still the master kill switch (off by
    default; operator must set SELF_TUNING_ENABLED=true).
  - SelfTuningSettings.shadow_mode stays True by default -- accepted
    challengers are logged as WOULD_PROMOTE, never written to
    VersionedConfigStore, until an operator sets SELF_TUNING_SHADOW_MODE=false.
  - tuning_pause_state (the operator's runtime pause switch, POST
    /tuning/pause) is honored -- pausing via the API stops this loop's
    attempts on the next cycle, same as it stops a manual script run.
  - TuningRunner still enforces the per-parameter cooldown + gate + never-
    regress checks on every attempt.

`hmm.entropy_threshold` / `hmm.entropy_scalar_floor` (Phase 4),
`risk.slippage_impact_coeff_bps` (Phase 8 item 2), and the five
`features.*_window` parameters (Phase 8 item 3) each have a working
backtest harness (run_entropy_threshold_backtest /
run_slippage_coeff_backtest / run_feature_window_backtest respectively).
Any other registered parameter with no evaluate_fn is intentionally left
unscheduled here.

The feature-window harness additionally requires a previously trained,
saved direction model (ModelTrainer.load_direction) -- on a fresh
deployment with no model trained yet, that parameter group is skipped
cleanly every cycle (FileNotFoundError is an expected state, not an
error) until training produces one.
"""

from __future__ import annotations

import asyncio
import math

import pandas as pd
import structlog
from xgboost import XGBClassifier

from src.config import Settings
from src.data.storage import AnyStorageBackend
from src.models.trainer import ModelTrainer
from src.tuning.backtest_harness import (
    SlippageFillSample,
    TradeSample,
    run_entropy_threshold_backtest,
    run_feature_window_backtest,
    run_slippage_coeff_backtest,
)
from src.tuning.bootstrap import (
    FEATURE_WINDOW_FIELDS,
    register_feature_window_param,
    register_hmm_entropy_scalar_floor,
    register_hmm_entropy_threshold,
    register_slippage_impact_coeff,
)
from src.tuning.evaluator import MetricComparison
from src.tuning.proposer import Proposal
from src.tuning.registry import TunableParameter
from src.tuning.state import parameter_registry, pause_state, runner


log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

_MIN_SAMPLES = 30  # below this, CPCV folds would be too thin to mean anything
_MIN_FEATURE_BARS = 300  # below this, CPCV folds over bars would be too thin
_FEATURE_BAR_FETCH_LIMIT = 5000


def _shannon_entropy(p_ranging: float, p_trending: float, p_volatile: float) -> float:
    """Normalized Shannon entropy of the 3-state posterior -- same
    normalization as RegimeDetector (src/regime/detector.py) so historical
    entropy is comparable to the live value used by position_scalar()."""
    probs = [p for p in (p_ranging, p_trending, p_volatile) if p > 0.0]
    if not probs:
        return 0.0
    max_entropy = math.log(3)
    if max_entropy <= 0.0:
        return 0.0
    raw = -sum(p * math.log(p) for p in probs)
    return max(0.0, min(1.0, raw / max_entropy))


class AutoTuningScheduler:
    """Runs one propose/evaluate/gate attempt per registered hmm-entropy
    parameter on a fixed wall-clock interval. TuningRunner's own cooldown
    still applies on top of this interval."""

    def __init__(
        self,
        storage: AnyStorageBackend,
        settings: Settings,
        symbol: str,
        timeframe: str,
        interval_hours: float = 1.0,
    ) -> None:
        self._storage = storage
        self._settings = settings
        self._symbol = symbol
        self._timeframe = timeframe
        self._interval_s = max(60.0, interval_hours * 3600.0)
        self._task: asyncio.Task[None] | None = None
        self._stopped = False

    def start(self) -> None:
        if not parameter_registry.is_registered("hmm.entropy_threshold"):
            register_hmm_entropy_threshold(parameter_registry, self._settings)
        if not parameter_registry.is_registered("hmm.entropy_scalar_floor"):
            register_hmm_entropy_scalar_floor(parameter_registry, self._settings)
        if not parameter_registry.is_registered("risk.slippage_impact_coeff_bps"):
            register_slippage_impact_coeff(parameter_registry, self._settings)
        for field_name in FEATURE_WINDOW_FIELDS:
            if not parameter_registry.is_registered(f"features.{field_name}"):
                register_feature_window_param(parameter_registry, field_name, self._settings)
        self._task = asyncio.create_task(self._loop(), name="auto_tuning_scheduler")
        log.info(
            "tuning.scheduler_started",
            interval_hours=self._interval_s / 3600.0,
            shadow_mode=self._settings.self_tuning.shadow_mode,
        )

    def stop(self) -> None:
        self._stopped = True
        if self._task is not None:
            self._task.cancel()

    async def _loop(self) -> None:
        while not self._stopped:
            try:
                if await pause_state.is_paused():
                    log.info("tuning.scheduler_paused")
                else:
                    await self._attempt_all()
            except Exception as exc:
                log.error("tuning.scheduler_attempt_failed", error=str(exc))
            try:
                await asyncio.sleep(self._interval_s)
            except asyncio.CancelledError:
                return

    async def _attempt_all(self) -> None:
        # NOTE: each parameter group below (entropy, slippage, feature-window)
        # draws on independent data (trades vs. bars) and must not be gated
        # behind another group's sample-sufficiency check -- an `if
        # insufficient: return` here previously skipped slippage AND
        # feature-window attempts too whenever entropy had too few closed
        # trades, even though feature-window tuning needs only bar history.
        samples = await self._build_trade_samples()
        if len(samples) < _MIN_SAMPLES:
            log.info("tuning.scheduler_insufficient_samples", n_samples=len(samples))
        else:
            champion_threshold = self._settings.hmm.entropy_threshold
            champion_floor = self._settings.hmm.entropy_scalar_floor

            for param_name in ("hmm.entropy_threshold", "hmm.entropy_scalar_floor"):

                def evaluate(
                    _param: TunableParameter,
                    proposal: Proposal,
                    _param_name: str = param_name,
                ) -> list[MetricComparison]:
                    if _param_name == "hmm.entropy_threshold":
                        return run_entropy_threshold_backtest(
                            samples,
                            champion_threshold=proposal.champion_value,
                            champion_floor=champion_floor,
                            challenger_threshold=proposal.challenger_value,
                            challenger_floor=champion_floor,
                            features_cfg=self._settings.features,
                        )
                    return run_entropy_threshold_backtest(
                        samples,
                        champion_threshold=champion_threshold,
                        champion_floor=proposal.champion_value,
                        challenger_threshold=champion_threshold,
                        challenger_floor=proposal.challenger_value,
                        features_cfg=self._settings.features,
                    )

                try:
                    result = runner.attempt(param_name, evaluate, primary_metric="oos_sharpe")
                    log.info(
                        "tuning.scheduler_attempt",
                        param=param_name,
                        attempted=result.attempted,
                        accepted=result.accepted,
                        promoted=result.promoted,
                        reasons=result.reasons,
                    )
                except Exception as exc:
                    log.error("tuning.scheduler_attempt_error", param=param_name, error=str(exc))

        slippage_samples = await self._build_slippage_samples()
        if len(slippage_samples) < _MIN_SAMPLES:
            log.info(
                "tuning.scheduler_insufficient_slippage_samples", n_samples=len(slippage_samples)
            )
        else:

            def evaluate_slippage(
                _param: TunableParameter, proposal: Proposal
            ) -> list[MetricComparison]:
                return run_slippage_coeff_backtest(
                    slippage_samples,
                    champion_coeff=proposal.champion_value,
                    challenger_coeff=proposal.challenger_value,
                    features_cfg=self._settings.features,
                )

            try:
                result = runner.attempt(
                    "risk.slippage_impact_coeff_bps",
                    evaluate_slippage,
                    primary_metric="slippage_prediction_accuracy",
                )
                log.info(
                    "tuning.scheduler_attempt",
                    param="risk.slippage_impact_coeff_bps",
                    attempted=result.attempted,
                    accepted=result.accepted,
                    promoted=result.promoted,
                    reasons=result.reasons,
                )
            except Exception as exc:
                log.error(
                    "tuning.scheduler_attempt_error",
                    param="risk.slippage_impact_coeff_bps",
                    error=str(exc),
                )

        direction_model = self._load_direction_model()
        if direction_model is None:
            log.info("tuning.scheduler_no_direction_model")
        else:
            bars_df = await self._build_feature_bars_df()
            if bars_df is None or len(bars_df) < _MIN_FEATURE_BARS:
                log.info(
                    "tuning.scheduler_insufficient_feature_bars",
                    n_bars=0 if bars_df is None else len(bars_df),
                )
            else:
                for field_name in FEATURE_WINDOW_FIELDS:
                    param_name = f"features.{field_name}"

                    def evaluate_feature_window(
                        _param: TunableParameter,
                        proposal: Proposal,
                        _field_name: str = field_name,
                    ) -> list[MetricComparison]:
                        return run_feature_window_backtest(
                            bars_df,
                            field_name=_field_name,
                            champion_window=max(2, round(proposal.champion_value)),
                            challenger_window=max(2, round(proposal.challenger_value)),
                            direction_model=direction_model,
                            features_cfg=self._settings.features,
                        )

                    try:
                        result = runner.attempt(
                            param_name, evaluate_feature_window, primary_metric="oos_sharpe"
                        )
                        log.info(
                            "tuning.scheduler_attempt",
                            param=param_name,
                            attempted=result.attempted,
                            accepted=result.accepted,
                            promoted=result.promoted,
                            reasons=result.reasons,
                        )
                    except Exception as exc:
                        log.error(
                            "tuning.scheduler_attempt_error", param=param_name, error=str(exc)
                        )

    def _load_direction_model(self) -> XGBClassifier | None:
        try:
            return ModelTrainer.load_direction(
                self._settings.storage.model_dir, self._symbol, self._timeframe
            )
        except FileNotFoundError:
            return None

    async def _build_feature_bars_df(self) -> pd.DataFrame | None:
        latest_ts = await self._storage.latest_bar_ts(self._symbol, self._timeframe)
        if latest_ts is None:
            return None
        bars = await self._storage.bars_before(
            self._symbol, self._timeframe, latest_ts, limit=_FEATURE_BAR_FETCH_LIMIT
        )
        if not bars:
            return None
        return pd.DataFrame(
            {
                "open": [b.open for b in bars],
                "high": [b.high for b in bars],
                "low": [b.low for b in bars],
                "close": [b.close for b in bars],
                "volume": [b.volume for b in bars],
            },
            index=[b.ts for b in bars],
        )

    async def _build_slippage_samples(self) -> list[SlippageFillSample]:
        trades = await self._storage.fetch_trades(
            symbol=self._symbol,
            trading_mode=self._settings.trading_mode.value,
            limit=1000,
        )
        spread_bps = self._settings.risk.slippage_default_spread_bps
        samples: list[SlippageFillSample] = []
        for t in trades:
            if t.entry_price <= 0.0 or t.quantity <= 0.0:
                continue
            bars = await self._storage.bars_before(
                self._symbol, self._timeframe, t.entry_ts, limit=21
            )
            if not bars:
                continue
            reference_price = bars[-1].close
            if reference_price <= 0.0:
                continue
            history = bars[:-1] if len(bars) > 1 else bars
            adv_20d = sum(b.volume for b in history) / len(history)
            if adv_20d <= 0.0:
                continue
            samples.append(
                SlippageFillSample(
                    reference_price=reference_price,
                    fill_price=t.entry_price,
                    qty=t.quantity,
                    adv_20d=adv_20d,
                    spread_bps=spread_bps,
                    direction=t.direction,
                )
            )
        samples.reverse()
        return samples

    async def _build_trade_samples(self) -> list[TradeSample]:
        trades = await self._storage.fetch_trades(
            symbol=self._symbol,
            trading_mode=self._settings.trading_mode.value,
            limit=1000,
        )
        samples: list[TradeSample] = []
        for t in trades:
            if t.exit_price is None or t.entry_price <= 0.0:
                continue
            snap = await self._storage.regime_snapshot_before(
                self._symbol, self._timeframe, t.entry_ts
            )
            if snap is None:
                continue
            entropy = _shannon_entropy(snap.prob_ranging, snap.prob_trending, snap.prob_volatile)
            raw_return = (t.exit_price / t.entry_price - 1.0) * (1 if t.direction == 1 else -1)
            samples.append(TradeSample(entropy=entropy, raw_return=raw_return))
        # Oldest-first so fold construction (contiguous blocks) reflects
        # chronological order, matching the harness's fold-purging assumption.
        samples.reverse()
        return samples
