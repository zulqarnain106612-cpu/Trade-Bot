# Trade-Bot — Institutional-Grade Algorithmic Trading System

## Architecture

```Market Data (Binance/OKX WebSocket)
         │
         ▼
 Regime Detector (HMM 3-state)
         │
         ▼
 Feature Pipeline (fractional diff · VWAP · OFI · realized vol)
         │
         ▼
 Primary Model (XGBoost direction classifier)
         │
         ▼
 Meta-Label Gate (XGBoost: should we bet at all?)
         │
         ▼
 Kelly Position Sizer
         │
         ▼
 Triple-Barrier Risk Gate (stop · take-profit · time-exit)
         │
    ┌────▼────┐
    │  Mode   │  AUTOMATIC / RESTRICTED / MANUAL
    └────┬────┘
         │
 Execution Engine (Paper / Live)
         │
 Binance REST · OKX REST
         │
 Portfolio Engine (P&L · drawdown · exposure)
         │
 Dashboard (FastAPI + React, real-time WebSocket)
```

## Stack

- Python 3.11+ — signal research, ML, portfolio, API
- Rust — WAL journal, execution hot path
- XGBoost 2.x — primary + meta-label classifiers
- hmmlearn — regime detection
- FastAPI + WebSocket — dashboard backend
- React + Recharts — dashboard frontend
- SQLite — local trade/signal/performance storage
- CCXT — unified Binance/OKX REST adapter

## Timeframes

- SCALPING  (15s–5m) — paper only unless regime = trending + spread < 2 ticks
- INTRADAY  (15m–4h) — primary real-money timeframe under $10k capital
- SWING     (4h–1D)  — activated when Sharpe > 1.5 confirmed on 30-day paper

All 3 run simultaneously in paper. Real capital routes only to timeframes
with confirmed positive expectancy in the current regime.

## Trading Modes

- AUTOMATIC  — no approvals, all trades execute
- RESTRICTED — autonomous below notional limit, approval required above it
- MANUAL     — every trade requires explicit approval, none execute without it

Timeout on approval requests: configurable (default 60s), auto-skip on timeout.

## Safety Gates (non-negotiable, cannot be disabled from dashboard)

1. Daily drawdown circuit breaker — halts all trading at configurable % loss
2. Consecutive loss halt — halts after N losing trades in a row
3. Regime-change halt — exits all positions when HMM regime shifts unexpectedly
4. Max position size — hard cap as % of total capital
5. Paper-first enforcement — system boots in PAPER mode, live requires explicit flag

## Setup

```bash
bash bootstrap.sh
```

Then open: <http://localhost:5173>

## API Keys

Set in dashboard Settings panel or in .env:

```BINANCE_API_KEY=...
BINANCE_API_SECRET=...
OKX_API_KEY=...
OKX_API_SECRET=...
OKX_PASSPHRASE=...
TRADING_MODE=paper   # change to 'live' only after 30-day paper validation
```

## References

- López de Prado, M. (2018). *Advances in Financial Machine Learning*. Wiley.
- Chan, E. (2013). *Algorithmic Trading: Winning Strategies and Their Rationale*. Wiley.
- Kelly, J.L. (1956). A New Interpretation of Information Rate. *Bell System Technical Journal*.
