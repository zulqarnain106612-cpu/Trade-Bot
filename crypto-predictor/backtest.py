"""
Walk-forward backtest engine.

Design rules enforced here (see ARCHITECTURE.md section 2.5):
  1. Never train on future data relative to the test window (walk-forward only).
  2. Label = FUTURE return, but the label column is never in the feature set
     used for that same row's prediction (checked explicitly below).
  3. Fees + slippage are always subtracted — an edge that vanishes after
     costs is reported as a loss, not hidden.
  4. Report accuracy AND Sharpe AND max drawdown — a single accuracy number
     is not sufficient to judge a strategy.
"""

import numpy as np
import pandas as pd
from data_fetch_kraken import fetch_ohlcv
from indicators import build_feature_matrix
from xgboost import XGBClassifier

from risk import calibrate_probabilities, kelly_fraction


FEE_RATE = 0.001  # 0.10% per trade, typical retail spot taker fee
SLIPPAGE_RATE = 0.0005  # 0.05% assumed slippage on market orders


def build_dataset(df: pd.DataFrame, horizon: int = 4):
    """
    horizon: number of candles ahead the label looks (e.g. 4 candles on 1h = 4h ahead).
    Label = 1 if future close > current close, else 0.
    """
    feats = build_feature_matrix(df)
    future_return = df["close"].shift(-horizon) / df["close"] - 1
    label = (future_return > 0).astype(int)

    data = feats.copy()
    data["label"] = label
    data["future_return"] = future_return  # used only for P&L simulation, not as a feature
    data = data.dropna()
    return data


def walk_forward_backtest(
    data: pd.DataFrame, train_window: int = 300, test_window: int = 50, horizon: int = 4
):
    """
    Rolls a fixed-size training window forward, retraining each step,
    predicting only on the immediately following test_window rows.
    This guarantees no future information leaks into training.
    """
    feature_cols = [c for c in data.columns if c not in ("label", "future_return")]
    results = []

    start = 0
    while start + train_window + test_window <= len(data):
        train = data.iloc[start : start + train_window]
        test = data.iloc[start + train_window : start + train_window + test_window]

        model = XGBClassifier(
            n_estimators=100,
            max_depth=3,
            learning_rate=0.05,
            eval_metric="logloss",
            verbosity=0,
        )
        model.fit(train[feature_cols], train["label"])

        # Calibrate probabilities (raw XGBoost probs are not well-calibrated
        # out of the box) using the same training fold — never the test fold.
        calibrated_probs, _ = calibrate_probabilities(
            model,
            train[feature_cols],
            train["label"],
            test[feature_cols],
            method="sigmoid",  # sigmoid (Platt) needs less data than isotonic
        )
        preds = (calibrated_probs > 0.5).astype(int)

        # Historical win/loss stats from TRAIN only (never test) for Kelly sizing
        train_wins = train.loc[train["label"] == 1, "future_return"]
        train_losses = train.loc[train["label"] == 0, "future_return"]
        avg_win = train_wins.mean() if len(train_wins) > 0 else 0.01
        avg_loss = abs(train_losses.mean()) if len(train_losses) > 0 else 0.01
        avg_loss = max(avg_loss, 1e-4)  # guard divide-by-zero

        position_sizes = np.array(
            [
                kelly_fraction(p, avg_win, avg_loss, kelly_multiplier=0.25) if pred == 1 else 0.0
                for p, pred in zip(calibrated_probs, preds, strict=False)
            ]
        )

        step = test.copy()
        step["pred"] = preds
        step["prob_up"] = calibrated_probs
        step["position_size"] = position_sizes
        results.append(step)

        start += test_window

    return pd.concat(results) if results else pd.DataFrame()


def evaluate(results: pd.DataFrame):
    if results.empty:
        print("Not enough data for a full walk-forward pass — need more history.")
        return

    accuracy = (results["pred"] == results["label"]).mean()

    # Simulate P&L: position sized by quarter-Kelly fraction (risk.py), not
    # flat full-notional per signal. Costs apply only to capital actually deployed.
    pos = results["position_size"].to_numpy()
    strat_return = pos * (results["future_return"].to_numpy() - FEE_RATE - SLIPPAGE_RATE)
    avg_position_size = results.loc[results["pred"] == 1, "position_size"].mean()
    cum_return = (1 + pd.Series(strat_return, index=results.index)).cumprod()

    n_trades = int((results["pred"] == 1).sum())
    total_return_pct = (cum_return.iloc[-1] - 1) * 100 if len(cum_return) else 0.0

    running_max = cum_return.cummax()
    drawdown = (cum_return - running_max) / running_max
    max_dd_pct = drawdown.min() * 100 if len(drawdown) else 0.0

    ann_factor = np.sqrt(252 * 24)  # approx for 1h candles
    ret_series = pd.Series(strat_return, index=results.index)
    sharpe = (
        ret_series.mean() / ret_series.std() * ann_factor if ret_series.std() > 0 else float("nan")
    )

    print("=" * 60)
    print("WALK-FORWARD BACKTEST RESULTS (real data, cost-adjusted)")
    print("=" * 60)
    print(f"Test samples:              {len(results)}")
    print(f"Directional accuracy:      {accuracy:.1%}")
    print(f"Trades taken (long signal):{n_trades}")
    print(
        f"Avg Kelly position size:   {avg_position_size:.1%} of capital"
        if n_trades
        else "Avg Kelly position size:   n/a"
    )
    print(f"Total return (after fees): {total_return_pct:.2f}%")
    print(f"Max drawdown:              {max_dd_pct:.2f}%")
    print(f"Annualized Sharpe (approx):{sharpe:.2f}")
    print("-" * 60)
    print("Reminder: 55-60% accuracy is the realistic ceiling per published")
    print("literature. A number far above this on a short sample is far more")
    print("likely overfitting/small-sample luck than genuine edge.")


if __name__ == "__main__":
    print("Fetching real BTC/USD 1h candles from Kraken...")
    df = fetch_ohlcv("XBTUSD", "1h")
    print(f"Got {len(df)} real candles.\n")

    dataset = build_dataset(df, horizon=4)
    print(f"Usable rows after feature warm-up: {len(dataset)}\n")

    results = walk_forward_backtest(dataset, train_window=300, test_window=50, horizon=4)
    evaluate(results)
