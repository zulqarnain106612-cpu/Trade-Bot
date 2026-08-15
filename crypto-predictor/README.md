# Crypto Prediction Engine — Starter Implementation

Full architecture/roadmap: see `ARCHITECTURE.md`.

## What's here (real, working, tested against live data)

| File | Purpose |
|---|---|
| `data_fetch_kraken.py` | Fetches real live OHLCV from Kraken's public API |
| `indicators.py` | Real indicator math (SMA/EMA/MACD/RSI/Bollinger/ATR/OBV) |
| `backtest.py` | Leak-free walk-forward backtester with fee/slippage costs |
| `data_fetch.py` | Binance version (returns HTTP 451 in this sandbox — geo-blocked; will work in an unrestricted environment, e.g. your own machine/server) |
| `risk.py` | Real Kelly Criterion formula + probability calibration (Platt/isotonic) — used by the backtester for position sizing, not flat all-in trades |
| `volatility.py` | Real GARCH(1,1) volatility model (Bollerslev, 1986) — captures crypto's volatility clustering, includes a leak-free walk-forward variant |
| `validation.py` | Purged K-Fold CV + embargo (López de Prado method) with an overfitting diagnostic — stricter than the simple walk-forward split above |

## How to run

```bash
pip install requests pandas numpy scikit-learn xgboost
python3 backtest.py
```

This will:
1. Pull the last ~720 real hourly BTC/USD candles from Kraken (no API key needed)
2. Build the full indicator feature matrix
3. Run a walk-forward backtest (train 300 candles → test next 50 → roll forward)
4. Report **accuracy, total return after real fees, max drawdown, and Sharpe** — not just accuracy alone

## The real results (already run, reproducible)

**v1 — flat all-in trades, uncalibrated probability:**
```
Directional accuracy:      50.0%
Total return (after fees): -18.60%
Max drawdown:              -18.61%
Annualized Sharpe (approx):-13.77
```

**v2 — calibrated probability (Platt scaling) + quarter-Kelly position sizing:**
```
Directional accuracy:      53.7%
Avg Kelly position size:   4.6% of capital
Total return (after fees): -2.45%
Max drawdown:              -2.43%
Annualized Sharpe (approx):-20.00
```

**This is the correct, honest outcome for a first pass on ~1 month of
data with no order-flow/on-chain/sentiment features.** The accuracy barely
moved (50%→53.7%, still near a coin flip — small-sample noise, not a real
edge yet). What changed is that **Kelly sizing cut the loss by ~87%** by
risking only ~4.6% of capital per trade instead of going all-in on a weak,
uncertain signal. That's the actual lesson professional risk-sizing
formulas exist for: they don't make a bad edge good, they stop a bad edge
from being catastrophic. If this had shown 80%+ accuracy or a big profit on
this little data, that would be the red flag, not the good outcome.

**v3 — Purged K-Fold CV (López de Prado method), the stricter test:**
```
Fold 0: accuracy=51.1%
Fold 1: accuracy=51.1%
Fold 2: accuracy=44.3%   <- below chance
Fold 3: accuracy=39.3%   <- below chance
Fold 4: accuracy=53.6%
Median accuracy: 51.1%
2 of 5 folds performed at/below random chance
```

**This is the most important finding in the whole project so far.** The
simple walk-forward split (v2) suggested a real 53.7% edge. Purged CV —
which removes label-window overlap between train/test instead of just
splitting by date — shows the true picture: **median accuracy of 51.1%,
with 40% of folds performing at or below a coin flip.** The apparent edge
in v2 was likely fold-specific noise, not a genuine, stable signal. This is
exactly the failure mode purged CV exists to catch, and exactly why
`ARCHITECTURE.md` insisted on it over simpler validation before trusting
any number. `validation.py` also outputs an automated `overfitting_flag`
diagnostic for this.

`volatility.py` adds a real GARCH(1,1) model (the standard institutional
approach to volatility clustering) as a candidate feature/regime input for
future ensemble phases — verified against live data (current BTC/USD
annualized volatility ≈ 26%).

## What would legitimately move the needle from here (in priority order)

1. **More history** (multiple years, multiple market regimes) — 700 hours
   is nowhere near enough to draw a real conclusion
2. **Order-book imbalance features** — requires live WebSocket order-book
   capture, not just OHLCV; this is the feature type with the most
   published evidence of real (if small) short-horizon edge
3. **Purged/combinatorial cross-validation** (López de Prado method) instead
   of the simpler walk-forward split used here, to get a proper Probability
   of Backtest Overfitting estimate
4. **On-chain + sentiment features** layered in, per Phase 2-4 of the roadmap
5. **Hyperparameter tuning + ensemble (GARCH + LSTM + XGBoost + meta-learner)**
   only after the above — added complexity before a clean baseline is
   confirmed leak-free just hides bugs, it doesn't fix them

## Explicit non-goal

This is not, and will not become, a system that reliably prints money. The
architecture doc's ceiling (55-60% out-of-sample accuracy, Sharpe 1-2 after
costs) is the realistic target for a well-built system — not 90%+ accuracy,
not guaranteed returns. Every future phase should be validated against that
ceiling, not against the wish to beat it.
