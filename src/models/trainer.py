"""
XGBoost primary classifier + meta-label gating.

Primary model:  predicts direction (1=up, 0=down) from feature matrix.
Meta-label model: predicts whether the primary model will be correct —
                  gates the signal. Only trades where both models agree execute.

Reference: López de Prado (2018), Ch.3 (triple-barrier labeling),
           Ch.4 (meta-labeling).

Validation: Combinatorial Purged Cross-Validation (CPCV) to prevent
            lookahead bias. Standard k-fold is invalid for time series.
            Reference: López de Prado (2018), Ch.7.
"""
from __future__ import annotations
import numpy as np
import pandas as pd
import xgboost as xgb
from sklearn.metrics import precision_score, recall_score
import joblib
import os
import structlog

log = structlog.get_logger()


def triple_barrier_labels(
    close:      pd.Series,
    vol:        pd.Series,
    pt_sl:      tuple[float, float] = (1.5, 1.0),
    horizon:    int = 10,
) -> pd.Series:
    """
    Triple-barrier labeling.
    pt_sl = (profit-taking multiplier, stop-loss multiplier) × daily_vol.
    Returns Series: 1 (up), -1 (down), 0 (timeout).
    Reference: López de Prado (2018), Ch.3.
    """
    labels = {}
    for i in range(len(close) - horizon):
        ret_fwd  = close.iloc[i + 1: i + horizon + 1] / close.iloc[i] - 1
        upper    = vol.iloc[i] * pt_sl[0]
        lower    = -vol.iloc[i] * pt_sl[1]
        hit_up   = ret_fwd[ret_fwd >= upper]
        hit_down = ret_fwd[ret_fwd <= lower]
        t_up     = hit_up.index[0]   if not hit_up.empty   else None
        t_down   = hit_down.index[0] if not hit_down.empty else None
        if t_up and t_down:
            labels[close.index[i]] = 1 if t_up < t_down else -1
        elif t_up:
            labels[close.index[i]] = 1
        elif t_down:
            labels[close.index[i]] = -1
        else:
            labels[close.index[i]] = 0
    return pd.Series(labels)


def cpcv_splits(n: int, n_splits: int = 6, pct_embargo: float = 0.01):
    """
    Combinatorial Purged Cross-Validation splits.
    Generates (train_idx, test_idx) pairs with purging and embargo.
    Reference: López de Prado (2018), Ch.7.
    """
    embargo = int(n * pct_embargo)
    fold_size = n // n_splits
    for i in range(n_splits):
        test_start = i * fold_size
        test_end   = test_start + fold_size
        train_idx  = list(range(0, max(0, test_start - embargo))) +                      list(range(min(n, test_end + embargo), n))
        test_idx   = list(range(test_start, test_end))
        if len(train_idx) > 100 and len(test_idx) > 20:
            yield train_idx, test_idx


class ModelTrainer:
    def __init__(self, model_dir: str = "./models"):
        self._model_dir    = model_dir
        self._primary:  xgb.XGBClassifier | None = None
        self._meta:     xgb.XGBClassifier | None = None

    def _xgb_params(self) -> dict:
        return {
            "n_estimators":     300,
            "max_depth":        4,
            "learning_rate":    0.05,
            "subsample":        0.8,
            "colsample_bytree": 0.8,
            "min_child_weight": 10,
            "reg_alpha":        0.1,
            "reg_lambda":       1.0,
            "use_label_encoder": False,
            "eval_metric":      "logloss",
            "random_state":     42,
            "n_jobs":           -1,
        }

    def train(
        self,
        features: pd.DataFrame,
        close:    pd.Series,
        timeframe: str = "intraday",
    ) -> dict:
        """
        Full training pipeline with CPCV validation.
        Returns performance metrics.
        """
        # Daily vol for triple-barrier
        ret = np.log(close / close.shift(1))
        vol = ret.rolling(20).std().fillna(method="bfill")

        # Labels
        labels = triple_barrier_labels(close, vol, pt_sl=(1.5, 1.0), horizon=10)
        labels = labels[labels != 0]  # drop timeout bars

        # Align features with labels
        common = features.index.intersection(labels.index)
        X = features.loc[common].values
        y = (labels.loc[common] == 1).astype(int).values

        if len(X) < 200:
            log.warning("insufficient data for training", bars=len(X))
            return {"status": "insufficient_data", "bars": len(X)}

        # CPCV validation
        oos_precision = []
        oos_recall    = []
        for train_idx, test_idx in cpcv_splits(len(X), n_splits=6):
            clf = xgb.XGBClassifier(**self._xgb_params())
            clf.fit(X[train_idx], y[train_idx], verbose=False)
            preds = clf.predict(X[test_idx])
            if len(np.unique(y[test_idx])) > 1:
                oos_precision.append(precision_score(y[test_idx], preds, zero_division=0))
                oos_recall.append(recall_score(y[test_idx], preds, zero_division=0))

        metrics = {
            "oos_precision": float(np.mean(oos_precision)) if oos_precision else 0.0,
            "oos_recall":    float(np.mean(oos_recall))    if oos_recall    else 0.0,
            "train_bars":    len(X),
        }
        log.info("CPCV validation", **metrics)

        # Train final primary model on all data
        self._primary = xgb.XGBClassifier(**self._xgb_params())
        self._primary.fit(X, y, verbose=False)

        # Meta-label: train on primary model's correctness
        primary_preds = self._primary.predict(X)
        meta_y = (primary_preds == y).astype(int)
        self._meta = xgb.XGBClassifier(**self._xgb_params())
        self._meta.fit(X, meta_y, verbose=False)

        os.makedirs(self._model_dir, exist_ok=True)
        joblib.dump(self._primary, f"{self._model_dir}/primary_{timeframe}.pkl")
        joblib.dump(self._meta,    f"{self._model_dir}/meta_{timeframe}.pkl")
        log.info("models saved", timeframe=timeframe)

        metrics["status"] = "trained"
        return metrics

    def load(self, timeframe: str) -> bool:
        p = f"{self._model_dir}/primary_{timeframe}.pkl"
        m = f"{self._model_dir}/meta_{timeframe}.pkl"
        if os.path.exists(p) and os.path.exists(m):
            self._primary = joblib.load(p)
            self._meta    = joblib.load(m)
            log.info("models loaded", timeframe=timeframe)
            return True
        return False

    def predict(self, feature_row: np.ndarray) -> tuple[int, float, float]:
        """
        Returns (direction, primary_confidence, meta_confidence).
        direction: 1=long, -1=short (returns -1 if primary says 0).
        """
        if self._primary is None or self._meta is None:
            raise RuntimeError("Models not loaded")
        row = feature_row.reshape(1, -1)
        primary_proba = self._primary.predict_proba(row)[0]
        primary_dir   = int(self._primary.predict(row)[0])
        meta_proba    = self._meta.predict_proba(row)[0]
        meta_conf     = float(meta_proba[1])
        direction     = 1 if primary_dir == 1 else -1
        return direction, float(primary_proba[primary_dir]), meta_conf

