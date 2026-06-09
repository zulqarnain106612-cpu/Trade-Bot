# Trade Bot

Production algorithmic trading bot — Binance (primary) + OKX (secondary).

## Stack

Python 3.11+ · FastAPI · XGBoost · GaussianHMM · React + Vite · SQLite (WAL)

## Signal Architecture

| Layer | Implementation |

|---|---|
| Regime | GaussianHMM 3-state (ranging / trending / volatile) |
| Features | Fractional diff (d=0.4), VWAP dev, OFI, realized vol ratio, ATR momentum, rolling Sharpe, volume z-score |
| Direction | XGBoost classifier → P(long) |
| Meta-label | XGBoost gate → P(bet) |
| Labeling | Triple-barrier method (AFML Ch.3) |
| Validation | CPCV — Combinatorial Purged Cross-Validation (AFML Ch.7) |
| Sizing | Half-Kelly (multiplier=0.5, ceiling=0.25) |

## Risk Gates (hard limits)

- Daily drawdown halt: **2%**
- Consecutive loss halt: **3 trades**
- Regime gate: no new positions when state = **volatile**
- Max position size: **5% of capital**
- Default: **paper** — live requires `TRADING_MODE=live` in `.env`
- Live gate: OOS Sharpe > 1.5 · max DD < 15% · 500+ trades

## Timeframes

| Stream | Interval | Role |

|---|---|---|
| Scalping | 1m | Paper only |
| Intraday | 15m | Primary real-money |
| Swing | 4h | Paper only |

## Execution Modes (runtime switchable via dashboard or POST /execution-mode)

| Mode | Behaviour |

|---|---|
| AUTOMATIC | No approvals — fires within risk gates |
| RESTRICTED | Auto below notional limit; approval above; auto-skip on timeout |
| MANUAL | Every trade queued for explicit operator approval |

## Directory Structure

```src/
  config.py               Settings, enums, constants
  data/
    fetcher.py            ccxt OHLCV + order-book fetch
    storage.py            Async SQLite (bars, trades, regime, metrics, equity)
  features/
    pipeline.py           7-feature pipeline + triple-barrier labels
  regime/
    detector.py           GaussianHMM fit / predict / persist
  models/
    trainer.py            XGBoost direction + meta-label + CPCV
  risk/
    kelly.py              Half-Kelly sizing
    gates.py              All hard risk gates
  execution/
    paper.py              Paper executor (all 3 execution modes)
    live.py               Live executor (ccxt market orders)
  engine/
    signal_engine.py      Per-timeframe signal pipeline
    orchestrator.py       Main async event loop
  api/
    main.py               FastAPI REST + WebSocket dashboard API
frontend/
  src/
    App.jsx               React dashboard (equity chart, positions, approvals)
    main.jsx              Entry point
  index.html
  package.json
  vite.config.js
tests/
  test_risk_gates.py
  test_kelly.py
  test_features.py
```

## Setup

### 1. Python environment

```bash
cd D:\Trade-Bot\Trade-Bot
python -m venv .venv
.venv\Scripts\activate          # Windows
pip install -r requirements.txt
```

### 2. Environment file

Create `.env` in the project root:

```env
# Exchange credentials
BINANCE_API_KEY=your_key
BINANCE_API_SECRET=your_secret
BINANCE_TESTNET=true

OKX_API_KEY=your_key
OKX_API_SECRET=your_secret
OKX_PASSPHRASE=your_passphrase
OKX_TESTNET=true

# Trading
TRADING_MODE=paper          # change to 'live' only after 30+ paper days
EXECUTION_MODE=manual       # automatic | restricted | manual
PRIMARY_SYMBOL=BTC/USDT
STARTING_CAPITAL_USD=1000.0

# Risk overrides (optional — defaults shown)
RISK_DAILY_DRAWDOWN_HALT_PCT=2.0
RISK_CONSECUTIVE_LOSS_HALT=3
RISK_MAX_POSITION_SIZE_PCT=5.0
RISK_KELLY_MULTIPLIER=0.5
RISK_KELLY_CEILING=0.25

# API
API_HOST=0.0.0.0
API_PORT=8000
API_CORS_ORIGINS=["http://localhost:5173"]
```

### 3. Frontend

```bash
cd frontend
npm install
npm run dev          # http://localhost:5173
```

### 4. Backend

```bash
cd D:\Trade-Bot\Trade-Bot
uvicorn src.api.main:app --host 0.0.0.0 --port 8000 --reload
```

## Tests

```bash
pytest tests/ -v --tb=short
```

## Live Trading Checklist

Run this checklist before setting `TRADING_MODE=live`:

- [ ] ≥ 30 calendar days paper trading completed
- [ ] Direction model: OOS Sharpe > 1.5
- [ ] Direction model: max drawdown < 15%
- [ ] Direction model: ≥ 500 OOS trades
- [ ] Meta-label model: all same thresholds
- [ ] Both models persisted to `models/artifacts/`
- [ ] Both metrics rows have `live_gate_pass=1` in database
- [ ] `BINANCE_TESTNET=false` confirmed
- [ ] Risk parameters reviewed and unchanged
- [ ] `EXECUTION_MODE` set to `restricted` or `manual` for first live session

Set `TRADING_MODE=live` in `.env` — this is the only way to unlock live trading.

## References

- López de Prado (2018) *Advances in Financial Machine Learning* — Ch.3–5, 7, 10, 17
- Hamilton (1989) "A New Approach to the Economic Analysis of Nonstationary Time Series" — *Econometrica* 57(2)
- Kelly (1956) "A New Interpretation of Information Rate" — *Bell System Technical Journal* 35(4)
- Chan (2013) *Algorithmic Trading: Winning Strategies and Their Rationale*
- Chen & Guestrin (2016) "XGBoost: A Scalable Tree Boosting System"
