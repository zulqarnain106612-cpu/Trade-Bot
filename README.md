# Trade Bot

Production algorithmic trading bot — Binance (primary) + OKX (secondary).

## Stack

Python 3.11+ · FastAPI · XGBoost · GaussianHMM · React + Vite + Tailwind · SQLite (WAL)

## Signal Architecture

| Layer | Implementation |
|---|---|
| Regime | GaussianHMM 3-state (ranging / trending / volatile) |
| Features | Fractional diff (d=0.4), VWAP dev, OFI, realized vol ratio, ATR momentum, rolling Sharpe, volume z-score |
| Direction | XGBoost classifier → P(long) |
| Meta-label | XGBoost gate → P(bet) |
| Labeling | Triple-barrier method (AFML Ch.3) |
| Validation | CPCV — Combinatorial Purged Cross-Validation (AFML Ch.7) |
| Sizing | Half-Kelly (multiplier=0.5, ceiling=0.25) + Carver forecast-scaled + AFML bet-size + Thorp variance-adjusted |

## Strategy Filters (applied before execution)

| Filter | Authority |
|---|---|
| EWM trend filter | Carver (2019) Ch.3 |
| Volatility-adjusted momentum | Chan (2013) Ch.4 |
| Overnight gap filter | Aronson (2006) Ch.8 |
| Regime-aware position scalar | AFML Ch.17 |
| Hurst exponent (H > 0.55) | Peters (1994) |
| OBV direction confirmation | Granville / Elder |
| Volatility explosion gate (2× median ATR) | Schwager (1984) |
| Multi-timeframe trend alignment | Schwager (1993) |

## Risk Gates (hard limits — sequential, short-circuit on first fail)

1. Daily drawdown halt: **2%** of starting equity
2. Consecutive loss halt: **3 trades**
3. Regime gate: no new positions when state = **volatile**
4. Max position size: **5% of capital**
5. Paper minimum days: **30 days** before live is permitted
6. Live gate: OOS Sharpe > 1.5 · max DD < 15% · 500+ trades

Default: **paper** — live requires `TRADING_MODE=live` in `.env`

## Timeframes

| Stream | Interval | Role |
|---|---|---|
| Scalping | 1m | Paper only |
| Intraday | 15m | Primary real-money |
| Swing | 4h | Paper only |

## Execution Modes (runtime switchable via POST /execution-mode)

| Mode | Behaviour |
|---|---|
| AUTOMATIC | No approvals — fires within risk gates |
| RESTRICTED | Auto below notional limit; approval above; auto-skip on timeout (30s default) |
| MANUAL | Every trade queued for explicit operator approval |

## API Endpoints

| Method | Path | Description |
|---|---|---|
| GET | /health | Storage counts, trading/execution mode |
| GET | /status | Equity, positions, regime, pending approvals |
| GET | /trades | Paginated trade history |
| GET | /equity | Equity curve for charting |
| GET | /regime/{timeframe} | Latest HMM regime snapshot |
| GET | /approvals | Pending approval requests |
| POST | /approvals/{id}/resolve | Approve or reject a trade |
| POST | /execution-mode | Switch execution mode (requires OPERATOR_SECRET) |
| GET | /model-metrics | OOS metrics + live gate status |
| WS | /ws | Live push — equity, positions, signals |
| GET | /debug/health | Runtime monitor snapshot |
| GET | /debug/audit | Trade decision audit log |
| GET | /debug/drift | Feature drift (KS test) + model degradation |
| POST | /debug/selftest | On-demand pipeline self-test |

All endpoints require `X-API-Key` header.

## Diagnostics

- `RuntimeMonitor` — async background probe polling (30s), tick-stall detection (5min), memory leak alerts (512MB warn / 1GB critical), dead-task scan
- `TradeAuditor` — per-tick decision log with features, probabilities, gate chain, outcome
- `SignalDebugger` — KS-test feature drift vs training baseline, model degradation tracker
- Pipeline self-test on startup — synthetic round-trip through feature pipeline

## Directory Structure

```
src/
  config.py               Settings (pydantic-settings), enums, RuntimeConfig
  data/
    fetcher.py            ccxt OHLCV + order-book fetch
    storage.py            Async SQLite (bars, trades, regime, metrics, equity, audit)
  features/
    pipeline.py           7-feature pipeline + triple-barrier labels
  regime/
    detector.py           GaussianHMM fit / predict / persist
  models/
    trainer.py            XGBoost direction + meta-label + CPCV
  risk/
    kelly.py              Half-Kelly sizing
    gates.py              All hard risk gates + DrawdownTracker
  execution/
    base.py               AbstractExecutor interface
    paper.py              Paper executor (all 3 execution modes)
    live.py               Live executor (ccxt market orders)
  strategies/
    filters.py            8 professional signal filters
    position_sizing.py    Carver / AFML / Thorp sizing methods
  engine/
    signal_engine.py      Per-timeframe signal pipeline
    orchestrator.py       Main async event loop
  api/
    main.py               FastAPI REST + WebSocket
    auth.py               API key + WS key verification
    middleware.py         CORS validation
  diagnostics/
    runtime_monitor.py    Async health monitor
    signal_debugger.py    Feature drift + model degradation
    trade_auditor.py      Per-tick decision auditing
frontend/
  src/
    App.jsx               React dashboard (equity chart, positions, approvals)
    main.jsx              Entry point
  tailwind.config.js
  vite.config.js
tests/
  test_risk_gates.py
  test_kelly.py
  test_features.py
```

## Setup

### 1. Python environment

```bash
python -m venv .venv
source .venv/bin/activate          # Linux/macOS
# .venv\Scripts\activate           # Windows
pip install -r requirements.txt
```

### 2. Environment file

Copy `.env.example` to `.env` and fill in:

```env
# Exchange credentials
BINANCE_API_KEY=your_key
BINANCE_API_SECRET=your_secret
BINANCE_TESTNET=true

OKX_API_KEY=your_key
OKX_API_SECRET=your_secret
OKX_PASSPHRASE=your_passphrase

# Security — generate with: openssl rand -hex 32
API_SECRET_KEY=<strong_random_secret>
OPERATOR_SECRET=<strong_random_secret>

# Trading
TRADING_MODE=paper
EXECUTION_MODE=manual
PRIMARY_SYMBOL=BTC/USDT
STARTING_CAPITAL_USD=1000.0

# Risk overrides (optional — defaults shown)
RISK_DAILY_DRAWDOWN_HALT_PCT=2.0
RISK_CONSECUTIVE_LOSS_HALT=3
RISK_MAX_POSITION_SIZE_PCT=5.0
RISK_KELLY_MULTIPLIER=0.5
RISK_KELLY_CEILING=0.25

# API
API_HOST=127.0.0.1
API_PORT=8000
API_CORS_ORIGINS=["http://localhost:5173"]
```

### 3. Backend

```bash
uvicorn src.api.main:app --host 127.0.0.1 --port 8000 --reload
```

### 4. Frontend

```bash
cd frontend
npm install
npm run dev          # http://localhost:5173
```

## Tests

```bash
pytest tests/ -v --tb=short
```

Coverage minimum: 60% (enforced in CI).

## Live Trading Checklist

- [ ] ≥ 30 calendar days paper trading completed
- [ ] Direction model: OOS Sharpe > 1.5, max DD < 15%, ≥ 500 OOS trades
- [ ] Meta-label model: same thresholds
- [ ] Both models persisted to `models/artifacts/`
- [ ] Both metrics rows have `live_gate_pass=1` in database
- [ ] `BINANCE_TESTNET=false` confirmed
- [ ] `API_SECRET_KEY` and `OPERATOR_SECRET` set to strong random values
- [ ] Server bound to loopback or behind TLS-terminating reverse proxy
- [ ] Risk parameters reviewed
- [ ] `EXECUTION_MODE=restricted` or `manual` for first live session

Set `TRADING_MODE=live` in `.env` — the only way to unlock live trading.

## CI / Tooling

- Ruff — lint + format (replaces black/flake8/isort)
- mypy — type checking
- bandit — SAST
- semgrep — custom security rules (`.semgrep/rules.yml`)
- pytest-cov — coverage gate (60%)
- GitHub Actions: `ci.yml`, `codeql.yml`, `security.yml`, `release.yml`
- Pre-commit hooks: `.pre-commit-config.yaml`
- detect-secrets baseline: `.secrets.baseline`

## References

- López de Prado (2018) *Advances in Financial Machine Learning* — Ch.3–5, 7, 10, 16, 17
- Hamilton (1989) "A New Approach to the Economic Analysis of Nonstationary Time Series" — *Econometrica* 57(2)
- Kelly (1956) "A New Interpretation of Information Rate" — *Bell System Technical Journal* 35(4)
- Chan (2013) *Algorithmic Trading: Winning Strategies and Their Rationale*
- Chen & Guestrin (2016) "XGBoost: A Scalable Tree Boosting System"
- Carver (2019) *Systematic Trading*
- Peters (1994) *Fractal Market Hypothesis*
- Thorp (2006) "The Kelly Criterion in Blackjack, Sports Betting and the Stock Market"
