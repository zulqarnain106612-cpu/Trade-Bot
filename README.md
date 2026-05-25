# Trade-Bot

## What This Is

- Algorithmic trading system for Crypto (Binance, OKX)
- Real-money capable after mandatory 30-day paper validation gate
- Single-screen dashboard — every control, chart, and mode on one page
- Boots in paper mode — live trading requires explicit opt-in

---

## Architecture

```Binance / OKX WebSocket
        │
        ▼
Regime Detector (HMM — 3 states: ranging · trending · volatile)
        │
        ▼
Feature Pipeline
  · Fractionally differentiated log-price (d=0.4)
  · VWAP deviation
  · Order flow imbalance
  · Realized volatility ratio (short/long)
  · ATR-normalized momentum
  · Rolling Sharpe
  · Volume z-score
        │
        ▼
Primary Model (XGBoost — direction: long / short)
        │
        ▼
Meta-Label Gate (XGBoost — should we bet at all?)
        │
        ▼
Kelly Position Sizer (half-Kelly, adaptive to trade history)
        │
        ▼
Triple-Barrier Risk Gate (stop · take-profit · time-exit)
        │
   ┌────▼────┐
   │  Mode   │
   └────┬────┘
        │
Execution Engine
  · Paper  — simulated fills, realistic slippage + fees
  · Live   — Binance / OKX REST (PostOnly → market fallback)
        │
Portfolio Engine → SQLite → Dashboard
```

---

## Trading Modes

- **AUTOMATIC** — no approvals, all signals execute immediately
- **RESTRICTED** — autonomous below notional limit · approval required above it · auto-skip on timeout
- **MANUAL** — every signal waits for approval · nothing executes without it

---

## Timeframes

- **SCALPING** `(15s – 5m)` — paper only unless regime = trending + spread < 2 ticks
- **INTRADAY** `(15m – 4h)` — primary real-money timeframe under $10k capital
- **SWING**    `(4h – 1D)`  — activates when Sharpe > 1.5 confirmed on 30-day paper
- All 3 run simultaneously in paper at all times
- Real capital routes only to timeframes with confirmed positive expectancy in current regime
- Timeframes toggled at runtime from dashboard — change takes effect immediately

---

## Signal Validation Pipeline

- Labels generated via triple-barrier method (profit-taking · stop-loss · time-exit barriers)
- Validation via Combinatorial Purged Cross-Validation (CPCV) — standard k-fold invalid for time-series
- Minimum 500 out-of-sample trades before any timeframe goes live
- Out-of-sample Sharpe > 1.5 required to graduate paper → live
- Walk-forward re-validation runs weekly

---

## Risk System

- **Daily drawdown circuit breaker** — halts all trading at configurable % daily loss
- **Consecutive loss halt** — halts after N losing trades in a row (configurable)
- **Regime gate** — no new positions when HMM state = volatile
- **Max position cap** — hard ceiling as % of total capital (configurable)
- **Kelly floor** — position size = 0.5% flat when trade history < 10 samples
- **Half-Kelly multiplier** — reduces Kelly fraction by 50% for drawdown robustness
- All halt thresholds tunable via dashboard sliders at runtime
- Resume from halt requires explicit manual dashboard action

---

## Dashboard (<http://localhost:5173>)

- Equity curve — 30-day area chart
- Daily P&L % — bar chart
- Live signal feed — direction · confidence · meta score · regime · Kelly fraction
- Approval banners — Approve / Skip buttons for restricted + manual modes
- Execution mode toggle — AUTOMATIC / RESTRICTED / MANUAL
- Timeframe multi-select — SCALPING / INTRADAY / SWING
- Risk sliders — drawdown halt % · position cap % · restricted limit USD · consecutive loss halt
- Session metrics — equity · session P&L · regime · pending approvals
- Trade table — last 50 trades with entry/exit/P&L/mode
- Halt banner with resume button
- All changes take effect immediately via WebSocket → backend

---

## Stack

- Python 3.11+
- XGBoost 2.x — primary classifier + meta-label classifier
- hmmlearn — regime detection
- pandas + numpy + scipy + statsmodels — feature pipeline
- CCXT — unified Binance / OKX REST + WebSocket
- FastAPI + WebSocket — dashboard backend
- React + Recharts — dashboard frontend
- SQLite (aiosqlite) — trades · signals · equity history
- Vite — frontend dev server + production build

---

## Project Structure

```trade-bot/
├── src/
│   ├── config.py                  # all settings, hot-reloadable at runtime
│   ├── data/
│   │   ├── fetcher.py             # OHLCV history + real-time WebSocket streaming
│   │   └── storage.py             # SQLite: trades, signals, performance
│   ├── features/
│   │   └── pipeline.py            # feature matrix construction
│   ├── regime/
│   │   └── detector.py            # HMM 3-state regime classifier
│   ├── models/
│   │   └── trainer.py             # XGBoost primary + meta-label + CPCV
│   ├── risk/
│   │   ├── kelly.py               # Kelly criterion position sizing
│   │   └── gates.py               # pre-trade and session-level risk gates
│   ├── execution/
│   │   ├── paper.py               # paper trading with slippage simulation
│   │   └── live.py                # live order placement via CCXT
│   ├── engine/
│   │   ├── signal_engine.py       # per-(exchange, symbol, timeframe) signal loop
│   │   └── orchestrator.py        # manages engines + approval queue + execution routing
│   └── api/
│       └── main.py                # FastAPI backend + WebSocket hub
├── frontend/
│   ├── src/
│   │   ├── App.jsx                # complete single-screen dashboard
│   │   └── main.jsx
│   ├── index.html
│   ├── package.json
│   └── vite.config.js
├── models/                        # trained model artifacts (gitignored)
├── data/                          # SQLite db + WAL (gitignored)
├── pyproject.toml
├── .env                           # API keys + mode flag (gitignored)
├── .gitignore
├── ai_protocols.toml
└── README.md
```

---

## Setup

```bash
bash bootstrap.sh
```

**After bootstrap completes:**

```bash
# Terminal 1 — backend
source .venv/bin/activate
uvicorn src.api.main:app --port 8000 --reload

# Terminal 2 — frontend
cd frontend && npm run dev
```

- Dashboard: `http://localhost:5173`
- API: `http://localhost:8000`

---

## Requirements

- Python 3.11+
- Node.js 18+
- Internet connection (Binance / OKX public WebSocket for market data)
- API keys optional for paper mode — required only for live trading

---

## Environment Variables (.env)

```BINANCE_API_KEY=
BINANCE_API_SECRET=
OKX_API_KEY=
OKX_API_SECRET=
OKX_PASSPHRASE=
TRADING_MODE=paper
DASHBOARD_PORT=8000
FRONTEND_PORT=5173
```

---

## Live Trading Gate (30-Day Rule)

- System runs paper mode for 30 days minimum before live is considered
- Per-timeframe gate — each timeframe validates independently
- Gate criteria per timeframe:
  - Out-of-sample Sharpe > 1.5
  - Max drawdown < 15%
  - Win rate > 50% on minimum 100 trades
  - Live performance within 20% of backtest — otherwise model retrains
- Flip live: set `TRADING_MODE=live` in `.env` after gate is passed
- Capital allocation starts at 5% of total on first live session

---

## References

- López de Prado, M. (2018). *Advances in Financial Machine Learning*. Wiley.
- Chan, E. (2013). *Algorithmic Trading: Winning Strategies and Their Rationale*. Wiley.
- Kelly, J.L. (1956). A New Interpretation of Information Rate. *Bell System Technical Journal*, 35(4).
- Hamilton, J.D. (1989). A New Approach to the Economic Analysis of Nonstationary Time Series. *Econometrica*, 57(2).
