"""
Model trainer — XGBoost direction classifier + meta-label gate.

Architecture (AFML Ch.3–4, Ch.7):
  1. Direction model   : XGBoostClassifier  → P(long | features, regime)
  2. Meta-label gate   : XGBoostClassifier  → P(bet | direction_prob, features)
  3. Validation        : CPCV (Combinatorial Purged Cross-Validation, AFML Ch.7)
  4. Sample weights    : absolute log-returns (larger moves weighted more)
  5. Purging / embargo : prevent look-ahead leakage in time-series CV

Authority:
  - López de Prado (2018) AFML Ch.3 (labels), Ch.4 (meta-label),
    Ch.7 (CPCV), Ch.10 (sample weights by returns)
  - Chen & Guestrin (2016) XGBoost paper
  - Kelly (1956) — Sharpe as validation metric
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

import joblib
import numpy as np
import pandas as pd
import structlog
from sklearn.metrics import (
    accuracy_score,
    f1_score,
    precision_score,
    recall_score,
)
from xgboost import XGBClassifier

from src.config import XGBoostSettings, FeatureSettings, get_settings
from src.data.storage import ModelMetricsRecord
from src.features.pipeline import (
    FEATURE_COLUMNS,
    FeatureMatrix,
    meta_labels,
)

log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MODEL_DIRECTION: Final[str] = "direction"
MODEL_META_LABEL: Final[str] = "meta_label"

_DIRECTION_FILENAME: Final[str] = "xgb_direction_{symbol}_{timeframe}.joblib"
_META_FILENAME: Final[str] = "xgb_meta_{symbol}_{timeframe}.joblib"


# ---------------------------------------------------------------------------
# CPCV implementation — AFML Ch.7
# ---------------------------------------------------------------------------


@dataclass
class CPCVFold:
    """Single CPCV fold: train indices, test indices, purge gap applied."""

    train_idx: np.ndarray
    test_idx: np.ndarray
    fold_id: int


def build_cpcv_folds(
    n_samples: int,
    n_splits: int,
    n_test_splits: int,
    purge_gap: int,
    embargo_pct: float,
) -> list[CPCVFold]:
    """
    Combinatorial Purged Cross-Validation fold generator.

    AFML Ch.7 — generates C(n_splits, n_test_splits) folds by treating
    every combination of n_test_splits groups as the test set and the
    remaining groups (minus purge gap and embargo) as training.

    Purging: removes training samples whose label spans overlap with the
    test window (prevents look-ahead via the triple-barrier horizon).

    Embargo: removes the first `embargo_pct * n_samples` samples immediately
    after each test block from training.

    Parameters
    ----------
    n_samples     : total number of labelled samples
    n_splits      : number of time groups (k in CPCV notation)
    n_test_splits : number of test groups per fold (t in CPCV)
    purge_gap     : bars to drop from training immediately before test
    embargo_pct   : fraction of dataset to embargo after each test block

    Returns
    -------
    List of CPCVFold objects.
    """
    from itertools import combinations

    indices = np.arange(n_samples)
    group_size = n_samples // n_splits
    # Build group boundaries
    groups: list[np.ndarray] = []
    for i in range(n_splits):
        start = i * group_size
        end = start + group_size if i < n_splits - 1 else n_samples
        groups.append(indices[start:end])

    embargo_size = max(1, int(n_samples * embargo_pct))

    folds: list[CPCVFold] = []
    for fold_id, test_group_ids in enumerate(combinations(range(n_splits), n_test_splits)):
        test_group_set = set(test_group_ids)
        test_idx = np.concatenate([groups[g] for g in sorted(test_group_set)])
        test_start = int(test_idx.min())
        test_end = int(test_idx.max())

        # Training: all groups not in test set, with purge + embargo
        train_parts: list[np.ndarray] = []
        for g in range(n_splits):
            if g in test_group_set:
                continue
            group_arr = groups[g]
            group_end = int(group_arr.max())
            group_start = int(group_arr.min())

            # Purge: remove samples within purge_gap bars before test window
            if group_end >= test_start - purge_gap and group_end < test_start:
                group_arr = group_arr[group_arr < test_start - purge_gap]
            # Embargo: remove samples within embargo_size bars after test window
            if group_start <= test_end + embargo_size and group_start > test_end:
                group_arr = group_arr[group_arr > test_end + embargo_size]

            if len(group_arr) > 0:
                train_parts.append(group_arr)

        if not train_parts:
            continue
        train_idx = np.concatenate(train_parts)

        if len(train_idx) < 30 or len(test_idx) < 10:
            continue

        folds.append(CPCVFold(train_idx=train_idx, test_idx=test_idx, fold_id=fold_id))

    return folds


# ---------------------------------------------------------------------------
# Sample weights — AFML Ch.10
# ---------------------------------------------------------------------------


def compute_sample_weights(log_returns: pd.Series) -> np.ndarray:
    """
    Weight each sample by the absolute magnitude of its log return.

    AFML Ch.10 — samples with larger price moves carry more information
    and should receive higher weight.  Weights are normalised to sum to 1.

    Zero-return rows receive a small floor weight (1e-4) to avoid
    XGBoost ignoring them entirely.
    """
    abs_ret = log_returns.abs().to_numpy(dtype=np.float64)
    floor = np.full_like(abs_ret, 1e-4)
    weights = np.maximum(abs_ret, floor)
    weights = weights / weights.sum()
    return weights


# ---------------------------------------------------------------------------
# OOS metrics — computed on each CPCV fold
# ---------------------------------------------------------------------------


@dataclass
class FoldMetrics:
    """Metrics for a single CPCV test fold."""

    fold_id: int
    accuracy: float
    precision: float
    recall: float
    f1: float
    n_test: int
    sharpe: float  # Sharpe of predicted long/short returns on test fold


@dataclass
class TrainingResult:
    """
    Full training outcome for one model.

    model         : fitted XGBClassifier
    oos_sharpe    : mean OOS Sharpe across CPCV folds
    max_drawdown  : maximum drawdown of OOS equity curve (%)
    n_trades      : total OOS predictions used
    accuracy      : mean OOS accuracy
    precision     : mean OOS precision
    recall        : mean OOS recall
    f1            : mean OOS F1
    live_gate_pass: True if all live-gate thresholds are met
    fold_metrics  : per-fold breakdown
    elapsed_s     : wall-clock training time
    """

    model: XGBClassifier
    oos_sharpe: float
    max_drawdown: float
    n_trades: int
    accuracy: float
    precision: float
    recall: float
    f1: float
    live_gate_pass: bool
    fold_metrics: list[FoldMetrics] = field(default_factory=list)
    elapsed_s: float = 0.0

    def to_metrics_record(
        self,
        model_name: str,
        timeframe: str,
        version: str,
    ) -> ModelMetricsRecord:
        return ModelMetricsRecord(
            model_name=model_name,
            timeframe=timeframe,
            version=version,
            oos_sharpe=self.oos_sharpe,
            max_drawdown=self.max_drawdown,
            n_trades=self.n_trades,
            accuracy=self.accuracy,
            precision_score=self.precision,
            recall_score=self.recall,
            f1_score=self.f1,
            live_gate_pass=self.live_gate_pass,
        )


# ---------------------------------------------------------------------------
# OOS equity curve → Sharpe + max drawdown
# ---------------------------------------------------------------------------


def _oos_sharpe_and_drawdown(
    y_pred: np.ndarray,
    log_returns: np.ndarray,
) -> tuple[float, float]:
    """
    Compute OOS Sharpe and maximum drawdown from predicted directions.

    Strategy return per bar:
      +log_return if predicted long (1)
      -log_return if predicted short (0)

    Returns (annualised_sharpe, max_drawdown_pct).
    Annualisation uses sqrt(252 * bars_per_day); for simplicity we use
    sqrt(n_test) as a proportional scaler across folds.
    """
    direction = np.where(y_pred == 1, 1.0, -1.0)
    strat_ret = direction * log_returns

    mu = float(np.mean(strat_ret))
    sigma = float(np.std(strat_ret, ddof=1))
    if sigma == 0.0:
        sharpe = 0.0
    else:
        sharpe = (mu / sigma) * np.sqrt(len(strat_ret))

    # Max drawdown of cumulative equity curve
    cum = np.cumprod(1.0 + strat_ret)
    running_max = np.maximum.accumulate(cum)
    dd = (cum - running_max) / running_max
    max_dd = float(abs(dd.min()) * 100.0)

    return sharpe, max_dd


# ---------------------------------------------------------------------------
# XGBoost builder
# ---------------------------------------------------------------------------


def _build_xgb(cfg: XGBoostSettings, scale_pos_weight: float = 1.0) -> XGBClassifier:
    """Construct an XGBClassifier from settings."""
    return XGBClassifier(
        n_estimators=cfg.n_estimators,
        max_depth=cfg.max_depth,
        learning_rate=cfg.learning_rate,
        subsample=cfg.subsample,
        colsample_bytree=cfg.colsample_bytree,
        min_child_weight=cfg.min_child_weight,
        reg_alpha=cfg.reg_alpha,
        reg_lambda=cfg.reg_lambda,
        use_label_encoder=False,
        eval_metric=cfg.eval_metric,
        tree_method=cfg.tree_method,
        device=cfg.device,
        random_state=cfg.random_state,
        early_stopping_rounds=cfg.early_stopping_rounds,
        scale_pos_weight=scale_pos_weight,
        verbosity=0,
    )


# ---------------------------------------------------------------------------
# ModelTrainer
# ---------------------------------------------------------------------------


class ModelTrainer:
    """
    Trains the XGBoost direction and meta-label models with CPCV validation.

    Usage::

        trainer = ModelTrainer('BTC/USDT', '15m')
        dir_result  = trainer.train_direction(feature_matrix)
        meta_result = trainer.train_meta_label(feature_matrix, dir_result.model)
        trainer.save(dir_result.model, meta_result.model, model_dir)
    """

    def __init__(
        self,
        symbol: str,
        timeframe: str,
        xgb_cfg: XGBoostSettings | None = None,
        feature_cfg: FeatureSettings | None = None,
    ) -> None:
        cfg = get_settings()
        self._symbol = symbol
        self._timeframe = timeframe
        self._xgb_cfg: XGBoostSettings = xgb_cfg or cfg.xgboost
        self._feature_cfg: FeatureSettings = feature_cfg or cfg.features
        self._risk_cfg = cfg.risk
        self._log = log.bind(
            component="trainer",
            symbol=symbol,
            timeframe=timeframe,
        )

    # ------------------------------------------------------------------
    # Direction model
    # ------------------------------------------------------------------

    def train_direction(self, fm: FeatureMatrix) -> TrainingResult:
        """
        Train the primary XGBoost direction classifier.

        Labels: 1 = long, 0 = short (triple-barrier outcomes, time-exits excluded).
        CPCV validation computes OOS Sharpe and max drawdown.
        Final model is re-fit on the full dataset.

        Parameters
        ----------
        fm : FeatureMatrix from pipeline.build_feature_matrix()

        Returns
        -------
        TrainingResult with fitted model and OOS metrics.
        """
        X = fm.features[FEATURE_COLUMNS].to_numpy(dtype=np.float64)
        y = fm.labels.to_numpy(dtype=np.int8)
        log_ret = fm.log_returns.to_numpy(dtype=np.float64)
        weights = compute_sample_weights(fm.log_returns)

        self._log.info(
            "trainer.direction.start",
            n_samples=len(X),
            n_long=int((y == 1).sum()),
            n_short=int((y == 0).sum()),
        )

        cpcv_cfg = self._feature_cfg
        folds = build_cpcv_folds(
            n_samples=len(X),
            n_splits=cpcv_cfg.cpcv_n_splits,
            n_test_splits=cpcv_cfg.cpcv_n_test_splits,
            purge_gap=cpcv_cfg.purge_gap_bars,
            embargo_pct=cpcv_cfg.embargo_pct,
        )

        t0 = time.perf_counter()
        fold_metrics, all_oos_ret, all_oos_pred = self._run_cpcv(
            X, y, log_ret, weights, folds, model_name=MODEL_DIRECTION
        )
        elapsed = time.perf_counter() - t0

        oos_sharpe, max_dd = _oos_sharpe_and_drawdown(
            np.concatenate(all_oos_pred), np.concatenate(all_oos_ret)
        )

        # Class imbalance correction
        pos_weight = float((y == 0).sum()) / max(float((y == 1).sum()), 1.0)

        # Final model on full dataset
        final_model = _build_xgb(self._xgb_cfg, scale_pos_weight=pos_weight)
        # Use last 15% as internal eval set for early stopping
        split = int(len(X) * 0.85)
        eval_set = [(X[split:], y[split:])]
        final_model.fit(
            X[:split],
            y[:split],
            sample_weight=weights[:split],
            eval_set=eval_set,
            verbose=False,
        )

        mean_acc = float(np.mean([f.accuracy for f in fold_metrics]))
        mean_prec = float(np.mean([f.precision for f in fold_metrics]))
        mean_rec = float(np.mean([f.recall for f in fold_metrics]))
        mean_f1 = float(np.mean([f.f1 for f in fold_metrics]))
        n_trades = sum(f.n_test for f in fold_metrics)

        live_gate = self._check_live_gate(oos_sharpe, max_dd, n_trades)

        result = TrainingResult(
            model=final_model,
            oos_sharpe=round(oos_sharpe, 4),
            max_drawdown=round(max_dd, 4),
            n_trades=n_trades,
            accuracy=round(mean_acc, 4),
            precision=round(mean_prec, 4),
            recall=round(mean_rec, 4),
            f1=round(mean_f1, 4),
            live_gate_pass=live_gate,
            fold_metrics=fold_metrics,
            elapsed_s=round(elapsed, 2),
        )

        self._log.info(
            "trainer.direction.done",
            oos_sharpe=result.oos_sharpe,
            max_drawdown=result.max_drawdown,
            accuracy=result.accuracy,
            f1=result.f1,
            live_gate_pass=live_gate,
            elapsed_s=result.elapsed_s,
        )
        return result

    # ------------------------------------------------------------------
    # Meta-label model
    # ------------------------------------------------------------------

    def train_meta_label(
        self,
        fm: FeatureMatrix,
        direction_model: XGBClassifier,
    ) -> TrainingResult:
        """
        Train the XGBoost meta-label gate.

        The meta-label model learns when to bet on the direction model's
        output (AFML Ch.4).

        Feature matrix for meta-label:
          - All 7 primary features
          - direction_model predicted probability of long (P_long)
          - absolute P_long - 0.5  (confidence proxy)

        Labels: 1 = direction model would be correct, 0 = incorrect.

        Parameters
        ----------
        fm               : FeatureMatrix (same as direction training)
        direction_model  : fitted direction XGBClassifier

        Returns
        -------
        TrainingResult for the meta-label model.
        """
        X_dir = fm.features[FEATURE_COLUMNS].to_numpy(dtype=np.float64)
        dir_probs = direction_model.predict_proba(X_dir)[:, 1]  # P(long)
        dir_preds = (dir_probs >= 0.5).astype(np.int8)

        # Meta-labels: 1 when direction model agrees with realized outcome
        fm.labels.to_numpy(dtype=np.int8)
        meta_y = meta_labels(
            pd.Series(dir_preds, index=fm.labels.index, dtype=np.int8),
            fm.labels,
        ).to_numpy(dtype=np.int8)

        # Extended feature matrix: primary features + direction signal features
        confidence = np.abs(dir_probs - 0.5).reshape(-1, 1)
        p_long = dir_probs.reshape(-1, 1)
        X_meta = np.hstack([X_dir, p_long, confidence])

        log_ret = fm.log_returns.to_numpy(dtype=np.float64)
        weights = compute_sample_weights(fm.log_returns)

        self._log.info(
            "trainer.meta.start",
            n_samples=len(X_meta),
            n_bet=int((meta_y == 1).sum()),
            n_skip=int((meta_y == 0).sum()),
        )

        cpcv_cfg = self._feature_cfg
        folds = build_cpcv_folds(
            n_samples=len(X_meta),
            n_splits=cpcv_cfg.cpcv_n_splits,
            n_test_splits=cpcv_cfg.cpcv_n_test_splits,
            purge_gap=cpcv_cfg.purge_gap_bars,
            embargo_pct=cpcv_cfg.embargo_pct,
        )

        t0 = time.perf_counter()
        fold_metrics, all_oos_ret, all_oos_pred = self._run_cpcv(
            X_meta, meta_y, log_ret, weights, folds, model_name=MODEL_META_LABEL
        )
        elapsed = time.perf_counter() - t0

        oos_sharpe, max_dd = _oos_sharpe_and_drawdown(
            np.concatenate(all_oos_pred), np.concatenate(all_oos_ret)
        )

        pos_weight = float((meta_y == 0).sum()) / max(float((meta_y == 1).sum()), 1.0)
        final_model = _build_xgb(self._xgb_cfg, scale_pos_weight=pos_weight)
        split = int(len(X_meta) * 0.85)
        eval_set = [(X_meta[split:], meta_y[split:])]
        final_model.fit(
            X_meta[:split],
            meta_y[:split],
            sample_weight=weights[:split],
            eval_set=eval_set,
            verbose=False,
        )

        mean_acc = float(np.mean([f.accuracy for f in fold_metrics]))
        mean_prec = float(np.mean([f.precision for f in fold_metrics]))
        mean_rec = float(np.mean([f.recall for f in fold_metrics]))
        mean_f1 = float(np.mean([f.f1 for f in fold_metrics]))
        n_trades = sum(f.n_test for f in fold_metrics)

        live_gate = self._check_live_gate(oos_sharpe, max_dd, n_trades)

        result = TrainingResult(
            model=final_model,
            oos_sharpe=round(oos_sharpe, 4),
            max_drawdown=round(max_dd, 4),
            n_trades=n_trades,
            accuracy=round(mean_acc, 4),
            precision=round(mean_prec, 4),
            recall=round(mean_rec, 4),
            f1=round(mean_f1, 4),
            live_gate_pass=live_gate,
            fold_metrics=fold_metrics,
            elapsed_s=round(elapsed, 2),
        )

        self._log.info(
            "trainer.meta.done",
            oos_sharpe=result.oos_sharpe,
            max_drawdown=result.max_drawdown,
            accuracy=result.accuracy,
            f1=result.f1,
            live_gate_pass=live_gate,
            elapsed_s=result.elapsed_s,
        )
        return result

    # ------------------------------------------------------------------
    # Inference helpers — used by signal engine
    # ------------------------------------------------------------------

    def predict_direction(
        self,
        model: XGBClassifier,
        feature_vec: pd.Series,
    ) -> tuple[int, float]:
        """
        Predict direction and confidence for a single bar feature vector.

        Parameters
        ----------
        model       : fitted direction XGBClassifier
        feature_vec : pd.Series indexed by FEATURE_COLUMNS

        Returns
        -------
        (direction, p_long) where direction is 1 (long) or 0 (short),
        and p_long is the probability of a long outcome.
        """
        X = feature_vec[FEATURE_COLUMNS].to_numpy(dtype=np.float64).reshape(1, -1)
        p_long = float(model.predict_proba(X)[0, 1])
        direction = 1 if p_long >= 0.5 else 0
        return direction, p_long

    def predict_meta(
        self,
        meta_model: XGBClassifier,
        feature_vec: pd.Series,
        p_long: float,
    ) -> tuple[int, float]:
        """
        Predict meta-label (bet=1 / skip=0) for a single bar.

        Parameters
        ----------
        meta_model  : fitted meta-label XGBClassifier
        feature_vec : pd.Series indexed by FEATURE_COLUMNS
        p_long      : direction model's P(long) for this bar

        Returns
        -------
        (meta_label, p_bet) where meta_label is 1 (bet) or 0 (skip),
        and p_bet is the probability of betting.
        """
        base = feature_vec[FEATURE_COLUMNS].to_numpy(dtype=np.float64)
        confidence = abs(p_long - 0.5)
        X = np.append(base, [p_long, confidence]).reshape(1, -1)
        p_bet = float(meta_model.predict_proba(X)[0, 1])
        meta = 1 if p_bet >= 0.5 else 0
        return meta, p_bet

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def save(
        self,
        direction_model: XGBClassifier,
        meta_model: XGBClassifier,
        model_dir: str | Path,
        version: str,
    ) -> tuple[Path, Path]:
        """
        Serialize both models to disk.

        Returns (direction_path, meta_path).
        """
        model_dir = Path(model_dir)
        model_dir.mkdir(parents=True, exist_ok=True)

        sym = self._symbol.replace("/", "_")
        tf = self._timeframe

        dir_path = model_dir / _DIRECTION_FILENAME.format(symbol=sym, timeframe=tf)
        meta_path = model_dir / _META_FILENAME.format(symbol=sym, timeframe=tf)

        joblib.dump(
            {"model": direction_model, "version": version, "symbol": self._symbol, "timeframe": tf},
            dir_path,
            compress=3,
        )
        joblib.dump(
            {"model": meta_model, "version": version, "symbol": self._symbol, "timeframe": tf},
            meta_path,
            compress=3,
        )

        self._log.info(
            "trainer.saved",
            direction=str(dir_path),
            meta=str(meta_path),
            version=version,
        )
        return dir_path, meta_path

    @staticmethod
    def load_direction(
        model_dir: str | Path,
        symbol: str,
        timeframe: str,
    ) -> XGBClassifier:
        """Load a previously saved direction model."""
        path = Path(model_dir) / _DIRECTION_FILENAME.format(
            symbol=symbol.replace("/", "_"), timeframe=timeframe
        )
        if not path.exists():
            raise FileNotFoundError(f"No direction model at {path}")
        return joblib.load(path)["model"]

    @staticmethod
    def load_meta(
        model_dir: str | Path,
        symbol: str,
        timeframe: str,
    ) -> XGBClassifier:
        """Load a previously saved meta-label model."""
        path = Path(model_dir) / _META_FILENAME.format(
            symbol=symbol.replace("/", "_"), timeframe=timeframe
        )
        if not path.exists():
            raise FileNotFoundError(f"No meta-label model at {path}")
        return joblib.load(path)["model"]

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _run_cpcv(
        self,
        X: np.ndarray,
        y: np.ndarray,
        log_ret: np.ndarray,
        weights: np.ndarray,
        folds: list[CPCVFold],
        model_name: str,
    ) -> tuple[list[FoldMetrics], list[np.ndarray], list[np.ndarray]]:
        """
        Execute CPCV cross-validation.

        Returns (fold_metrics, oos_returns_per_fold, oos_preds_per_fold).
        """
        fold_metrics: list[FoldMetrics] = []
        all_oos_ret: list[np.ndarray] = []
        all_oos_pred: list[np.ndarray] = []

        for fold in folds:
            tr = fold.train_idx
            te = fold.test_idx

            # Guard: both classes present in train
            if len(np.unique(y[tr])) < 2:
                self._log.debug(
                    "trainer.cpcv.skip_fold",
                    fold_id=fold.fold_id,
                    reason="single_class_in_train",
                )
                continue

            pos_weight = float((y[tr] == 0).sum()) / max(float((y[tr] == 1).sum()), 1.0)
            model = _build_xgb(self._xgb_cfg, scale_pos_weight=pos_weight)

            # Internal eval for early stopping: last 10% of train
            inner_split = int(len(tr) * 0.90)
            tr_inner = tr[:inner_split]
            val_inner = tr[inner_split:]

            if len(val_inner) < 5 or len(np.unique(y[val_inner])) < 2:
                # Not enough for eval set — disable early stopping for this fold
                model.set_params(early_stopping_rounds=None)
                model.fit(
                    X[tr],
                    y[tr],
                    sample_weight=weights[tr],
                    verbose=False,
                )
            else:
                model.fit(
                    X[tr_inner],
                    y[tr_inner],
                    sample_weight=weights[tr_inner],
                    eval_set=[(X[val_inner], y[val_inner])],
                    verbose=False,
                )

            y_pred = model.predict(X[te])
            y_true = y[te]

            acc = float(accuracy_score(y_true, y_pred))
            prec = float(precision_score(y_true, y_pred, zero_division=0))
            rec = float(recall_score(y_true, y_pred, zero_division=0))
            f1 = float(f1_score(y_true, y_pred, zero_division=0))
            fold_sharpe, _ = _oos_sharpe_and_drawdown(y_pred, log_ret[te])

            fold_metrics.append(
                FoldMetrics(
                    fold_id=fold.fold_id,
                    accuracy=acc,
                    precision=prec,
                    recall=rec,
                    f1=f1,
                    n_test=len(te),
                    sharpe=fold_sharpe,
                )
            )
            all_oos_ret.append(log_ret[te])
            all_oos_pred.append(y_pred)

            self._log.debug(
                f"trainer.{model_name}.fold",
                fold_id=fold.fold_id,
                n_train=len(tr),
                n_test=len(te),
                acc=round(acc, 3),
                f1=round(f1, 3),
                sharpe=round(fold_sharpe, 3),
            )

        return fold_metrics, all_oos_ret, all_oos_pred

    def _check_live_gate(
        self,
        oos_sharpe: float,
        max_drawdown: float,
        n_trades: int,
    ) -> bool:
        """
        Evaluate live-gate thresholds.

        All three must pass simultaneously (AFML Ch.7 OOS validation):
          - OOS Sharpe > oos_sharpe_threshold (default 1.5)
          - max drawdown < max_drawdown_threshold (default 15%)
          - n_trades >= min_trades_live_gate (default 500)
        """
        r = self._risk_cfg
        passes = bool(
            oos_sharpe > r.oos_sharpe_threshold
            and max_drawdown < r.max_drawdown_threshold
            and n_trades >= r.min_trades_live_gate
        )
        self._log.info(
            "trainer.live_gate",
            oos_sharpe=round(oos_sharpe, 4),
            max_drawdown=round(max_drawdown, 4),
            n_trades=n_trades,
            threshold_sharpe=r.oos_sharpe_threshold,
            threshold_dd=r.max_drawdown_threshold,
            threshold_trades=r.min_trades_live_gate,
            passes=passes,
        )
        return passes
