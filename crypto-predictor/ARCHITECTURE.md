# Crypto Price Prediction & Decision-Support Engine
## Architecture, Roadmap, and Reference Document

---

## 0. Scope, Honesty Constraints, and Success Definition

**What this system is:** a research-grade decision-support engine that ingests market
data, computes features/indicators, produces probabilistic forecasts, backtests them
rigorously, and outputs risk-adjusted signals with confidence intervals.

**What this system is NOT:** a guaranteed profit generator. No system — mine or any
quant fund's — beats the market with certainty. This is not financial advice; I am
not a financial advisor or licensed trader. The goal is to build the *best
mathematically honest* estimator, with variance and error bounds reported
alongside every output, not to promise returns.

**Realistic success definition (from published quant literature):**
- Directional accuracy target: **55-60%** on out-of-sample data (anything claiming
  90%+ sustained accuracy is overfit or fraudulent)
- Sharpe ratio target: **1.0-2.0** after fees/slippage (institutional quant funds
  average ~1-3; retail-accessible systems rarely sustain >2)
- Primary value: **consistent risk-adjusted decision-making**, not prediction alone

---

## 1. System Architecture (High Level)

```
┌─────────────────────────────────────────────────────────────────────┐
│                         DATA INGESTION LAYER                         │
│  Exchange WebSocket/REST │ On-chain indexers │ News/Sentiment feeds  │
└───────────────────────────────┬───────────────────────────────────────┘
                                 ▼
┌─────────────────────────────────────────────────────────────────────┐
│                       STORAGE / MESSAGE LAYER                        │
│   Kafka/Redpanda (stream) │ TimescaleDB (time-series) │ Redis (cache)│
└───────────────────────────────┬───────────────────────────────────────┘
                                 ▼
┌─────────────────────────────────────────────────────────────────────┐
│                     FEATURE ENGINEERING LAYER                        │
│  Technical indicators │ Order-flow features │ On-chain metrics       │
│  Sentiment scores │ Macro correlation features                      │
└───────────────────────────────┬───────────────────────────────────────┘
                                 ▼
┌─────────────────────────────────────────────────────────────────────┐
│                        MODELING LAYER (ENSEMBLE)                     │
│  Statistical (ARIMA/GARCH) │ ML (XGBoost/LightGBM)                   │
│  Deep sequence (LSTM/Transformer) │ Order-flow microstructure model  │
│           └──────────────► Meta-learner / stacking ◄──────────┘      │
└───────────────────────────────┬───────────────────────────────────────┘
                                 ▼
┌─────────────────────────────────────────────────────────────────────┐
│                   VALIDATION & BACKTEST ENGINE                       │
│  Walk-forward validation │ Purged K-fold CV │ Monte Carlo stress     │
│  Slippage/fee-adjusted P&L │ Regime-change robustness tests          │
└───────────────────────────────┬───────────────────────────────────────┘
                                 ▼
┌─────────────────────────────────────────────────────────────────────┐
│                      RISK MANAGEMENT LAYER                           │
│  Position sizing (Kelly-fractional) │ Stop-loss/ATR-based │          │
│  Max drawdown circuit breakers │ Portfolio correlation limits        │
└───────────────────────────────┬───────────────────────────────────────┘
                                 ▼
┌─────────────────────────────────────────────────────────────────────┐
│                 SIGNAL / DECISION AGGREGATION ENGINE                 │
│  Confidence-weighted signal fusion │ Human-readable rationale output │
└───────────────────────────────┬───────────────────────────────────────┘
                                 ▼
┌─────────────────────────────────────────────────────────────────────┐
│              EXECUTION LAYER (paper-trade first, always)             │
│  Exchange order API │ Dry-run/simulation mode │ Kill-switch           │
└───────────────────────────────┬───────────────────────────────────────┘
                                 ▼
┌─────────────────────────────────────────────────────────────────────┐
│                    OBSERVABILITY / MONITORING                        │
│  Prediction-vs-actual drift tracking │ Model decay alerts │ Logging  │
└─────────────────────────────────────────────────────────────────────┘
```

---

## 2. Layer-by-Layer Design

### 2.1 Data Ingestion Layer

| Source | Method | Data | Update frequency |
|---|---|---|---|
| Binance/Coinbase/Kraken | WebSocket | Order book, trades, OHLCV | Real-time (ms) |
| CoinGecko/CMC | REST | Aggregated price, market cap | 30-60s |
| On-chain (Glassnode API, or self-hosted node + The Graph) | REST/GraphQL | Exchange flows, active addresses, whale txns | Per block / hourly |
| News/Twitter (X API, RSS) | REST/stream | Sentiment source text | Continuous |

**Failure handling:** every source needs reconnect-with-backoff, heartbeat
monitoring, and a fallback source (never single-point-of-failure on price feed —
a bad tick from one exchange must not corrupt the model input).

### 2.2 Storage Layer

- **Kafka/Redpanda**: durable stream buffer between ingestion and processing —
  decouples producers/consumers, allows replay for backtesting on the exact
  historical stream
- **TimescaleDB** (Postgres extension): time-series storage, native OHLCV
  bucketing, retention policies
- **Redis**: hot cache for latest order book state, feature vectors — sub-ms
  read latency for the live decision path

### 2.3 Feature Engineering Layer

Reuses everything discussed earlier (SMA/EMA/MACD/RSI/Bollinger/ATR/OBV) plus:
- **Order-book imbalance** (highest-value short-horizon feature per microstructure literature)
- **On-chain**: exchange net flow (accumulation vs distribution signal), miner reserves
- **Sentiment embeddings**: FinBERT or CryptoBERT-style transformer sentiment scores, not just keyword counting
- **Cross-asset correlation**: BTC dominance, DXY, S&P 500 futures correlation (crypto increasingly correlates with macro risk assets)

**Critical engineering rule: no look-ahead bias.** Every feature must be computable
using *only* information available at time `t`. This is the single most common
reason backtests look good and live systems fail.

### 2.4 Modeling Layer (Ensemble, not single model)

No single model type is used alone — an ensemble/meta-learner combines:

1. **GARCH** — volatility regime estimate (crypto volatility clusters heavily)
2. **XGBoost/LightGBM** — tabular features (indicators + on-chain + sentiment) → probability of direction, with SHAP values for interpretability
3. **LSTM or Temporal Fusion Transformer** — raw sequence pattern capture
4. **Order-flow microstructure model** — short-horizon (seconds-minutes) prediction from book imbalance
5. **Meta-learner (logistic regression or small MLP)** — combines the above model outputs into a final calibrated probability, weighted by each sub-model's *recent* rolling accuracy (not fixed weights — models that are decaying get down-weighted automatically)

**Output is always a probability distribution, never a point prediction** —
e.g. "62% probability of upward move in next 4h, with a 90% confidence interval
of ±1.8%" — never "price will be $X."

### 2.5 Validation & Backtest Engine (this is the most important layer — most systems fail here)

- **Walk-forward validation**: train on window [t0,t1], test on [t1,t2], roll forward — never train on future data
- **Purged K-fold CV** (López de Prado method): standard K-fold leaks information in time-series; purging removes overlapping samples between train/test
- **Combinatorial Purged Cross-Validation (CPCV)**: generates multiple realistic backtest paths instead of one, exposes overfitting via Probability of Backtest Overfitting (PBO) metric
- **Transaction cost + slippage modeling**: every backtest must subtract realistic fees (exchange fee tier) and slippage (spread × trade size / liquidity) — a strategy profitable before costs and unprofitable after costs is the most common false-positive
- **Regime stress testing**: explicitly test performance across bull/bear/sideways/high-volatility historical regimes separately, not just aggregate — a model that only works in trending markets will be reported honestly as such

### 2.6 Risk Management Layer

- **Position sizing**: fractional Kelly criterion (half-Kelly or quarter-Kelly in practice — full Kelly is too volatile even when the edge estimate is correct, and edge estimates are never fully certain)
- **Stop-loss**: ATR-based dynamic stops (not fixed %, adapts to current volatility)
- **Max drawdown circuit breaker**: system auto-halts trading/signals if drawdown exceeds a pre-set threshold (e.g. 15%), forcing manual review before resuming
- **Correlation limits**: if running multiple assets, caps total exposure to correlated positions (BTC+ETH+SOL often move together — this isn't real diversification)

### 2.7 Signal Aggregation / Decision Engine

Converts model probability + risk layer into a human-readable decision object:

```json
{
  "asset": "BTC/USDT",
  "timestamp": "2026-07-27T14:00:00Z",
  "direction_probability": {"up": 0.58, "down": 0.42},
  "confidence_interval_90": [-1.2, 2.4],
  "model_agreement": "3/4 sub-models agree on direction",
  "recommended_position_size_pct": 2.1,
  "stop_loss_price": 42150.00,
  "rationale": "Order-flow imbalance positive, RSI neutral, GARCH indicates rising volatility, on-chain shows net accumulation",
  "recent_model_accuracy_rolling_30d": 0.564
}
```

This is designed so **every output is auditable** — you can see *why* a signal
fired, not just a black-box number.

### 2.8 Execution Layer

- **Paper trading mode is default and mandatory** for minimum 60-90 days before any real capital, matching standard quant practice
- Kill-switch: instant manual override to flatten all positions
- Exchange API integration only added after paper-trading validation is complete

### 2.9 Observability

- Continuous tracking of **prediction vs. realized outcome** — plotted drift over time
- **Model decay alerts**: if rolling accuracy drops below a threshold (e.g., 2 std devs below backtest average), auto-flag for retraining/review — markets change regime, and a static model silently decays
- Full logging of every decision + underlying feature vector, for auditability and retraining datasets

---

## 3. Tech Stack Recommendation

| Layer | Recommended | Why |
|---|---|---|
| Language | Python 3.11+ | Ecosystem maturity (pandas, sklearn, pytorch) |
| Streaming | Kafka or Redpanda | Durable, replayable, industry standard |
| Time-series DB | TimescaleDB | Postgres-compatible, native time bucketing |
| Cache | Redis | Sub-ms hot-path reads |
| ML | XGBoost/LightGBM, PyTorch (LSTM/Transformer) | Best-in-class for tabular + sequence respectively |
| Backtesting | `vectorbt` or custom (López de Prado methods not in most off-the-shelf libs) | Speed + correctness |
| Orchestration | Airflow or Prefect | Scheduled retraining, feature pipeline DAGs |
| Deployment | Docker + Kubernetes (or single-VM Docker Compose for MVP) | Reproducibility |
| Monitoring | Grafana + Prometheus | Standard observability stack |

---

## 4. Roadmap (Phased, each phase independently valuable)

### Phase 1 — Data Foundation (Week 1-2)
- Exchange WebSocket ingestion (1 exchange, 1-2 assets to start: BTC, ETH)
- TimescaleDB schema + historical backfill (at least 2-3 years for backtesting)
- Basic OHLCV candle construction + storage validation

### Phase 2 — Feature Engineering (Week 2-3)
- Implement indicator library (SMA/EMA/MACD/RSI/Bollinger/ATR/OBV)
- Order-book imbalance feature from live book snapshots
- On-chain data integration (start with free-tier Glassnode or public node)

### Phase 3 — Baseline Modeling + Honest Backtest (Week 3-5)
- Start with **one simple model** (XGBoost on engineered features) — resist the urge to build the full ensemble before proving the pipeline is leak-free
- Implement walk-forward validation + transaction cost modeling FIRST — before trusting any accuracy number
- Establish the honest baseline accuracy number before adding complexity

### Phase 4 — Ensemble + Meta-learner (Week 5-7)
- Add GARCH, LSTM/Transformer, order-flow model
- Build meta-learner combining sub-model outputs with rolling-accuracy weighting

### Phase 5 — Risk Layer + Paper Trading (Week 7-9)
- Kelly-fractional position sizing, ATR stops, drawdown circuit breaker
- Deploy in paper-trading mode — minimum 60-90 days observation before considering real capital

### Phase 6 — Monitoring + Iteration (Ongoing)
- Drift detection, decay alerts, continuous retraining pipeline
- Regular regime-stress re-validation as market conditions evolve

---

## 5. Key References (for verification, not blind trust)

- López de Prado, M. — *Advances in Financial Machine Learning* (2018) — the standard reference for purged CV, CPCV, and avoiding backtest overfitting in finance ML
- Shannon, C. — foundational information theory (relevant to feature entropy considerations)
- Easley, D., López de Prado, M., O'Hara, M. — market microstructure / order-flow toxicity literature (VPIN metric)
- Binance/Coinbase official API docs — for exact rate limits, WebSocket schemas
- Glassnode Academy — on-chain metric definitions
- `vectorbt` and `backtrader` documentation — backtesting engine references

---

## 6. Explicit Limitations (read before building)

- No model here will reliably predict short-term price direction above the
  55-60% ceiling seen in published literature. Anyone or anything claiming
  otherwise on a sustained basis is either overfitting or lying.
- Backtested performance is not a promise of future performance — regime
  change is the most common cause of live underperformance vs. backtest.
- This system is a decision-support tool. The recommended architecture
  defaults to paper-trading and human review at every stage before real
  capital is ever at risk.
