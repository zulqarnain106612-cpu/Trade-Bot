"""
Purged K-Fold Cross-Validation — López de Prado, "Advances in Financial
Machine Learning" (2018), Ch. 7.

Problem this solves: standard K-fold CV assumes samples are i.i.d. Financial
time-series labels are NOT i.i.d. — a label at time t is often computed
using information overlapping with time t+1, t+2, ... (e.g. our label looks
`horizon` candles ahead). If a training fold's overlap-range intersects a
test fold, information leaks even in a "walk-forward" split. Two fixes:

  1. PURGING: remove training samples whose label-computation window
     overlaps the test set's date range.
  2. EMBARGO: additionally remove a buffer of training samples immediately
     AFTER the test set, since serial correlation in return-based features
     (e.g. rolling volatility) can leak backward-looking information from
     the test period into training if the model is fit before test data is
     locked (defensive extra margin; strictly needed only in some setups,
     included here because it's cheap insurance).

This is stricter than the simple walk-forward split used in backtest.py's
v1/v2, and produces a "Probability of Backtest Overfitting" (PBO) style
estimate: how consistent performance is across many folds vs. one lucky split.
"""

import numpy as np


def purged_kfold_splits(
    n_samples: int, n_splits: int = 5, embargo_pct: float = 0.01, horizon: int = 4
):
    """
    Yields (train_idx, test_idx) arrays. Each test fold is a contiguous
    block; training data excludes:
      (a) any sample whose label window [i, i+horizon] overlaps the test block
      (b) an embargo buffer immediately after the test block
    """
    indices = np.arange(n_samples)
    fold_sizes = np.full(n_splits, n_samples // n_splits, dtype=int)
    fold_sizes[: n_samples % n_splits] += 1
    embargo = int(n_samples * embargo_pct)

    current = 0
    for fold_size in fold_sizes:
        test_start, test_end = current, current + fold_size  # [start, end)
        test_idx = indices[test_start:test_end]

        # Purge: drop train samples whose label window overlaps test range
        purge_start = max(0, test_start - horizon)
        purge_end = min(n_samples, test_end + horizon + embargo)

        train_mask = np.ones(n_samples, dtype=bool)
        train_mask[purge_start:purge_end] = False
        train_idx = indices[train_mask]

        yield train_idx, test_idx
        current = test_end


def probability_of_backtest_overfitting(fold_accuracies: list) -> dict:
    """
    Simplified PBO-style diagnostic (full CPCV/PBO per López de Prado
    requires combinatorial fold generation; this is an honest lightweight
    proxy): if performance is consistent across folds, overfitting risk is
    lower. High variance or a large gap between best-fold and median-fold
    performance signals a fold-specific fluke rather than genuine edge.
    """
    accs = np.array(fold_accuracies)
    median_acc = np.median(accs)
    best_acc = np.max(accs)
    std_acc = np.std(accs)

    # Fraction of folds performing at/below random chance (0.5) —
    # a robust real edge should rarely dip to coin-flip or below
    frac_at_or_below_chance = float((accs <= 0.50).mean())

    return {
        "fold_accuracies": accs.tolist(),
        "median_accuracy": float(median_acc),
        "best_fold_accuracy": float(best_acc),
        "std_across_folds": float(std_acc),
        "fraction_folds_at_or_below_chance": frac_at_or_below_chance,
        "overfitting_flag": bool((best_acc - median_acc > 0.08) or (frac_at_or_below_chance > 0.4)),
    }


if __name__ == "__main__":
    from data_fetch_kraken import fetch_ohlcv
    from indicators import build_feature_matrix
    from xgboost import XGBClassifier

    df = fetch_ohlcv("XBTUSD", "1h")
    feats = build_feature_matrix(df)
    horizon = 4
    future_return = df["close"].shift(-horizon) / df["close"] - 1
    label = (future_return > 0).astype(int)
    data = feats.copy()
    data["label"] = label
    data = data.dropna()

    feature_cols = [c for c in data.columns if c != "label"]
    fold_accs = []

    print("Running Purged K-Fold CV (5 folds, embargo 1%, horizon=4)...\n")
    for fold_i, (train_idx, test_idx) in enumerate(
        purged_kfold_splits(len(data), n_splits=5, embargo_pct=0.01, horizon=horizon)
    ):
        if len(train_idx) < 50 or len(test_idx) < 5:
            continue
        train = data.iloc[train_idx]
        test = data.iloc[test_idx]

        model = XGBClassifier(
            n_estimators=100, max_depth=3, learning_rate=0.05, eval_metric="logloss", verbosity=0
        )
        model.fit(train[feature_cols], train["label"])
        preds = model.predict(test[feature_cols])
        acc = (preds == test["label"].values).mean()
        fold_accs.append(acc)
        print(f"Fold {fold_i}: train={len(train_idx)} test={len(test_idx)} accuracy={acc:.1%}")

    print()
    diagnostic = probability_of_backtest_overfitting(fold_accs)
    print("=" * 60)
    print("OVERFITTING DIAGNOSTIC")
    print("=" * 60)
    for k, v in diagnostic.items():
        print(f"{k}: {v}")
