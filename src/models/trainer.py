"""
Model trainer — XGBoost direction classifier + meta-label gate.

Architecture (AFML Ch.3-4, Ch.7):
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

import hashlib
import hmac  # SCAN2-008: was inline-imported inside hmac_compare(); moved to module level
import io
import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Final

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

from src.config import FeatureSettings, XGBoostSettings, get_settings
from src.data.storage import ModelMetricsRecord
from src.features.pipeline import (
    BASE_FEATURE_COLUMNS,
    FeatureMatrix,
    get_active_feature_columns,
    meta_labels,
)
from src.tuning.live_overrides import effective_feature_settings, effective_xgboost_settings


if TYPE_CHECKING:
    from src.intelligence.ensemble_predictor import EnsemblePredictor


log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MODEL_DIRECTION: Final[str] = "direction"
MODEL_META_LABEL: Final[str] = "meta_label"

_DIRECTION_FILENAME: Final[str] = "xgb_direction_{symbol}_{timeframe}.joblib"
_META_FILENAME: Final[str] = "xgb_meta_{symbol}_{timeframe}.joblib"
_MANIFEST_SUFFIX: Final[str] = ".sha256"


_TIMEFRAME_SAFE_RE: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9_-]+$")


def _validate_timeframe(timeframe: str) -> None:
    """UI-014: `timeframe` is interpolated directly into a model filename
    (unlike `symbol`, which gets `.replace('/', '_')`) with no sanitization.
    Callers throughout this codebase use `timeframe` as a loosely-typed
    free-form string (not strictly the three-value Timeframe enum -- e.g.
    "1h" appears in several test fixtures/storage call sites), so this
    intentionally does NOT enforce the Timeframe enum allowlist; it only
    rejects path-traversal-shaped input ('/', '\\', '..', empty) before it
    reaches a path join, as defense-in-depth against a caller that didn't
    validate its own input."""
    if not timeframe or not _TIMEFRAME_SAFE_RE.match(timeframe):
        raise ValueError(
            f"Invalid timeframe {timeframe!r}: must be a non-empty string of "
            "letters, digits, '_', or '-' only."
        )


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    """
    Write bytes to `path` atomically via temp-file + os.replace.

    VF-019: joblib.dump() / write_text() previously wrote directly to the
    target path. A concurrent reader (e.g. signal_engine.swap_models()
    hot-loading a new model while a retrain job is mid-save) could observe
    a partially-written file — truncated pickle, or a manifest whose JSON
    is half-flushed. os.replace() is atomic on the same filesystem on both
    POSIX and Windows, so readers only ever see the old complete file or
    the new complete file, never an intermediate state.
    """
    tmp_path = path.with_name(f"{path.name}.tmp{os.getpid()}")
    tmp_path.write_bytes(data)
    os.replace(tmp_path, path)


def _write_manifest(path: Path, data: bytes | None = None) -> None:
    """
    Write a SHA-256 manifest file alongside a model file.

    `data` should be the exact bytes already written to `path` so the file
    is not re-read from disk; falls back to reading `path` if omitted.
    """
    digest = hashlib.sha256(data if data is not None else path.read_bytes()).hexdigest()
    manifest = {"file": path.name, "sha256": digest}
    manifest_path = path.with_suffix(_MANIFEST_SUFFIX)
    _atomic_write_bytes(manifest_path, json.dumps(manifest).encode("utf-8"))


def _verify_manifest(path: Path) -> bytes:
    """
    Verify a model file against its SHA-256 manifest and return its bytes.

    VF-020: the file is read exactly once and the same byte buffer is used
    for both hash verification and (by the caller) deserialization. The
    previous implementation verified the hash via `path.read_bytes()` and
    then had the caller separately re-read the file via `joblib.load(path)`
    — a TOCTOU window in which anything with write access to the model
    directory could swap the file between the two reads and bypass the
    integrity check entirely. Returning the verified bytes here closes
    that window: whatever was hashed is exactly what gets deserialized.

    Raises RuntimeError if the manifest is missing or the hash mismatches,
    preventing tampered or poisoned model files from being loaded.
    """
    manifest_path = path.with_suffix(_MANIFEST_SUFFIX)
    if not manifest_path.exists():
        raise RuntimeError(
            f"Model manifest missing for {path}. " "Re-train the model to regenerate the manifest."
        )
    manifest = json.loads(manifest_path.read_text())
    expected = manifest.get("sha256", "")
    data = path.read_bytes()
    actual = hashlib.sha256(data).hexdigest()
    if not hmac_compare(actual, expected):
        raise RuntimeError(
            f"Model file integrity check FAILED for {path}. "
            "The file may have been tampered with. Re-train to replace it."
        )
    return data


def hmac_compare(a: str, b: str) -> bool:
    """Constant-time string comparison (prevents timing oracle on hash compare).
    SCAN2-008: import moved to module level.
    """
    return hmac.compare_digest(a.encode(), b.encode())


# ---------------------------------------------------------------------------
# CPCV implementation — AFML Ch.7
# ---------------------------------------------------------------------------


@dataclass
class CPCVFold:
    """Single CPCV fold: train indices, test indices, purge gap applied."""

    train_idx: np.ndarray
    test_idx: np.ndarray
    fold_id: int


def _build_groups(n_samples: int, n_splits: int) -> list[np.ndarray]:
    """Partition sample indices into n_splits contiguous groups."""
    indices = np.arange(n_samples)
    group_size = n_samples // n_splits
    groups: list[np.ndarray] = []
    for i in range(n_splits):
        start = i * group_size
        end = start + group_size if i < n_splits - 1 else n_samples
        groups.append(indices[start:end])
    return groups


def _apply_purge_embargo(
    group_arr: np.ndarray,
    group_start: int,
    group_end: int,
    test_start: int,
    test_end: int,
    purge_gap: int,
    embargo_size: int,
) -> np.ndarray:
    """Remove samples that fall within the purge or embargo zones."""
    if group_end >= test_start - purge_gap and group_end < test_start:
        group_arr = group_arr[group_arr < test_start - purge_gap]
    if group_start <= test_end + embargo_size and group_start > test_end:
        group_arr = group_arr[group_arr > test_end + embargo_size]
    return group_arr


def _build_train_indices(
    groups: list[np.ndarray],
    n_splits: int,
    test_group_set: set[int],
    test_start: int,
    test_end: int,
    purge_gap: int,
    embargo_size: int,
) -> np.ndarray | None:
    """Collect train indices across all non-test groups with purge/embargo applied."""
    train_parts: list[np.ndarray] = []
    for g in range(n_splits):
        if g in test_group_set:
            continue
        group_arr = groups[g]
        group_arr = _apply_purge_embargo(
            group_arr,
            int(group_arr.min()),
            int(group_arr.max()),
            test_start,
            test_end,
            purge_gap,
            embargo_size,
        )
        if len(group_arr) > 0:
            train_parts.append(group_arr)
    return np.concatenate(train_parts) if train_parts else None


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

    # VF-023: with n_splits > n_samples (or n_splits <= 0), `_build_groups`
    # produces empty groups (group_size = n_samples // n_splits == 0), and
    # `_build_train_indices` calls `.min()`/`.max()` on those empty arrays
    # before its length check — raising an unhandled
    # "zero-size array to reduction operation minimum" ValueError deep in
    # the call stack. A small/new-listing symbol with too few bootstrapped
    # bars would crash the retrain job with a cryptic error instead of a
    # clear one. Fail fast here with an actionable message.
    if n_splits < 2:
        raise ValueError(f"build_cpcv_folds: n_splits must be >= 2, got {n_splits}")
    if n_samples < n_splits:
        raise ValueError(
            f"build_cpcv_folds: n_samples={n_samples} < n_splits={n_splits} — "
            "every CPCV group must contain at least one sample. Provide more "
            "historical bars or reduce cpcv_n_splits in FeatureSettings."
        )

    groups = _build_groups(n_samples, n_splits)
    embargo_size = max(1, int(n_samples * embargo_pct))
    folds: list[CPCVFold] = []

    for fold_id, test_group_ids in enumerate(combinations(range(n_splits), n_test_splits)):
        test_group_set = set(test_group_ids)
        test_idx = np.concatenate([groups[g] for g in sorted(test_group_set)])
        test_start = int(test_idx.min())
        test_end = int(test_idx.max())

        train_idx = _build_train_indices(
            groups,
            n_splits,
            test_group_set,
            test_start,
            test_end,
            purge_gap,
            embargo_size,
        )
        if train_idx is None or len(train_idx) < 30 or len(test_idx) < 10:
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


def oos_sharpe_and_drawdown(
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
    sigma = float(np.std(strat_ret, ddof=1)) if len(strat_ret) > 1 else 0.0
    # VF-022: np.std on a degenerate fold (e.g. ddof=1 with len<=1, or a
    # NaN strat_ret from upstream) can return NaN. `abs(nan) < 1e-10` is
    # False (NaN comparisons never short-circuit true), so the original
    # guard let NaN fall through to the else-branch and silently propagate
    # into oos_sharpe — a metric that gets persisted via ModelMetricsRecord
    # and exposed over the API. NaN is not valid JSON and would break
    # strict downstream parsers; explicitly check finiteness first.
    if not np.isfinite(sigma) or abs(sigma) < 1e-10:
        sharpe = 0.0
    else:
        sharpe = (mu / sigma) * np.sqrt(len(strat_ret))
    if not np.isfinite(sharpe):
        sharpe = 0.0

    # Max drawdown of cumulative equity curve
    cum = np.cumprod(1.0 + strat_ret)
    running_max = np.maximum.accumulate(cum)
    # Guard against running_max <= 0 (e.g. a single-bar strat_ret <= -100%,
    # which is possible if upstream log_returns are corrupted/extreme):
    # dividing by zero/negative would otherwise yield inf/nan that silently
    # poisons the persisted live-gate drawdown metric.
    safe_running_max = np.where(running_max > 1e-12, running_max, 1e-12)
    dd = (cum - running_max) / safe_running_max
    dd = dd[np.isfinite(dd)]
    max_dd = float(abs(dd.min()) * 100.0) if dd.size else 100.0

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

        trainer = ModelTrainer("BTC/USDT", "15m")
        dir_result = trainer.train_direction(feature_matrix)
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
        _validate_timeframe(timeframe)
        cfg = get_settings()
        self._symbol = symbol
        self._timeframe = timeframe
        self._xgb_cfg: XGBoostSettings = xgb_cfg or effective_xgboost_settings(cfg.xgboost)
        self._feature_cfg: FeatureSettings = feature_cfg or effective_feature_settings(cfg.features)
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
        # GAP-015 Step 5: use coverage-gated feature set.
        # If an intelligence_coverage dict is attached to fm, resolve the
        # active column list; otherwise fall back to 7 base features.
        _active_cols = get_active_feature_columns(
            coverage=getattr(fm, "intelligence_coverage", None),
            min_coverage=0.6,
        )
        # Ensure all active columns are present in fm.features (guard against
        # stale FeatureMatrix built before backfill).
        _present_cols = [c for c in _active_cols if c in fm.features.columns]
        _missing = [c for c in _active_cols if c not in fm.features.columns]
        if _missing:
            self._log.warning(
                "trainer.direction.missing_columns",
                missing=_missing,
                reason="column in active set but absent from FeatureMatrix — dropping",
            )
        _active_cols = _present_cols

        X = fm.features[_active_cols].to_numpy(dtype=np.float64)
        y = fm.labels.to_numpy(dtype=np.int8)
        log_ret = fm.log_returns.to_numpy(dtype=np.float64)
        weights = compute_sample_weights(fm.log_returns)

        self._log.info(
            "trainer.direction.start",
            n_samples=len(X),
            n_long=int((y == 1).sum()),
            n_short=int((y == 0).sum()),
            n_features=len(_active_cols),
            feature_mode=f"{len(_active_cols)}" if len(_active_cols) > 7 else "9",
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

        # M-07: guard against empty fold_metrics — np.mean([]) = nan → TypeError on round()
        if not fold_metrics:
            self._log.warning(
                "trainer.cpcv.no_valid_folds",
                n_samples=len(X),
                action="returning failed TrainingResult — dataset too small for CPCV",
            )
            # VF-021: no eval_set available here — early_stopping_rounds is always
            # configured by _build_xgb, and XGBoost requires an eval_set whenever
            # early stopping is set, so fit() would raise. Disable it explicitly.
            final_model = _build_xgb(self._xgb_cfg, scale_pos_weight=1.0)
            final_model.set_params(early_stopping_rounds=None)
            final_model.fit(X, y, sample_weight=weights, verbose=False)
            return TrainingResult(
                model=final_model,
                oos_sharpe=0.0,
                max_drawdown=100.0,
                n_trades=0,
                accuracy=0.0,
                precision=0.0,
                recall=0.0,
                f1=0.0,
                live_gate_pass=False,
                elapsed_s=round(elapsed, 2),
            )

        oos_sharpe, max_dd = oos_sharpe_and_drawdown(
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

        # Patch D: push training feature distribution to drift monitor so
        # FeatureDriftMonitor has a baseline to compare live values against.
        # Also update the degradation tracker with this run's OOS accuracy.
        # Aronson (2006) Ch.6 — training baseline is ground truth for stationarity.
        try:
            from src.diagnostics.signal_debugger import (
                get_degradation_tracker,
                get_drift_monitor,
            )

            _X_df = fm.features[_active_cols]
            _dm = get_drift_monitor()
            for col in _active_cols:
                if col in _X_df.columns:
                    _dm.set_baseline(col, _X_df[col].dropna().tolist())
            get_degradation_tracker().set_training_metrics(
                accuracy=float(mean_acc),
                f1=float(mean_f1),
            )
        except Exception as _diag_exc:
            self._log.warning(
                "trainer.drift_baseline_push_failed",
                error=str(_diag_exc)[:200],
            )

        return result

    # ------------------------------------------------------------------
    # Ensemble predictor (ARIMA/XGBoost/LSTM/GP/TreeEnsemble)
    # ------------------------------------------------------------------

    def train_ensemble(self, fm: FeatureMatrix) -> EnsemblePredictor:
        """
        Fit the diversified prediction ensemble (src/intelligence/ensemble_predictor.py)
        alongside the direction/meta-label models.

        Target: fm.log_returns — the same per-bar log-return series already
        used for CPCV sample weighting and oos_sharpe_and_drawdown() in this
        module, so the ensemble's regression target matches the convention
        this trainer already establishes rather than introducing a second,
        differently-defined "return" semantic.

        Feature columns: the same coverage-gated active column set used by
        train_direction(), so signal_engine.py can build one feature row
        and feed it to both the XGBoost direction model and the ensemble.
        """
        from src.intelligence.ensemble_predictor import EnsemblePredictor

        _active_cols = get_active_feature_columns(
            coverage=getattr(fm, "intelligence_coverage", None),
            min_coverage=0.6,
        )
        _active_cols = [c for c in _active_cols if c in fm.features.columns]

        X = fm.features[_active_cols]
        y = fm.log_returns

        self._log.info("trainer.ensemble.start", n_samples=len(X), n_features=len(_active_cols))
        ensemble = EnsemblePredictor()
        ensemble.fit(X, y)
        self._log.info("trainer.ensemble.done", weights=ensemble.weights)
        return ensemble

    def save_ensemble(
        self,
        ensemble: EnsemblePredictor,
        model_dir: str | Path,
    ) -> Path:
        """Persist a fitted EnsemblePredictor for this trainer's symbol/timeframe."""
        return ensemble.save(model_dir, self._symbol, self._timeframe)

    @staticmethod
    def load_ensemble(
        model_dir: str | Path,
        symbol: str,
        timeframe: str,
    ) -> EnsemblePredictor:
        """Load a previously saved EnsemblePredictor, verifying SHA-256 integrity."""
        from src.intelligence.ensemble_predictor import EnsemblePredictor

        _validate_timeframe(timeframe)
        return EnsemblePredictor.load(model_dir, symbol, timeframe)

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
        # GAP-015: resolve active cols same way as train_direction.
        _active_cols_meta = get_active_feature_columns(
            coverage=getattr(fm, "intelligence_coverage", None),
            min_coverage=0.6,
        )
        _active_cols_meta = [c for c in _active_cols_meta if c in fm.features.columns]
        x_dir = fm.features[_active_cols_meta].to_numpy(dtype=np.float64)
        dir_probs = direction_model.predict_proba(x_dir)[:, 1]  # P(long)
        dir_preds = (dir_probs >= 0.5).astype(np.int8)

        # Meta-labels: 1 when direction model agrees with realized outcome
        meta_y = meta_labels(
            pd.Series(dir_preds, index=fm.labels.index, dtype=np.int8),
            fm.labels,
        ).to_numpy(dtype=np.int8)

        # Extended feature matrix: primary features + direction signal features
        confidence = np.abs(dir_probs - 0.5).reshape(-1, 1)
        p_long = dir_probs.reshape(-1, 1)
        x_meta = np.hstack([x_dir, p_long, confidence])

        log_ret = fm.log_returns.to_numpy(dtype=np.float64)
        weights = compute_sample_weights(fm.log_returns)

        self._log.info(
            "trainer.meta.start",
            n_samples=len(x_meta),
            n_bet=int((meta_y == 1).sum()),
            n_skip=int((meta_y == 0).sum()),
        )

        cpcv_cfg = self._feature_cfg
        folds = build_cpcv_folds(
            n_samples=len(x_meta),
            n_splits=cpcv_cfg.cpcv_n_splits,
            n_test_splits=cpcv_cfg.cpcv_n_test_splits,
            purge_gap=cpcv_cfg.purge_gap_bars,
            embargo_pct=cpcv_cfg.embargo_pct,
        )

        t0 = time.perf_counter()
        fold_metrics, all_oos_ret, all_oos_pred = self._run_cpcv(
            x_meta, meta_y, log_ret, weights, folds, model_name=MODEL_META_LABEL
        )
        elapsed = time.perf_counter() - t0

        # M-07 (meta model): guard against empty fold_metrics
        if not fold_metrics:
            self._log.warning(
                "trainer.cpcv.no_valid_folds",
                model=MODEL_META_LABEL,
                n_samples=len(x_meta),
                action="returning failed TrainingResult — dataset too small for CPCV",
            )
            # VF-021: no eval_set available here — see matching comment in
            # train_direction() above.
            final_model = _build_xgb(self._xgb_cfg, scale_pos_weight=1.0)
            final_model.set_params(early_stopping_rounds=None)
            final_model.fit(x_meta, meta_y, sample_weight=weights, verbose=False)
            return TrainingResult(
                model=final_model,
                oos_sharpe=0.0,
                max_drawdown=100.0,
                n_trades=0,
                accuracy=0.0,
                precision=0.0,
                recall=0.0,
                f1=0.0,
                live_gate_pass=False,
                elapsed_s=round(elapsed, 2),
            )

        oos_sharpe, max_dd = oos_sharpe_and_drawdown(
            np.concatenate(all_oos_pred), np.concatenate(all_oos_ret)
        )

        pos_weight = float((meta_y == 0).sum()) / max(float((meta_y == 1).sum()), 1.0)
        final_model = _build_xgb(self._xgb_cfg, scale_pos_weight=pos_weight)
        split = int(len(x_meta) * 0.85)
        eval_set = [(x_meta[split:], meta_y[split:])]
        final_model.fit(
            x_meta[:split],
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
        # Use model's n_features_in_ to slice the correct columns.
        # Falls back to 7 base features for models trained before GAP-015.
        _n = getattr(model, "n_features_in_", len(BASE_FEATURE_COLUMNS))
        _pred_cols = (
            list(feature_vec.index[:_n]) if len(feature_vec) >= _n else list(feature_vec.index)
        )
        X = feature_vec.reindex(_pred_cols).to_numpy(dtype=np.float64).reshape(1, -1)
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
        # GAP-015: resolve base feature count from model's n_features_in_.
        # meta_model is trained with (base_features + 2 direction signals).
        # Legitimate cases:
        #   - Pre-GAP-015 model: n_features_in_ = 9 (BASE) + 2 = 9.  feature_vec has 7.
        #   - GAP-015 model: n_features_in_ = 9 + N_intel + 2.        feature_vec has 7 + N_intel.
        # Illegitimate case (SCAN2-005): feature_vec columns don't match model schema at all.
        expected_n = getattr(meta_model, "n_features_in_", None)
        if expected_n is not None:
            # Minimum valid schema: 7 base (BASE_FEATURE_COLUMNS) + 2 direction signals.
            # Any model with n_features_in_ < this minimum has an incompatible schema.
            _min_valid = len(BASE_FEATURE_COLUMNS) + 2
            if expected_n < _min_valid:
                raise ValueError(
                    f"predict_meta: meta_model expects {expected_n} input columns, "
                    f"which is below the minimum valid schema ({_min_valid} = "
                    f"{len(BASE_FEATURE_COLUMNS)} base + 2 signals). "
                    "Model was trained with a different feature schema — retrain required."
                )
            # expected base cols that the model was trained on
            _base_n_expected = expected_n - 2
            # base cols available in this feature_vec
            _base_n_available = len(feature_vec)
            # Invalid iff available < expected (missing required columns).
            if _base_n_available < _base_n_expected:
                raise ValueError(
                    f"predict_meta: feature vector has {_base_n_available} base columns "
                    f"but meta_model expects {_base_n_expected} base columns (+2 signals = {expected_n}). "
                    "Model was trained with a different feature schema — retrain required."
                )
            # Slice to exactly the count the model expects
            _pred_cols_meta = list(feature_vec.index[:_base_n_expected])
        else:
            _pred_cols_meta = list(feature_vec.index)

        base = feature_vec.reindex(_pred_cols_meta).to_numpy(dtype=np.float64)
        confidence = abs(p_long - 0.5)
        X = np.append(base, [p_long, confidence]).reshape(1, -1)
        # Final shape sanity check (catches unexpected edge cases)
        if expected_n is not None and X.shape[1] != expected_n:
            raise ValueError(
                f"predict_meta: feature vector has {X.shape[1]} columns but "
                f"meta_model expects {expected_n}. "
                "Model was trained with a different feature schema — retrain required."
            )
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
            dir_buf := io.BytesIO(),
            compress=3,
        )
        dir_data = dir_buf.getvalue()
        _atomic_write_bytes(dir_path, dir_data)
        _write_manifest(dir_path, dir_data)

        joblib.dump(
            {"model": meta_model, "version": version, "symbol": self._symbol, "timeframe": tf},
            meta_buf := io.BytesIO(),
            compress=3,
        )
        meta_data = meta_buf.getvalue()
        _atomic_write_bytes(meta_path, meta_data)
        _write_manifest(meta_path, meta_data)

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
        """Load a previously saved direction model, verifying SHA-256 integrity."""
        _validate_timeframe(timeframe)
        path = Path(model_dir) / _DIRECTION_FILENAME.format(
            symbol=symbol.replace("/", "_"), timeframe=timeframe
        )
        if not path.exists():
            raise FileNotFoundError(f"No direction model at {path}")
        data = _verify_manifest(path)
        return joblib.load(io.BytesIO(data))["model"]

    @staticmethod
    def load_meta(
        model_dir: str | Path,
        symbol: str,
        timeframe: str,
    ) -> XGBClassifier:
        """Load a previously saved meta-label model, verifying SHA-256 integrity."""
        _validate_timeframe(timeframe)
        path = Path(model_dir) / _META_FILENAME.format(
            symbol=symbol.replace("/", "_"), timeframe=timeframe
        )
        if not path.exists():
            raise FileNotFoundError(f"No meta-label model at {path}")
        data = _verify_manifest(path)
        return joblib.load(io.BytesIO(data))["model"]

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
        skipped_folds = 0

        for fold in folds:
            tr = fold.train_idx
            te = fold.test_idx

            # Guard: both classes present in train
            if len(np.unique(y[tr])) < 2:
                skipped_folds += 1
                # M-10: promoted to WARNING — silent skipping at DEBUG degrades OOS
                # estimate quality without alerting operators.
                self._log.warning(
                    "trainer.cpcv.skip_fold_imbalanced",
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
                # Not enough for eval set — fit on full train fold without early stopping.
                # VF-021: set early_stopping_rounds=None via set_params is not reliably
                # honoured across all XGBoost versions; explicitly rebuild without it.
                from copy import deepcopy as _deepcopy

                model_no_es = _deepcopy(model)
                model_no_es.set_params(early_stopping_rounds=None)
                model_no_es.fit(
                    X[tr],
                    y[tr],
                    sample_weight=weights[tr],
                    verbose=False,
                )
                model = model_no_es
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
            fold_sharpe, _ = oos_sharpe_and_drawdown(y_pred, log_ret[te])

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

        # M-10: fail-safe for excessive fold skipping — more than 30% skipped
        # means the dataset has severe label imbalance; OOS metrics are unreliable.
        if folds and skipped_folds / len(folds) > 0.30:
            self._log.error(
                "trainer.cpcv.excessive_fold_skipping",
                skipped=skipped_folds,
                total=len(folds),
                action="live_gate_forced_fail — label imbalance too severe for reliable CPCV",
            )
            # Return empty metrics so caller's empty-fold guard fires live_gate=False
            return [], [], []

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
