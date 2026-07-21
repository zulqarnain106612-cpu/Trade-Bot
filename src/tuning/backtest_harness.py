"""
Backtest harness — produces real MetricComparison samples for the
self-tuning evaluator from historical (regime, trade outcome) data.

Design: docs/SELF_TUNING_IMPLEMENTATION_PLAN.md Phase 3.

For `hmm.entropy_threshold` / `hmm.entropy_scalar_floor`, no retraining is
required: RegimePrediction.position_scalar(cfg) is a pure function of a
stored entropy value and the HMM config, so champion vs. challenger can
be replayed against the same historical (entropy, raw_return) pairs
without re-running the detector or re-fitting anything. This keeps the
cheapest, lowest-risk parameters cheap to evaluate, matching the priority
order in the design doc's parameter table (§3).

Fold construction follows the same purge-gap principle as the live CPCV
split (FeatureSettings.cpcv_n_splits / purge_gap_bars) -- contiguous
blocks with a gap between them -- so adjacent folds aren't dominated by
the same short-term autocorrelated stretch of trades, without requiring
the full walk-forward train/test machinery (there is no "training" step
here, only a deterministic re-scaling of realized returns).
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Callable
from dataclasses import dataclass

import numpy as np
import pandas as pd
from xgboost import XGBClassifier

from src.config import FeatureSettings, HMMSettings, XGBoostSettings
from src.data.storage import TradeRecord
from src.features.pipeline import (
    COL_ATR_MOMENTUM,
    COL_OFI,
    COL_ROLLING_SHARPE,
    COL_VOLUME_ZSCORE,
    COL_VWAP_DEV,
    FEATURE_COLUMNS,
    FeatureMatrix,
    atr_momentum,
    build_feature_matrix,
    order_flow_imbalance,
    rolling_sharpe,
    volume_zscore,
    vwap_deviation_zscore,
)
from src.models.trainer import ModelTrainer, oos_sharpe_and_drawdown
from src.tuning.bootstrap import XGBOOST_HYPERPARAM_FIELDS
from src.tuning.evaluator import (
    ChallengerEvaluator,
    MetricComparison,
    probabilistic_sharpe_ratio,
)


@dataclass(frozen=True)
class TradeSample:
    """One historical closed trade, enriched with the regime entropy at
    signal time. `raw_return` is the trade's realized return as a fraction
    of notional, computed at full (unscaled) position size."""

    entropy: float
    raw_return: float


class InsufficientDataError(ValueError):
    """Raised when there are too few trade samples to form the configured folds."""


def _position_scalar(entropy: float, threshold: float, floor: float) -> float:
    """Standalone reimplementation of RegimePrediction.position_scalar's
    math, operating on a stored entropy value rather than a live
    RegimePrediction object (avoids needing to reconstruct one)."""
    if entropy <= threshold:
        return 1.0
    span = 1.0 - threshold
    if span <= 0.0:
        return floor
    t = min(1.0, (entropy - threshold) / span)
    return 1.0 - t * (1.0 - floor)


def _make_folds(n: int, n_splits: int, purge_gap: int) -> list[tuple[int, int]]:
    """Contiguous index folds over [0, n) with a purge gap between them."""
    if n_splits < 1:
        raise ValueError(f"n_splits must be >= 1, got {n_splits}")
    fold_width = n // n_splits
    if fold_width <= purge_gap:
        raise InsufficientDataError(
            f"not enough samples ({n}) to form {n_splits} folds with "
            f"purge_gap={purge_gap} (need > {purge_gap} samples per fold)"
        )
    folds: list[tuple[int, int]] = []
    start = 0
    for i in range(n_splits):
        end = start + fold_width if i < n_splits - 1 else n
        usable_end = max(start, end - purge_gap) if i < n_splits - 1 else end
        folds.append((start, usable_end))
        start = end
    return folds


def _max_drawdown_inverted(returns: list[float]) -> float:
    """1 - max_drawdown_fraction over the cumulative return path within a
    fold. Higher is better, consistent with the "higher is better"
    convention ChallengerEvaluator expects."""
    cumulative = 0.0
    peak = 0.0
    max_dd = 0.0
    for r in returns:
        cumulative += r
        peak = max(peak, cumulative)
        drawdown = peak - cumulative
        max_dd = max(max_dd, drawdown)
    return 1.0 - max_dd


def run_entropy_threshold_backtest(
    samples: list[TradeSample],
    champion_threshold: float,
    champion_floor: float,
    challenger_threshold: float,
    challenger_floor: float,
    features_cfg: FeatureSettings | None = None,
) -> list[MetricComparison]:
    """
    Replay historical trades under champion vs. challenger entropy-gate
    config, fold the results per FeatureSettings.cpcv_n_splits with a
    purge gap, and return significance-tested comparisons for
    oos_sharpe, win_rate, and max_drawdown_inverted.

    Only entropy_threshold/entropy_scalar_floor are varied here (matches
    the eval_strategy="cpcv_oos_sharpe" contract for those two
    parameters specifically -- a different parameter would need its own
    harness function with its own re-scaling logic).
    """
    if features_cfg is None:
        features_cfg = FeatureSettings()

    folds = _make_folds(len(samples), features_cfg.cpcv_n_splits, features_cfg.purge_gap_bars)

    champion_fold_sharpes: list[float] = []
    challenger_fold_sharpes: list[float] = []
    champion_fold_dd: list[float] = []
    challenger_fold_dd: list[float] = []
    champion_fold_psr: list[float] = []
    challenger_fold_psr: list[float] = []
    champion_wins = 0
    challenger_wins = 0
    total_trades = 0

    for start, end in folds:
        fold_samples = samples[start:end]
        if not fold_samples:
            continue

        champion_returns = [
            s.raw_return * _position_scalar(s.entropy, champion_threshold, champion_floor)
            for s in fold_samples
        ]
        challenger_returns = [
            s.raw_return * _position_scalar(s.entropy, challenger_threshold, challenger_floor)
            for s in fold_samples
        ]

        champion_fold_sharpes.append(_fold_sharpe(champion_returns))
        challenger_fold_sharpes.append(_fold_sharpe(challenger_returns))
        champion_fold_dd.append(_max_drawdown_inverted(champion_returns))
        challenger_fold_dd.append(_max_drawdown_inverted(challenger_returns))
        # Bailey & Lopez de Prado PSR -- confidence the fold's true Sharpe
        # ratio is positive, correcting for skew/kurtosis of the fold's
        # return distribution rather than assuming normality (AFML Ch. 14).
        champion_fold_psr.append(probabilistic_sharpe_ratio(champion_returns))
        challenger_fold_psr.append(probabilistic_sharpe_ratio(challenger_returns))

        champion_wins += sum(1 for r in champion_returns if r > 0)
        challenger_wins += sum(1 for r in challenger_returns if r > 0)
        total_trades += len(fold_samples)

    evaluator = ChallengerEvaluator()
    comparisons = [
        evaluator.compare_metric("oos_sharpe", champion_fold_sharpes, challenger_fold_sharpes),
        evaluator.compare_metric("max_drawdown_inverted", champion_fold_dd, challenger_fold_dd),
        evaluator.compare_metric(
            "probabilistic_sharpe_ratio", champion_fold_psr, challenger_fold_psr
        ),
        evaluator.compare_proportion(
            "win_rate",
            champion_p=champion_wins / total_trades if total_trades else 0.0,
            champion_n=total_trades,
            challenger_p=challenger_wins / total_trades if total_trades else 0.0,
            challenger_n=total_trades,
        ),
    ]
    return comparisons


def _fold_sharpe(returns: list[float]) -> float:
    if len(returns) < 2:
        return 0.0
    mean = statistics.mean(returns)
    stdev = statistics.stdev(returns)
    if stdev <= 0.0:
        return 0.0
    return mean / stdev


def default_hmm_cfg_for(threshold: float, floor: float) -> HMMSettings:
    """Convenience for callers that want a full HMMSettings with just the
    entropy fields overridden, e.g. for logging/audit evidence."""
    base = HMMSettings()
    return base.model_copy(update={"entropy_threshold": threshold, "entropy_scalar_floor": floor})


@dataclass(frozen=True)
class SlippageFillSample:
    """One historical filled trade, enriched with the reference price and
    ADV-20d that were live at signal time.

    `reference_price` is the close of the most recent bar at/before the
    trade's entry timestamp (a proxy for the pre-order decision price --
    no separate arrival-price capture exists on the live/paper execution
    path today, see docs/SELF_TUNING_IMPLEMENTATION_PLAN.md Phase 8 item 2).
    `fill_price` is the trade's recorded entry_price. `spread_bps` is the
    configured default spread assumption (no historical order-book spread
    is persisted, so champion and challenger share the same spread input
    and only the impact-coefficient term is being recalibrated).
    """

    reference_price: float
    fill_price: float
    qty: float
    adv_20d: float
    spread_bps: float
    direction: int


def _realized_slippage_bps(sample: SlippageFillSample) -> float:
    sign = 1.0 if sample.direction == 1 else -1.0
    return (sample.fill_price - sample.reference_price) / sample.reference_price * 10_000.0 * sign


def _predicted_slippage_bps(sample: SlippageFillSample, impact_coeff_bps: float) -> float:
    participation = sample.qty / sample.adv_20d if sample.adv_20d > 0.0 else 0.0
    return sample.spread_bps + impact_coeff_bps * math.sqrt(max(0.0, participation))


def run_slippage_coeff_backtest(
    samples: list[SlippageFillSample],
    champion_coeff: float,
    challenger_coeff: float,
    features_cfg: FeatureSettings | None = None,
) -> list[MetricComparison]:
    """
    Recalibrate `risk.slippage_impact_coeff_bps` (Phase 8 item 2) against
    realized fill cost: for each fold, compare how far the champion vs.
    challenger coefficient's predicted total slippage (spread + Almgren-
    Chriss impact) falls from the realized (fill_price vs. reference_price)
    slippage actually observed.

    Two metrics, both "higher is better" after negation per
    src.tuning.evaluator's convention:
      - slippage_prediction_accuracy: -mean(|predicted - realized|) per fold
        (overall calibration error magnitude).
      - slippage_prediction_bias: -|mean(predicted - realized)| per fold
        (systematic over/under-estimation, distinct from raw magnitude --
        a coefficient can have low bias but high variance or vice versa).
    """
    if features_cfg is None:
        features_cfg = FeatureSettings()

    folds = _make_folds(len(samples), features_cfg.cpcv_n_splits, features_cfg.purge_gap_bars)

    champion_neg_mae: list[float] = []
    challenger_neg_mae: list[float] = []
    champion_neg_bias: list[float] = []
    challenger_neg_bias: list[float] = []

    for start, end in folds:
        fold_samples = samples[start:end]
        if not fold_samples:
            continue

        realized = [_realized_slippage_bps(s) for s in fold_samples]
        champion_errors = [
            _predicted_slippage_bps(s, champion_coeff) - r
            for s, r in zip(fold_samples, realized, strict=True)
        ]
        challenger_errors = [
            _predicted_slippage_bps(s, challenger_coeff) - r
            for s, r in zip(fold_samples, realized, strict=True)
        ]

        champion_neg_mae.append(-statistics.mean(abs(e) for e in champion_errors))
        challenger_neg_mae.append(-statistics.mean(abs(e) for e in challenger_errors))
        champion_neg_bias.append(-abs(statistics.mean(champion_errors)))
        challenger_neg_bias.append(-abs(statistics.mean(challenger_errors)))

    evaluator = ChallengerEvaluator()
    return [
        evaluator.compare_metric(
            "slippage_prediction_accuracy", champion_neg_mae, challenger_neg_mae
        ),
        evaluator.compare_metric(
            "slippage_prediction_bias", champion_neg_bias, challenger_neg_bias
        ),
    ]


# ---------------------------------------------------------------------------
# risk.ensemble_blend_weight -- EnsemblePredictor blend recalibration
# ---------------------------------------------------------------------------
#
# "Prediction accuracy" style, like run_slippage_coeff_backtest (recalibrate
# a coefficient against a realized error signal), NOT the Kelly/regime-input
# style of run_entropy_threshold_backtest (replay a pure function of a
# stored regime value). No retraining or resimulated fills -- only the
# blended p_long implied by a candidate blend weight is recomputed and
# scored against the trade's already-realized outcome.


@dataclass(frozen=True)
class EnsembleBlendSample:
    """
    One historical closed trade with both the XGBoost direction model's
    p_long and EnsemblePredictor's point_estimate logged at signal time
    (src/engine/signal_engine.py's ensemble-blend wiring; persisted via
    trades.ensemble_point_estimate / trades.ensemble_blend_weight --
    src/data/storage.py's update_trade_ensemble_fields).

    `raw_p_long` is the XGBoost model's OWN probability BEFORE blending --
    reconstructed from the persisted post-blend `raw_signal` via the inverse
    of signal_engine.py's linear blend (see
    ensemble_blend_samples_from_trades), not read from a separate column.
    This lets the harness replay what p_long WOULD have been under any
    candidate blend weight, not just the one that was live when the trade
    was taken.
    """

    raw_p_long: float
    ensemble_point_estimate: float
    direction: int  # 1=long, 0=short -- the direction actually traded
    raw_return: float  # realized return, signed to the direction actually traded


def ensemble_blend_samples_from_trades(
    trades: list[TradeRecord],
) -> list[EnsembleBlendSample]:
    """
    Build EnsembleBlendSample list from closed TradeRecord rows
    (src/data/storage.py). Skips trades where ensemble blending wasn't
    active (ensemble_point_estimate/ensemble_blend_weight NULL -- the
    predictor wasn't injected, or RiskSettings.ensemble_blend_weight was
    0.0 at signal time) or not yet closed (exit_price is None), and skips
    blend_weight in {0.0, 1.0} where the champion-vs-challenger inversion
    below is degenerate (0.0: raw_p_long IS raw_signal already, no
    reconstruction needed but also no ensemble signal was actually blended
    in, so there is nothing to recalibrate from; 1.0: raw_signal carries no
    information about raw_p_long at all).
    """
    samples: list[EnsembleBlendSample] = []
    for t in trades:
        if t.exit_price is None or t.entry_price <= 0.0:
            continue
        w = t.ensemble_blend_weight
        e = t.ensemble_point_estimate
        p_blended = t.raw_signal
        if w is None or e is None or p_blended is None:
            continue
        if not (0.0 < w < 1.0):
            continue
        # Inverse of signal_engine.py's p_long = (1-w)*raw_p_long + w*e.
        raw_p_long = (p_blended - w * e) / (1.0 - w)
        raw_p_long = min(max(raw_p_long, 0.0), 1.0)
        raw_return = (t.exit_price / t.entry_price - 1.0) * (1 if t.direction == 1 else -1)
        samples.append(
            EnsembleBlendSample(
                raw_p_long=raw_p_long,
                ensemble_point_estimate=e,
                direction=t.direction,
                raw_return=raw_return,
            )
        )
    samples.reverse()  # oldest-first, matching every other *_samples_from_* builder
    return samples


def _blended_p_long(sample: EnsembleBlendSample, weight: float) -> float:
    return (1.0 - weight) * sample.raw_p_long + weight * sample.ensemble_point_estimate


def run_ensemble_blend_backtest(
    samples: list[EnsembleBlendSample],
    champion_weight: float,
    challenger_weight: float,
    features_cfg: FeatureSettings | None = None,
) -> list[MetricComparison]:
    """
    Recalibrate `risk.ensemble_blend_weight` against realized OOS trade
    outcomes.

    Two metrics, both "higher is better" per src.tuning.evaluator's
    convention:
      - ensemble_prediction_accuracy: -mean((p_win_implied - realized_win)^2)
        per fold, i.e. negative Brier score (Brier 1950) of the blend
        weight's implied win-probability for the direction actually
        traded, against whether that trade actually won. Measures
        calibration, not direction -- the direction traded is taken as
        given (no resimulated fills for a different direction).
      - oos_sharpe: per-fold Sharpe of the trade's realized return, sign-
        flipped whenever the candidate weight's implied direction would
        have disagreed with the direction actually traded (same
        direction-flip simplification run_feature_window_backtest uses --
        not a full re-execution/re-fill simulation).
    """
    if features_cfg is None:
        features_cfg = FeatureSettings()

    folds = _make_folds(len(samples), features_cfg.cpcv_n_splits, features_cfg.purge_gap_bars)

    champion_fold_neg_brier: list[float] = []
    challenger_fold_neg_brier: list[float] = []
    champion_fold_sharpes: list[float] = []
    challenger_fold_sharpes: list[float] = []

    for start, end in folds:
        fold_samples = samples[start:end]
        if not fold_samples:
            continue

        champion_briers: list[float] = []
        challenger_briers: list[float] = []
        champion_returns: list[float] = []
        challenger_returns: list[float] = []

        for s in fold_samples:
            realized_win = 1.0 if s.raw_return > 0.0 else 0.0

            champion_p_long = _blended_p_long(s, champion_weight)
            champion_p_win = champion_p_long if s.direction == 1 else (1.0 - champion_p_long)
            champion_briers.append((champion_p_win - realized_win) ** 2)
            champion_direction = 1 if champion_p_long >= 0.5 else 0
            champion_returns.append(
                s.raw_return if champion_direction == s.direction else -s.raw_return
            )

            challenger_p_long = _blended_p_long(s, challenger_weight)
            challenger_p_win = challenger_p_long if s.direction == 1 else (1.0 - challenger_p_long)
            challenger_briers.append((challenger_p_win - realized_win) ** 2)
            challenger_direction = 1 if challenger_p_long >= 0.5 else 0
            challenger_returns.append(
                s.raw_return if challenger_direction == s.direction else -s.raw_return
            )

        champion_fold_neg_brier.append(-statistics.mean(champion_briers))
        challenger_fold_neg_brier.append(-statistics.mean(challenger_briers))
        champion_fold_sharpes.append(_fold_sharpe(champion_returns))
        challenger_fold_sharpes.append(_fold_sharpe(challenger_returns))

    evaluator = ChallengerEvaluator()
    return [
        evaluator.compare_metric(
            "ensemble_prediction_accuracy", champion_fold_neg_brier, challenger_fold_neg_brier
        ),
        evaluator.compare_metric("oos_sharpe", champion_fold_sharpes, challenger_fold_sharpes),
    ]


# ---------------------------------------------------------------------------
# Phase 8 item 3 -- feature-window parameters
# ---------------------------------------------------------------------------

# Each of the five tunable rolling-window parameters maps to the target
# feature column it produces and the pure pipeline function that recomputes
# it from raw OHLCV at an arbitrary window -- confirmed independent of the
# other six base feature columns (src/features/pipeline.py), so swapping
# one column doesn't require recomputing the rest.
_FEATURE_WINDOW_RECOMPUTERS: dict[str, tuple[str, Callable[[pd.DataFrame, int], pd.Series]]] = {
    "vwap_window": (
        COL_VWAP_DEV,
        lambda bars, window: vwap_deviation_zscore(
            bars["high"], bars["low"], bars["close"], bars["volume"], window=window
        ),
    ),
    "ofi_window": (
        COL_OFI,
        lambda bars, window: order_flow_imbalance(bars["close"], bars["volume"], window=window),
    ),
    "atr_window": (
        COL_ATR_MOMENTUM,
        lambda bars, window: atr_momentum(bars["high"], bars["low"], bars["close"], window=window),
    ),
    "sharpe_window": (
        COL_ROLLING_SHARPE,
        lambda bars, window: rolling_sharpe(bars["close"], window=window),
    ),
    "volume_zscore_window": (
        COL_VOLUME_ZSCORE,
        lambda bars, window: volume_zscore(bars["volume"], window=window),
    ),
}


class UnknownFeatureWindowFieldError(ValueError):
    """Raised when run_feature_window_backtest is asked to vary a
    FeatureSettings field with no registered recompute function."""


def _predict_direction_batch(model: XGBClassifier, features: pd.DataFrame) -> np.ndarray:
    """
    Vectorised counterpart of ModelTrainer.predict_direction's schema
    slicing (GAP-015 backward compatibility): use model.n_features_in_ to
    select the correct leading columns rather than assuming a fixed
    7-column schema. `model.predict()` applies the same 0.5 threshold
    trainer.py's own CPCV fold evaluation (_run_cpcv) uses -- not a
    hand-rolled predict_proba threshold.
    """
    n = getattr(model, "n_features_in_", features.shape[1])
    cols = list(features.columns[:n]) if features.shape[1] >= n else list(features.columns)
    arr = features.reindex(columns=cols).to_numpy(dtype=np.float64)
    return model.predict(arr)


def run_feature_window_backtest(
    bars: pd.DataFrame,
    field_name: str,
    champion_window: int,
    challenger_window: int,
    direction_model: XGBClassifier,
    features_cfg: FeatureSettings | None = None,
) -> list[MetricComparison]:
    """
    Recalibrate one of the five rolling-window feature parameters (Phase 8
    item 3) against the currently deployed, FROZEN direction model's
    out-of-sample predictive quality -- this does NOT retrain. It measures
    the frozen model's sensitivity to a perturbed input feature, a
    materially weaker claim than "a model retrained with this window would
    generalize better" (docs/SELF_TUNING_IMPLEMENTATION_PLAN.md Phase 8
    item 3, risk 1) -- acceptable because the ±20% symmetric-bound
    convention (src/tuning/bootstrap.py) keeps challengers close to the
    window the model was actually trained on.

    Builds the baseline 7-column feature matrix once at production
    settings, then recomputes ONLY `field_name`'s column at the champion
    and challenger window sizes and swaps it in. Scores both variants with
    the same frozen model and folds oos_sharpe_and_drawdown's single-bar-
    ahead strategy return -- the same simplification
    ModelTrainer._run_cpcv's own OOS Sharpe already uses (no meta-label
    gate, no triple-barrier P&L simulation).
    """
    if field_name not in _FEATURE_WINDOW_RECOMPUTERS:
        raise UnknownFeatureWindowFieldError(
            f"no recompute function registered for {field_name!r}; "
            f"supported: {sorted(_FEATURE_WINDOW_RECOMPUTERS)}"
        )
    if features_cfg is None:
        features_cfg = FeatureSettings()

    column, recompute = _FEATURE_WINDOW_RECOMPUTERS[field_name]

    baseline = build_feature_matrix(bars, cfg=features_cfg)
    idx = baseline.features.index

    champion_col = recompute(bars, champion_window).reindex(idx)
    challenger_col = recompute(bars, challenger_window).reindex(idx)

    champion_features = baseline.features.copy()
    champion_features[column] = champion_col
    challenger_features = baseline.features.copy()
    challenger_features[column] = challenger_col

    # A larger window has a longer warmup NaN prefix than the baseline's
    # production-window default -- align both variants to rows valid under
    # BOTH before folding, so champion and challenger see identical bars.
    valid = champion_features[column].notna() & challenger_features[column].notna()
    champion_features = champion_features.loc[valid, FEATURE_COLUMNS]
    challenger_features = challenger_features.loc[valid, FEATURE_COLUMNS]
    log_ret = baseline.log_returns.reindex(idx).loc[valid].to_numpy(dtype=np.float64)

    champion_pred = _predict_direction_batch(direction_model, champion_features)
    challenger_pred = _predict_direction_batch(direction_model, challenger_features)

    folds = _make_folds(len(log_ret), features_cfg.cpcv_n_splits, features_cfg.purge_gap_bars)

    champion_fold_sharpes: list[float] = []
    challenger_fold_sharpes: list[float] = []
    champion_wins = 0
    challenger_wins = 0
    total_bars = 0

    for start, end in folds:
        fold_ret = log_ret[start:end]
        if len(fold_ret) == 0:
            continue
        fold_champion_pred = champion_pred[start:end]
        fold_challenger_pred = challenger_pred[start:end]

        champion_sharpe, _ = oos_sharpe_and_drawdown(fold_champion_pred, fold_ret)
        challenger_sharpe, _ = oos_sharpe_and_drawdown(fold_challenger_pred, fold_ret)
        champion_fold_sharpes.append(champion_sharpe)
        challenger_fold_sharpes.append(challenger_sharpe)

        champion_dir = np.where(fold_champion_pred == 1, 1.0, -1.0)
        challenger_dir = np.where(fold_challenger_pred == 1, 1.0, -1.0)
        champion_wins += int(np.sum(champion_dir * fold_ret > 0))
        challenger_wins += int(np.sum(challenger_dir * fold_ret > 0))
        total_bars += len(fold_ret)

    evaluator = ChallengerEvaluator()
    return [
        evaluator.compare_metric("oos_sharpe", champion_fold_sharpes, challenger_fold_sharpes),
        evaluator.compare_proportion(
            "win_rate",
            champion_p=champion_wins / total_bars if total_bars else 0.0,
            champion_n=total_bars,
            challenger_p=challenger_wins / total_bars if total_bars else 0.0,
            challenger_n=total_bars,
        ),
    ]


# ---------------------------------------------------------------------------
# Phase 8 item 4 -- XGBoost hyperparameters
# ---------------------------------------------------------------------------

# Fields whose champion/challenger value must be an integer before being
# passed to XGBoostSettings.model_copy() -- model_copy() does not
# re-validate, so a float slipped into an int field would reach
# XGBClassifier's constructor un-coerced.
XGBOOST_INT_FIELDS: frozenset[str] = frozenset({"n_estimators", "max_depth", "min_child_weight"})


class UnknownXGBHyperparamFieldError(ValueError):
    """Raised when run_xgboost_hyperparam_backtest is asked to vary an
    XGBoostSettings field with no bounds/support registered."""


def run_xgboost_hyperparam_backtest(
    fm: FeatureMatrix,
    field_name: str,
    champion_value: float,
    challenger_value: float,
    base_xgb_cfg: XGBoostSettings,
    symbol: str,
    timeframe: str,
    feature_cfg: FeatureSettings | None = None,
) -> list[MetricComparison]:
    """
    Recalibrate one XGBoost hyperparameter (Phase 8 item 4) via FULL CPCV
    retraining -- unlike every other harness in this module, this is
    genuinely expensive (fits len(folds) x 2 real XGBoost models per
    attempt) and is the reason the design doc ranks this parameter group
    last, only after cheaper parameters establish the harness pattern is
    trustworthy. This function is plain synchronous code, same as every
    other harness here; the CALLER (src/tuning/scheduler.py) is
    responsible for running it off the asyncio event loop (a thread-pool
    executor) so a multi-second-to-minutes retrain doesn't stall the
    live API/trading loop.

    Reuses ModelTrainer.train_direction's existing CPCV harness directly
    rather than reimplementing fold construction / retraining -- this is
    the SAME code path that trains the live-deployed model, so unlike
    items 1-3 (which test a frozen model's sensitivity to a perturbed
    input), this is a faithful "would a model retrained with this
    hyperparameter generalize better" comparison.
    """
    if field_name not in XGBOOST_HYPERPARAM_FIELDS:
        raise UnknownXGBHyperparamFieldError(
            f"no bounds registered for XGBoost field {field_name!r}; "
            f"supported: {sorted(XGBOOST_HYPERPARAM_FIELDS)}"
        )
    if feature_cfg is None:
        feature_cfg = FeatureSettings()

    champion_cfg = base_xgb_cfg.model_copy(update={field_name: champion_value})
    challenger_cfg = base_xgb_cfg.model_copy(update={field_name: challenger_value})

    champion_result = ModelTrainer(
        symbol, timeframe, xgb_cfg=champion_cfg, feature_cfg=feature_cfg
    ).train_direction(fm)
    challenger_result = ModelTrainer(
        symbol, timeframe, xgb_cfg=challenger_cfg, feature_cfg=feature_cfg
    ).train_direction(fm)

    champion_sharpes = [f.sharpe for f in champion_result.fold_metrics]
    challenger_sharpes = [f.sharpe for f in challenger_result.fold_metrics]
    champion_acc = [f.accuracy for f in champion_result.fold_metrics]
    challenger_acc = [f.accuracy for f in challenger_result.fold_metrics]

    evaluator = ChallengerEvaluator()
    return [
        evaluator.compare_metric("oos_sharpe", champion_sharpes, challenger_sharpes),
        evaluator.compare_metric("accuracy", champion_acc, challenger_acc),
    ]
