# Trade Bot

Production algorithmic trading bot — Binance (primary) + OKX (secondary) —
with a multi-source on-chain/exchange intelligence layer, self-tuning risk
parameters, and both a web dashboard and an Electron desktop app.

## Stack

Python 3.11+ (managed with [uv](https://github.com/astral-sh/uv)) · FastAPI ·
XGBoost · GaussianHMM · ccxt · React + Vite + Tailwind · Electron ·
SQLite (WAL) or TimescaleDB · structlog

## Signal Architecture

| Layer | Implementation |
|---|---|
| Regime | GaussianHMM 3-state (ranging / trending / volatile) |
| Features | Fractional diff (d=0.4), VWAP dev, OFI, realized vol ratio, ATR momentum, rolling Sharpe, volume z-score, intelligence-derived features |
| Direction | XGBoost classifier → P(long) |
| Meta-label | XGBoost gate → P(bet) |
| Labeling | Triple-barrier method (AFML Ch.3) |
| Validation | CPCV — Combinatorial Purged Cross-Validation (AFML Ch.7) |
| Sizing | Half-Kelly (multiplier=0.5, ceiling=0.25) + Carver forecast-scaled + AFML bet-size + Thorp variance-adjusted |
| Online adaptation | `src/models/online_trainer.py` — incremental SGD updates between full retrains. **Built and tested, not yet wired into the signal path** — no caller blends its prediction today. |

## Intelligence Layer

Aggregates exchange and on-chain signals to feed the feature pipeline and
risk gates (`src/intelligence/`, `src/features/intelligence_features.py`).
Fails open (safe fallback values + reduced confidence) if a provider is
unreachable or unkeyed — never blocks the core trading loop.

| Source | Provider | Notes |
|---|---|---|
| Exchange (funding, OI, basis, whale flow) | Binance, OKX | Free public REST via ccxt, no key required |
| On-chain | Arkham Intel, Dune Analytics, Coinglass, DeFiLlama | Free-tier keys, optional |
| On-chain (paid) | Glassnode, CryptoQuant | Optional; CryptoQuant funding-rate falls back to Binance perp if unset |
| Market cap / dominance | CoinGecko | Free |
| Ensemble | `src/intelligence/ensemble_predictor.py`, `probabilistic.py` | Combines provider signals, probabilistic calibration |
| Causal weighting | `src/intelligence/causal_inference.py` | **Experimental, not wired** — blocked on API key provisioning (DECISION_LOG GAP-015) |

`GET /intelligence/coverage` and `GET /intelligence/providers` report live
provider health and field coverage.

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
5. Exchange-stress / whale-activity gates (intelligence-derived)
6. Portfolio correlation gate (`src/risk/portfolio_correlation.py`)
7. Slippage veto (`src/risk/slippage.py`)
8. Paper minimum days: **30 days** before live is permitted
9. Live gate: OOS Sharpe > 1.5 · max DD < 15% · 500+ trades

Default: **paper** — live requires `TRADING_MODE=live` in `.env`.
`src/risk/cognitive_engine.py` and `performance_drift.py` continuously
monitor for behavioral drift and degrade sizing/confidence rather than
hard-failing.

## Self-Tuning (optional, off by default)

`src/tuning/` runs a bounded auto-tuning loop over selected risk/HMM
parameters (`AutoTuningScheduler`, `bayesian_proposer.py` using Optuna TPE,
or a simpler `random_walk` proposer). Gated by `SELF_TUNING_ENABLED` (master
kill switch, default `false`) and `SELF_TUNING_SHADOW_MODE` (default `true`
— accepted changes are logged as `WOULD_PROMOTE` but never applied until
explicitly disabled). A watchdog (`src/tuning/watchdog.py`) puts newly
promoted values on probation and can roll them back automatically. See
`GET /self-tuning/status`, `POST /self-tuning/pause`, `POST
/self-tuning/resume`, `POST /self-tuning/rollback/{param_name}`.

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

Orders are tracked through an explicit finite-state machine
(`src/execution/order_fsm.py`, `order_manager.py`) accounting for partial
fills, reconnects, and exchange-side rejections.

## API Endpoints

| Method | Path | Description |
|---|---|---|
| GET | /health | Storage counts, trading/execution mode |
| GET | /metrics | Prometheus metrics |
| GET | /status | Equity, positions, regime, pending approvals |
| GET | /trades | Paginated trade history |
| GET | /missed-trades | Trades filtered out by a risk gate (audit trail) |
| GET | /equity | Equity curve for charting |
| GET | /regime/{timeframe} | Latest HMM regime snapshot |
| GET | /approvals | Pending approval requests |
| POST | /approvals/{request_id}/resolve | Approve or reject a trade |
| POST | /execution-mode | Switch execution mode (requires OPERATOR_SECRET) |
| GET / POST | /risk-controls | View/adjust live risk parameters |
| GET | /model-metrics | OOS metrics + live gate status |
| GET | /self-tuning/status | Self-tuning scheduler state |
| POST | /self-tuning/pause \| /resume \| /rollback/{param_name} | Self-tuning controls |
| WS | /ws | Live push — equity, positions, signals |
| GET | /orders/{order_id}/status | Order FSM status |
| GET | /ledger | Cross-venue unified book (net/gross exposure, margin) |
| GET | /recovery/status | Startup reconciliation state (live vs exchange) |
| POST | /recovery/acknowledge | Clear the reconciliation block (requires OPERATOR_SECRET) |
| GET | /performance-drift | Behavioral/performance drift snapshot |
| GET | /intelligence/coverage | Intelligence feature coverage report |
| GET | /intelligence/providers | Intelligence provider health |
| GET | /debug/health | Runtime monitor snapshot |
| GET | /debug/audit | Trade decision audit log |
| GET | /debug/drift | Feature drift (KS test) + model degradation |
| POST | /debug/selftest | On-demand pipeline self-test |
| GET | /debug/reconcile | In-memory book vs persisted open trades (crash recovery) |
| GET | /strategies/attribution | Per-strategy P&L attribution |
| GET | /strategies/allocation | Performance-weighted capital allocation |
| GET | /strategies/gauntlet | Promotion-gauntlet status per strategy candidate |

All endpoints require `X-API-Key` header. A key set in `API_READONLY_KEY`
authenticates the same way but is refused with `403` on the four mutating
routes (`/execution-mode`, `/risk-controls`, `/approvals/{id}/resolve`,
`/self-tuning/*`) — see [API roles](#api-roles).

## Diagnostics

- `RuntimeMonitor` — async background probe polling (30s), tick-stall detection (5min), memory leak alerts (512MB warn / 1GB critical), dead-task scan
- `TradeAuditor` — per-tick decision log with features, probabilities, gate chain, outcome
- `SignalDebugger` — KS-test feature drift vs training baseline, model degradation tracker
- `PerformanceDriftDetector` — behavioral drift vs a rolling performance baseline
- Pipeline self-test on startup — synthetic round-trip through feature pipeline

## Storage

Dual backend, selected via `STORAGE_BACKEND` (`sqlite` default, or
`timescale`):

- **SQLite** (`src/data/storage.py`) — embedded, WAL mode, default for tests/dev, zero setup.
- **TimescaleDB** (`src/data/timescale_storage.py`) — local container via `scripts/timescaledb.sh` (rootless podman/docker), for higher-volume/production use. `STORAGE_TIMESCALE_DSN` configures the connection.

Both implement the same schema: bars, trades, regime snapshots, model
metrics, equity curve, audit log, intelligence feature history, missed
trades.

## Directory Structure

```
src/
  config.py               Settings (pydantic-settings), enums, RuntimeConfig
  data/
    fetcher.py            ccxt OHLCV + order-book fetch
    storage.py             Async SQLite backend
    timescale_storage.py   Async TimescaleDB backend (same schema)
  features/
    pipeline.py            Feature pipeline + triple-barrier labels
    intelligence_features.py  Intelligence-derived feature adapters
  intelligence/
    client.py               Aggregator entrypoint used by the feature pipeline
    providers/               Exchange providers: binance, okx, coingecko, blockchain.info
    onchain/                 On-chain providers: arkham, dune, coinglass, defillama, cryptoquant
    ensemble_predictor.py, causal_inference.py, probabilistic.py, calibration.py, risk_quantification.py
  regime/
    detector.py             GaussianHMM fit / predict / persist
  models/
    trainer.py               XGBoost direction + meta-label + CPCV
    online_trainer.py         Incremental updates between full retrains
  risk/
    kelly.py                 Half-Kelly sizing
    gates.py                 All hard risk gates + DrawdownTracker
    cognitive_engine.py       Behavioral drift monitoring
    performance_drift.py      Performance baseline drift detection
    portfolio_correlation.py  Cross-position correlation gate
    slippage.py               Slippage veto
  execution/
    base.py                 AbstractExecutor interface
    paper.py                 Paper executor (all 3 execution modes)
    live.py                  Live executor (ccxt market orders)
    order_fsm.py             Order lifecycle state machine
    order_manager.py          Order tracking/reconciliation
  strategies/
    filters.py               8 professional signal filters
    position_sizing.py       Carver / AFML / Thorp sizing methods
  tuning/
    scheduler.py, proposer.py, bayesian_proposer.py, evaluator.py, gate.py,
    watchdog.py, registry.py, store.py   Self-tuning subsystem (opt-in)
  engine/
    signal_engine.py         Per-timeframe signal pipeline
    orchestrator.py           Main async event loop
  api/
    main.py                  FastAPI REST + WebSocket
    auth.py                   API key + WS key verification
    middleware.py              CORS validation
    metrics.py                  Prometheus metrics endpoint
  diagnostics/
    runtime_monitor.py        Async health monitor
    signal_debugger.py         Feature drift + model degradation
    trade_auditor.py            Per-tick decision auditing
frontend/
  src/
    App.jsx                  React dashboard (equity chart, positions, approvals)
    main.jsx                  Entry point
  electron/                  Electron main process (desktop app)
  tailwind.config.js
  vite.config.js
tests/                       79+ test modules, pytest-asyncio, pytest-cov
scripts/
  timescaledb.sh             Local TimescaleDB container lifecycle
  check_coverage_floors.py    Per-file coverage floor enforcement (CI)
```

## Setup

### 1. Python environment (uv)

```bash
uv sync                       # installs from requirements.lock (hash-verified)
```

`requirements.txt` / `requirements.lock` are runtime deps;
`requirements-dev.txt` adds lint/type/test/security tooling
(ruff, mypy, pytest, pytest-cov, detect-secrets, pip-tools).
Regenerate the lockfile with:
`pip-compile --allow-unsafe --generate-hashes requirements.in -o requirements.lock`

### 2. Environment file

Copy `.env.example` to `.env` and fill in credentials. Key sections:

- **Exchange**: `BINANCE_API_KEY` / `BINANCE_API_SECRET` / `BINANCE_TESTNET`, `OKX_API_KEY` / `OKX_API_SECRET` / `OKX_PASSPHRASE`
- **Security**: `API_SECRET_KEY`, `OPERATOR_SECRET` (generate with `openssl rand -hex 32`).
  Optional `API_READONLY_KEY` — a second key that authenticates but resolves to the
  read-only role, so the mutating endpoints (`/approvals/{id}/resolve`,
  `/execution-mode`, `/risk-controls`, `/self-tuning/*`) answer 403 for it. Leave it
  unset for a single-key deployment; `API_SECRET_KEY` keeps full authority either way.
  It must differ from `API_SECRET_KEY` and meet the same 32-character minimum, or the
  API fails closed with 503.
- **Trading**: `TRADING_MODE` (`paper`/`live`), `EXECUTION_MODE`, `PRIMARY_SYMBOL`, `STARTING_CAPITAL_USD`
- **Risk overrides** (optional, defaults shown above): `RISK_DAILY_DRAWDOWN_HALT_PCT`, `RISK_CONSECUTIVE_LOSS_HALT`, `RISK_MAX_POSITION_SIZE_PCT`, `RISK_KELLY_MULTIPLIER`, `RISK_KELLY_CEILING`
- **Storage**: `STORAGE_BACKEND` (`sqlite`/`timescale`), `STORAGE_TIMESCALE_DSN`
- **Intelligence** (all optional, fail-open if unset): `INTELLIGENCE_GLASSNODE_API_KEY`, `INTELLIGENCE_CRYPTOQUANT_API_KEY`, `INTELLIGENCE_ARKHAM_API_KEY`, `INTELLIGENCE_DUNE_API_KEY`, `INTELLIGENCE_COINGLASS_API_KEY`
- **Self-tuning** (optional, off by default): `SELF_TUNING_ENABLED`, `SELF_TUNING_SHADOW_MODE`, `SELF_TUNING_DECISION_LOG_PATH` (Markdown journal of live promotions; empty disables)
- **Options Greeks caps** (optional, both or neither): `STRATEGY_OPTIONS_CARRY_MAX_ABS_DELTA`, `STRATEGY_OPTIONS_CARRY_MAX_ABS_VEGA` — book-level ceilings the options-carry strategy checks before selling premium, since Kelly sizes on notional and cannot see an option's non-linear exposure
- **API**: `API_HOST`, `API_PORT`, `API_CORS_ORIGINS`

#### API roles

Every request carries `X-API-Key`. The key determines the caller's role:

| Key | Role | Can do |
| --- | --- | --- |
| `API_SECRET_KEY` | `trade_authorizing` | everything |
| `API_READONLY_KEY` (optional) | `read_only` | GET routes + `/ws` only |

A `read_only` key gets `403` on `POST /execution-mode`, `POST /risk-controls`,
`POST /approvals/{id}/resolve`, and the `/self-tuning/*` mutations. Leave
`API_READONLY_KEY` unset for a single-key deployment — behaviour is then
identical to a deployment with no roles at all. When set it must be at least
32 characters and must differ from `API_SECRET_KEY`; otherwise the API fails
closed with `503` rather than silently collapsing the two roles.

Roles are *not* a substitute for `OPERATOR_SECRET`: mode and risk changes
still require that second factor on top of a trade-authorizing key.

### 3. Backend

```bash
uv run uvicorn src.api.main:app --host 127.0.0.1 --port 8000 --reload
```

### 4. TimescaleDB (optional — only if STORAGE_BACKEND=timescale)

```bash
bash scripts/timescaledb.sh up       # rootless podman/docker, binds 127.0.0.1:5433 only
```

### 5. Frontend (web dashboard)

```bash
cd frontend
npm install
npm run dev          # http://localhost:5173
```

### 6. Frontend (Electron desktop app)

```bash
cd frontend
npm run electron:dev     # dev mode: vite + electron together
npm run electron:build   # packaged build via electron-builder
```

## Tests

```bash
uv run pytest tests/ -x -q
```

- Global coverage gate: **95%** (`--cov-fail-under=95`, enforced in `pyproject.toml`)
- Per-file floors (stricter, safety-critical paths — `scripts/check_coverage_floors.py`): `src/execution/live.py` 75%, `paper.py`/`order_fsm.py`/`order_manager.py` 70%, `src/engine/orchestrator.py` 60%, `signal_engine.py` 65%, `src/risk/gates.py` 70%, `cognitive_engine.py` 65%, `kelly.py` 70%, `src/diagnostics/runtime_monitor.py` 50%
- `tests/test_timescale_storage.py` requires a reachable TimescaleDB (`scripts/timescaledb.sh up` locally; a service container in CI) — self-skips otherwise

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
- [ ] `SELF_TUNING_SHADOW_MODE=true` (or self-tuning disabled) unless the shadow soak + watchdog have been reviewed

Set `TRADING_MODE=live` in `.env` — the only way to unlock live trading.

## CI / Tooling

- `uv` — Python dependency management (never bare `pip`/`python3` for app code)
- Ruff — lint + format (replaces black/flake8/isort)
- mypy — type checking
- bandit, semgrep, CodeQL — SAST
- detect-secrets — baseline-gated secret scanning (`.secrets.baseline`)
- pip-audit — dependency CVE scanning
- pytest-cov — coverage gate (95% global + per-file floors)
- GitHub Actions workflows: `ci.yml` (lint/type/test + frontend build),
  `security.yml` (Bandit/Semgrep/TruffleHog/pip-audit), `codeql.yml`,
  `mutation-testing.yml`, `auto-fix.yml` / `auto-debug.yml` (automated
  lint-fix and failure triage), `claude-code-review.yml`,
  `dependabot-auto-merge.yml`, `release.yml`
- Pre-commit hooks: `.pre-commit-config.yaml`

## References

- López de Prado (2018) *Advances in Financial Machine Learning* — Ch.3–5, 7, 10, 16, 17
- Hamilton (1989) "A New Approach to the Economic Analysis of Nonstationary Time Series" — *Econometrica* 57(2)
- Kelly (1956) "A New Interpretation of Information Rate" — *Bell System Technical Journal* 35(4)
- Chan (2013) *Algorithmic Trading: Winning Strategies and Their Rationale*
- Chen & Guestrin (2016) "XGBoost: A Scalable Tree Boosting System"
- Carver (2019) *Systematic Trading*
- Peters (1994) *Fractal Market Hypothesis*
- Thorp (2006) "The Kelly Criterion in Blackjack, Sports Betting and the Stock Market"
