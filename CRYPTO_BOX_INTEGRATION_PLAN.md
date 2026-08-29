# Crypto-Box → Trade-Bot Integration Master Plan

**Branch:** `feat/crypto-box-integration`
**Source:** `/home/fujitsu/Projects/Crypto-Box/Crypto_Box.md`
**Date:** 2026-08-04

---

## Executive Summary

Crypto-Box is a **regime-aware, multi-engine mathematical prediction system** (18 engines, 9 regimes).
Trade-Bot is an **execution system** (Kelly risk, multi-strategy, live/paper trading).

Integration goal: wire Crypto-Box prediction signals as high-quality intelligence inputs into
Trade-Bot's signal pipeline, replacing or augmenting current regime detection and signal generation
without breaking execution, risk gates, or existing strategies.

The plan is organized into 8 categories. Every item is concrete, file-mapped, and directly actionable.

---

## Gaps Found in Crypto-Box (Fixed in This Plan)

| ID | Gap | Fix Applied |
|---|---|---|
| G-01 | E-15 (RL) has no state/action/reward spec | §CAT-3 defines full MDP spec |
| G-02 | E-18 (Network Centrality) has no concrete impl spec | §CAT-3 adds networkx eigenvector + whale clustering spec |
| G-03 | Spoofing penalty (E-16) only adjusts E-02 weight; E-17 equally affected | §CAT-5 extends penalty to E-17 |
| G-04 | Signal TTL uses hard entropy thresholds without hysteresis → oscillation risk | §CAT-5 adds dead-band (±0.05) around 0.3/0.7 |
| G-05 | E-12 (options) covers BTC/ETH only; LTC/XMR have no Deribit options | §CAT-2 specifies fallback: skip E-12, redistribute weight to E-01 |
| G-06 | E-10 (S2F) is BTC-native; ETH/LTC adaptation not specced | §CAT-3 defines per-coin emission models |
| G-07 | Walk-forward: no feature-computation gap to prevent look-ahead | §CAT-6 mandates 1-candle gap between train-end and feature window |
| G-08 | 30s full-cycle limit unrealistic for 18 engines; no per-engine budget | §CAT-7 defines per-engine SLA and parallel execution map |
| G-09 | No spec for how prediction → trade signal (threshold, direction) | §CAT-4 defines consensus-to-signal gate |
| G-10 | No spec for position sizing integration with Trade-Bot Kelly | §CAT-4 defines confidence-scaled Kelly multiplier |
| G-11 | Regime weight rows (Liq.Crisis, Options-Driven) need column-sum validation | §CAT-5 adds unit-test: each row must sum to 1.0 ±0.001 |
| G-12 | E-05 (on-chain graph) requires local blockchain node — not available | §CAT-2 specifies Glassnode free-tier + DeFi Llama fallback |
| G-13 | No integration with existing Trade-Bot diagnostics / audit trail | §CAT-7 wires engine outputs into audit_trail.py |
| G-14 | No circuit breaker spec when E-16 fires manipulation flag | §CAT-4 defines pause-all-signals circuit breaker |

---

## Category 1 · Data Pipeline Extension

**Goal:** Extend existing `src/data/` to ingest all 6 Crypto-Box data sources.

### 1.1 · Binance WebSocket — Orderbook + Trades (Already Partially Exists)

**Existing:** `src/data/fetcher.py` pulls OHLCV via ccxt.
**Gap:** No live orderbook depth or individual trade stream.

**New file:** `src/data/orderbook_stream.py`
```
Class: OrderbookStream
- WebSocket to wss://stream.binance.com:9443/ws/{symbol}@depth20@100ms
- WebSocket to wss://stream.binance.com:9443/ws/{symbol}@aggTrade
- Writes to: data/orderbook/{symbol}/{date}.parquet
- Writes to: data/trades/{symbol}/{date}.parquet
- Schema (orderbook): timestamp_utc, bids_json, asks_json, mid, spread_bps
- Schema (trades): timestamp_utc, price, qty, is_buyer_maker
- Data quality gate: reject if spread > 200 bps (exchange fault)
```

### 1.2 · Deribit REST — Options Chain (E-12 Feed)

**New file:** `src/data/deribit_provider.py`
```
Class: DeribitProvider
- Endpoint: https://www.deribit.com/api/v2/public/get_instruments
- Endpoint: https://www.deribit.com/api/v2/public/get_order_book (per strike)
- Symbols: BTC, ETH only (LTC/XMR → no options, E-12 skipped, weight redistributed)
- Poll: every 60s
- Cache: data/options/{symbol}/{date}.parquet
- Schema: timestamp_utc, expiry, strike, option_type, iv, oi, volume, delta, gamma
- Data quality gate: reject if IV = 0 or OI = 0
```

### 1.3 · Fear & Greed Index (E-14 Feed)

**New file:** `src/data/sentiment_provider.py`
```
Class: SentimentProvider
- alternative.me/fapi/v2/fear-and-greed-index → JSON, no auth
- Poll: every 60 min
- Cache: data/sentiment/{date}.parquet
- Schema: timestamp_utc, fg_score, fg_label
```

### 1.4 · Macro Daily Data (E-13 Feed)

**New file:** `src/data/macro_provider.py`
```
Class: MacroProvider
- yfinance: tickers = [SPX=^GSPC, DXY=DX-Y.NYB, GLD, VIX=^VIX]
- Poll: every 24h (market close)
- Cache: data/macro/{date}.parquet
- Schema: date, spx_close, dxy_close, gld_close, vix_close, spx_ret, dxy_ret
```

### 1.5 · RSS NLP Headlines (E-14 Feed)

**New file:** `src/data/rss_provider.py`
```
Class: RssNlpProvider
- Sources: CoinDesk RSS, CryptoSlate RSS (public, no auth)
- Poll: every 15 min
- NLP: VADER SentimentIntensityAnalyzer (vader_lexicon)
- Cache: data/sentiment/{date}.parquet (appended to sentiment table)
- Schema: timestamp_utc, headline, vader_compound, source
```

### 1.6 · Data Quality Gate Layer

**New file:** `src/data/quality_gate.py`
```
Class: DataQualityGate
- OHLCV: reject if last candle > 5 min old, or |return| > 15%, or volume = 0 × 3 consecutive
- Orderbook: reject if spread > 200 bps
- Options: reject if IV = 0 or OI = 0
- Macro: accept stale up to 2 days (markets closed weekends)
- Cross-validation: flag if Binance mid deviates > 0.5% from secondary source
- All gates log to diagnostics/audit_trail.py on rejection
```

### 1.7 · Parquet Storage Schema

Extend `src/data/storage.py` with `ParquetStorage` class:
```
data/
  ohlcv/{symbol}/{YYYY-MM-DD}.parquet       ← OHLCV + YZ-vol computed on ingest
  orderbook/{symbol}/{YYYY-MM-DD}.parquet   ← depth snapshots
  trades/{symbol}/{YYYY-MM-DD}.parquet      ← tick trades for E-16/E-17
  options/{symbol}/{YYYY-MM-DD}.parquet     ← IV surface
  macro/{YYYY-MM-DD}.parquet               ← SPX/DXY/GLD/VIX
  sentiment/{YYYY-MM-DD}.parquet           ← FG + NLP headlines
  engine_outputs/{YYYY-MM-DD}.parquet      ← all engine predictions (audit log)
```

---

## Category 2 · Engine Integration (Reuse Existing Repos)

**Goal:** Wrap 8 reuse-engines into `src/engines/` with standardized output schema.

### Output Schema (all engines must return this)

```python
@dataclass
class EngineOutput:
    engine_id: str            # "E-01" through "E-18"
    symbol: str               # "BTC/USDT"
    timestamp_utc: datetime
    predicted_price: float    # consensus target
    confidence: float         # 0.0–1.0
    direction: int            # +1 long, -1 short, 0 neutral
    horizon_hours: int        # 1, 4, 24
    metadata: dict            # engine-specific signals
```

### 2.1 · E-01 — Statistics / HMM / ARIMA / GRU

**Existing:** `src/regime/detector.py` (HMM), `src/intelligence/ensemble_predictor.py`
**Action:** Extract HMM signal as E-01 output; add ARIMA via `statsmodels.tsa.arima` locally.
**New file:** `src/engines/e01_statistical.py`
- ARIMA(p,d,q): auto_arima via pmdarima on rolling 180-candle window
- HMM: use existing `regime/detector.py` hidden state probability as confidence
- GRU: defer to E-09 (CryptoPredictions meta-engine wrapper)

### 2.2 · E-02 — Order Book Imbalance / Microstructure

**New file:** `src/engines/e02_microstructure.py`
```
Inputs: data/orderbook/{symbol}/today.parquet
Signals:
  bid_ask_imbalance = Σ_bid_vol(top-5) / (Σ_bid + Σ_ask)
  mid_price_drift   = (mid_now - mid_5min_ago) / mid_5min_ago
  order_flow_toxicity = |imbalance - 0.5| × 2    # 0=balanced, 1=max toxic
Direction: imbalance > 0.6 → +1; < 0.4 → -1; else 0
Confidence: order_flow_toxicity
```

### 2.3 · E-04 — Fourier / Cyclical / Halving Patterns

**New file:** `src/engines/e04_fourier.py`
```
Inputs: data/ohlcv/{symbol}/rolling 365d
FFT on log-price series → dominant cycle periods
Predict: price at T+horizon using dominant cycle phase
BTC only: halving cycle overlay from E-10 (reuse block_height)
Confidence: explained_variance_ratio of top-3 FFT components
```

### 2.4 · E-05 — On-Chain Graph / Wallet Flows

**Gap G-12 fix:** No local blockchain node. Use free APIs:
- DeFi Llama public API: TVL flows, protocol inflows
- CoinGecko public API: large holder changes (free tier)
- Existing: `src/intelligence/onchain/` providers already partially cover this

**New file:** `src/engines/e05_onchain.py`
```
Inputs: defillama TVL change, coingecko supply distribution
Net flow signal: TVL_24h_change / TVL (normalized)
Direction: >+2% → +1 (accumulation); <-2% → -1 (distribution); else 0
Confidence: |net_flow_normalized| capped at 1.0
```

### 2.5 · E-06 — Fractal / Hurst / Chaos

**New file:** `src/engines/e06_fractal.py`
```
Inputs: data/ohlcv/{symbol}/rolling 256 candles
Hurst DFA method (see §CAT-5 math spec):
  H > 0.6 → trending signal (direction follows last drift)
  H = 0.5 ±0.05 → neutral (direction = 0)
  H < 0.4 → mean-reverting signal (direction opposes last drift)
Confidence: |H - 0.5| × 2
```

### 2.6 · E-07 — PCA / Cointegration

**New file:** `src/engines/e07_linear_algebra.py`
```
Inputs: data/ohlcv/ for target + 3 correlated coins (rolling 60d)
PCA: first principal component direction as signal
Cointegration (statsmodels VECM): spread z-score
Direction: z > 2.0 → -1 (revert); z < -2.0 → +1; else 0
Confidence: 1 / (1 + |spread_z|) for mean-reversion; PCA loading for trend
```

### 2.7 · E-08 — Topology / Persistent Homology

**New file:** `src/engines/e08_topology.py`
```
Deps: giotto-tda (pip: giotto-tda)
Inputs: data/ohlcv/{symbol}/rolling 128 candles → sliding window embedding (dim=3, lag=1)
Vietoris-Rips complex → persistence diagram
Wasserstein distance W_dist(t) vs W_dist(t-1)
Persistence entropy H_topo
Signal: W_dist spike > 2σ_historical → regime break imminent → direction = 0, suppress confidence
Normal: H_topo low (< 0.3) → clean signal → use PCA direction from E-07
```

### 2.8 · E-09 — ML Meta-Engine (CryptoPredictions)

**New file:** `src/engines/e09_ml_meta.py`
```
Model: XGBoost (already in Trade-Bot deps)
Features: all E-01..E-08 outputs (confidence-weighted feature vector)
Target: next 4h return (binary: up/down)
Training: walk-forward, retrain every 30 days on 180d window
Output: P(up) as confidence; direction = +1 if P(up)>0.55, -1 if <0.45, else 0
Note: E-09 is always-on across all regimes (acts as meta-learner)
```

---

## Category 3 · Custom Engine Build

**Goal:** Implement 10 engines that don't exist or require custom math.

### 3.1 · E-03 — Information Theory Engine

**New file:** `src/engines/e03_information_theory.py`

```python
# Shannon entropy on return distribution
def shannon_entropy(returns: np.ndarray, bins=50) -> float:
    counts, _ = np.histogram(returns, bins=bins, density=False)
    probs = counts / counts.sum()
    probs = probs[probs > 0]
    return float(-np.sum(probs * np.log2(probs)))

# Transfer entropy TE(X→Y): how much X's past reduces uncertainty of Y's future
def transfer_entropy(x: np.ndarray, y: np.ndarray, lag=1) -> float:
    # Binned: p(y_{t+1}, y_t, x_t) via 3D histogram
    # TE = Σ p(y+,y,x) * log[p(y+|y,x)/p(y+|y)]
    ...  # scipy stats, no external deps

# Sample entropy (regularity measure)
def sample_entropy(series: np.ndarray, m=2, r_factor=0.2) -> float:
    r = r_factor * np.std(series)
    ...  # template matching, O(n²) but n=256

# Outputs:
#   entropy_score: float 0–1 (normalized; 0=structured, 1=chaotic)
#   predictability_index: 1 - entropy_score
#   te_btc_eth: transfer entropy BTC→ETH (if multi-coin mode)
```

Signal: entropy_score feeds Depth Detector; confidence = predictability_index.
Direction: entropy alone gives no direction (modulates other engines via confidence dampening).

### 3.2 · E-10 — Protocol Supply Engine

**New file:** `src/engines/e10_supply.py`

```python
# BTC emission curve
def btc_supply_at_block(height: int) -> float:
    halvings = height // 210_000
    subsidy = 50 / (2 ** halvings)
    mined_per_period = [50 * min(210_000, max(0, height - h * 210_000))
                        for h in range(halvings + 1)]
    return sum(mined_per_period)

# Stock-to-Flow
def btc_s2f(height: int) -> float:
    supply = btc_supply_at_block(height)
    annual_new = 365 * 144 * (50 / (2 ** (height // 210_000)))
    return supply / annual_new

# S2F price model (PlanB log-linear)
def s2f_model_price(sf: float, a=14.6, b=3.3) -> float:
    return math.exp(a + b * math.log(sf))

# Gap G-06: ETH/LTC adaptation
# ETH: PoS post-Merge → annual_new ≈ 0.3% of supply (staking yield) → SF ≈ 333
# LTC: same halving math as BTC; 84M cap; 840k blocks; 2.5min blocks
```

Output: protocol_fair_value, s2f_ratio, cycle_position (early/mid/late), deviation_pct.
Direction: price < fair_value × 0.8 → +1 (undervalued); > 1.2 → -1; else 0.
Confidence: 1 / (1 + abs(deviation_pct))

### 3.3 · E-11 — Stochastic Calculus Engine (GBM/Heston/Merton)

**New file:** `src/engines/e11_stochastic.py`

```python
# Yang-Zhang volatility estimator (replaces close-to-close everywhere)
def yang_zhang_vol(df: pd.DataFrame, window=21) -> float:
    log_oc = np.log(df['open'] / df['close'].shift(1))
    log_co = np.log(df['close'] / df['open'])
    log_ho = np.log(df['high'] / df['open'])
    log_lo = np.log(df['low'] / df['open'])
    sigma_oc = log_oc.rolling(window).var()
    sigma_co = log_co.rolling(window).var()
    rs = (log_ho*(log_ho-log_co) + log_lo*(log_lo-log_co)).rolling(window).mean()
    k = 0.34 / (1.34 + (window+1)/(window-1))
    return float(np.sqrt(sigma_oc.iloc[-1] + k*sigma_co.iloc[-1] + (1-k)*rs.iloc[-1]) * np.sqrt(8760))

# GBM Monte Carlo
def gbm_mc(S0, mu, sigma, T_hours, N=2000) -> np.ndarray:
    dt = T_hours / 8760
    Z = np.random.standard_normal(N)
    return S0 * np.exp((mu - 0.5*sigma**2)*dt + sigma*np.sqrt(dt)*Z)

# Merton jump: λ from fat-tail events (|return| > 3σ)
# Heston: κ,θ,ξ,ρ from EM on OHLCV (hmmlearn)
# Output: price distribution at T+1h/4h/24h → expected + 5th/95th pct + jump_prob
```

### 3.4 · E-12 — Options Market Signal Engine

**New file:** `src/engines/e12_options.py`

```python
# Inputs: data/options/{symbol}/today.parquet (Deribit feed)
# Gap G-05: LTC/XMR → no options → return None; consensus redistributes

def compute_gex(options_df: pd.DataFrame, spot: float) -> float:
    # GEX = Σ(gamma × OI × contract_size × spot²/100)
    # Positive GEX → dealers stabilize; Negative → dealers amplify
    ...

def put_call_ratio(options_df: pd.DataFrame) -> float:
    put_oi = options_df[options_df.option_type=='put']['oi'].sum()
    call_oi = options_df[options_df.option_type=='call']['oi'].sum()
    return put_oi / max(call_oi, 1)

def iv_skew(options_df: pd.DataFrame) -> float:
    # OTM put IV - OTM call IV at same delta (0.25)
    ...

def max_pain(options_df: pd.DataFrame) -> float:
    # Strike where aggregate option value destruction is maximum
    ...

# Outputs: pc_ratio, iv_skew, gex, max_pain_level, vrp (IV²-RV²)
# Direction: pc_ratio>1.2 → -1; <0.8 → +1; gex<0 → amplifies move
# Confidence: |iv_skew| / max_historical_skew (normalized)
```

### 3.5 · E-13 — Cross-Asset Contagion Engine

**New file:** `src/engines/e13_contagion.py`

```python
# Granger causality: statsmodels.tsa.stattools.grangercausalitytests
# Rolling correlation matrix (30d): BTC, ETH, SPX, DXY, GLD, VIX
# Contagion index: C(t) = Σ|ρᵢ(t) - ρᵢ(t-30d)| / N

REGIMES_CONTAGION = {
    'risk_on':   lambda corr: corr['btc_spx'] > 0.7,
    'dxy_driven': lambda corr: corr['btc_dxy'] < -0.6,
    'decoupling': lambda c_t, c_t30: abs(c_t['btc_spx'] - c_t30['btc_spx']) > 0.3,
    'vix_cascade': lambda signals: signals['vix_spike'] and signals['btc_drop'],
}

# Outputs: contagion_score (0-1), correlation_state label, Granger p-values
# Direction: when contagion_score > 0.7 → follow SPX direction; else 0 (crypto-native)
# Confidence: contagion_score
```

### 3.6 · E-14 — Sentiment Quantification Engine

**New file:** `src/engines/e14_sentiment.py`

```python
# Inputs: data/sentiment/today.parquet (FG + VADER scores)
alpha, beta, gamma = 0.5, 0.3, 0.2   # calibratable

def raw_sentiment(fg: float, nlp: float, social_vol: float) -> float:
    return alpha * fg + beta * nlp + gamma * social_vol

def contrarian_signal(raw: float, window_min: float, window_max: float) -> float:
    normalized = (raw - window_min) / max(window_max - window_min, 1e-9)
    # Contrarian: extreme fear → +1, extreme greed → -1
    return 1.0 - 2.0 * normalized

# Historical calibration: FG < 20 → historically +40% 30d; FG > 80 → -15% 30d
# Direction: signal > 0.3 → +1; < -0.3 → -1; else 0
# Confidence: |contrarian_signal|
```

### 3.7 · E-15 — Reinforcement Learning Engine

**Gap G-01 fix:** Full MDP specification.

**New file:** `src/engines/e15_rl.py`

```
State space S (dim=18+9=27):
  - E-01..E-17 confidence scores (17 floats)
  - Current regime one-hot (9 floats)
  - Recent realized return (1 float)

Action space A: {hold=0, long=1, short=2}

Reward: sign(action) × realized_return - 0.0002 (fee) - 0.01 × |action_change| (switching cost)

Algorithm: DQN (stable-baselines3 DQN)
  - Replay buffer: 10,000 steps of engine history
  - Training: offline on 180d backtest, update every 30d
  - Inference: argmax Q(s, a) → direction

Note: E-15 is lowest-weight engine. Acts as sanity check, not primary signal.
```

### 3.8 · E-16 — Adversarial Detection Engine

**New file:** `src/engines/e16_adversarial.py`

```python
# Spoofing detection on live orderbook stream
def spoof_detection(orderbook_events: list) -> float:
    large_walls = [e for e in orderbook_events if e['size'] > 3 * sigma_mean_bid]
    cancel_rate = sum(1 for w in large_walls if w['cancelled_ms'] < 500) / max(len(large_walls), 1)
    return cancel_rate  # spoof_confidence 0-1

# Wash trading: Benford's law on trade sizes
def benford_deviation(trade_sizes: np.ndarray) -> float:
    first_digits = np.array([int(str(s)[0]) for s in trade_sizes if s > 0])
    observed = np.bincount(first_digits, minlength=10)[1:] / len(first_digits)
    expected = np.array([np.log10(1 + 1/d) for d in range(1, 10)])
    return float(np.sum(np.abs(observed - expected)))  # volume_trust_score

# Gap G-03 fix: penalty extends to E-17 too (not just E-02)
# Outputs: spoof_confidence, volume_trust_score, manipulation_flag (bool)
# If manipulation_flag: E-02 weight halved, E-17 weight halved, E-03 weight raised
```

### 3.9 · E-17 — Liquidity Stress Engine

**New file:** `src/engines/e17_liquidity.py`

```python
def kyle_lambda(price_changes: np.ndarray, signed_volumes: np.ndarray) -> float:
    cov = np.cov(price_changes, signed_volumes)[0, 1]
    var = np.var(signed_volumes)
    return cov / max(var, 1e-12)

def amihud_ratio(returns: np.ndarray, volumes: np.ndarray, window=20) -> float:
    return np.mean(np.abs(returns[-window:]) / np.maximum(volumes[-window:], 1))

def depth_score(bids: list, spot: float, n_bps=50) -> float:
    threshold = spot * (1 - n_bps / 10000)
    return sum(b['size'] for b in bids if b['price'] >= threshold)

# Cascade threshold: price where orderbook support < 10th percentile historical depth
# Outputs: liquidity_score (0-1), stress_flag (bool), cascade_price_level
# stress_flag: kyle_lambda > 2σ AND depth < 30th pct
```

### 3.10 · E-18 — Network Centrality Engine

**Gap G-02 fix:** Concrete impl spec.

**New file:** `src/engines/e18_network.py`

```python
# Exchange flow graph: nodes = exchanges, edges = BTC/USDT flow (from DeFi Llama + CoinGecko)
# Eigenvector centrality via networkx
import networkx as nx

def exchange_flow_graph(flow_data: list) -> nx.DiGraph:
    G = nx.DiGraph()
    for flow in flow_data:
        G.add_edge(flow['from'], flow['to'], weight=flow['usd_volume'])
    return G

def centrality_signal(G: nx.DiGraph, target: str) -> float:
    centrality = nx.eigenvector_centrality_numpy(G, weight='weight')
    return centrality.get(target, 0.0)

# Whale cluster detection: PageRank on wallet-level flow (from on-chain provider)
# If no graph data: confidence = 0 (engine abstains gracefully)
# Outputs: exchange_centrality, whale_cluster_score, dominant_exchange
```

---

## Category 4 · Depth Detector v2 — 9-Regime HMM

**Goal:** Extend existing 5-regime HMM in `src/regime/detector.py` to 9 regimes.

### 4.1 · Existing → Extended Mapping

| Old Regime | New Regime | What Changed |
|---|---|---|
| trending | Trending | + E-11 activation |
| ranging | Ranging | + E-17 activation |
| volatile | Volatile | + E-11 jump-diffusion |
| accumulation | Accumulation | unchanged |
| transition | Transition | + E-15 RL activation |
| (new) | Liquidity Crisis | E-16+E-17+E-02 dominant |
| (new) | Options-Driven | E-12+E-11 dominant |
| (new) | Macro-Dominated | E-13+E-07 dominant |
| (new) | Capitulation | E-14+E-17+E-16 dominant |

### 4.2 · Observation Feature Vector (extended)

```python
features = [
    e01.confidence,           # volatility proxy
    e03.entropy_score,        # information entropy
    e06.hurst,                # persistence (DFA method)
    e02.order_flow_toxicity,  # microstructure imbalance
    e05.net_flow_normalized,  # on-chain net flow
    e10.deviation_pct,        # S2F deviation
    e12.gex,                  # GEX sign (if available, else 0)
    e13.contagion_score,      # macro coupling
    e14.contrarian_signal,    # sentiment extreme
    e17.amihud_ratio,         # liquidity stress
    adx_14,                   # ADX for trend strength
    bb_width,                 # Bollinger Band squeeze
]
```

### 4.3 · Regime Detection File Changes

**File:** `src/regime/detector.py`
- Extend `N_COMPONENTS` from 5 to 9
- Add trigger conditions table (ADX, Hurst, Amihud, contagion, GEX thresholds)
- Output: regime label string (one of 9), confidence float, weight vector (18 floats)

### 4.4 · Regime Weight Table

Implement as module-level constant in `src/engines/consensus.py` (new file):
```python
REGIME_WEIGHTS = {
    "Trending":        [0.20,0.08,0.04,0.20,0.04,0.04,0.04,0.04,0.10,0.04,0.10,0.04,0.02,0.01,0.01,0.00,0.00,0.00],
    "Ranging":         [0.15,0.20,0.08,0.04,0.04,0.08,0.04,0.04,0.08,0.00,0.05,0.05,0.02,0.02,0.01,0.05,0.05,0.00],
    "Volatile":        [0.10,0.08,0.15,0.04,0.04,0.15,0.04,0.08,0.05,0.00,0.12,0.04,0.03,0.04,0.01,0.02,0.01,0.00],
    "Accumulation":    [0.08,0.08,0.08,0.12,0.20,0.04,0.04,0.04,0.08,0.08,0.04,0.02,0.03,0.02,0.01,0.02,0.02,0.00],
    "Transition":      [0.08,0.08,0.08,0.08,0.08,0.08,0.08,0.12,0.08,0.04,0.05,0.04,0.04,0.03,0.04,0.02,0.00,0.00],
    "LiquidityCrisis": [0.05,0.15,0.05,0.02,0.02,0.05,0.02,0.02,0.05,0.00,0.05,0.05,0.03,0.03,0.01,0.20,0.20,0.00],
    "OptionsDriven":   [0.10,0.05,0.04,0.04,0.02,0.04,0.02,0.04,0.08,0.00,0.15,0.30,0.03,0.02,0.01,0.04,0.02,0.00],
    "MacroDominated":  [0.15,0.05,0.05,0.03,0.02,0.03,0.10,0.02,0.08,0.00,0.05,0.05,0.25,0.04,0.01,0.03,0.04,0.00],
    "Capitulation":    [0.05,0.05,0.05,0.02,0.03,0.05,0.02,0.05,0.05,0.00,0.08,0.08,0.04,0.20,0.01,0.12,0.10,0.00],
}
# E-18 column added (index 17); all rows must sum to 1.0 (validated by unit test — G-11 fix)
```

---

## Category 5 · Consensus Layer v2

**Goal:** Implement full Consensus Layer in `src/engines/consensus.py`.

### 5.1 · Weighted Aggregation

```python
def consensus_price(outputs: list[EngineOutput], regime: str, spoof_penalty: float) -> float:
    weights = REGIME_WEIGHTS[regime].copy()
    # Gap G-03 fix: apply spoof penalty to both E-02 (idx 1) and E-17 (idx 16)
    weights[1] *= (1 - spoof_penalty)
    weights[16] *= (1 - spoof_penalty)
    weights = np.array(weights) * np.array([o.confidence for o in outputs])
    weights /= weights.sum()
    prices = np.array([o.predicted_price for o in outputs])
    return float(np.dot(weights, prices))
```

### 5.2 · Bayesian Bootstrap Confidence Interval

```python
def bootstrap_ci(outputs: list[EngineOutput], weights: np.ndarray, N=1000) -> tuple[float, float]:
    samples = []
    for _ in range(N):
        idx = np.random.choice(len(outputs), size=len(outputs), p=weights/weights.sum())
        samples.append(np.mean([outputs[i].predicted_price for i in idx]))
    return float(np.percentile(samples, 5)), float(np.percentile(samples, 95))
```

### 5.3 · Outlier Detection (Chauvenet's Criterion)

```python
def chauvenet_outliers(outputs: list[EngineOutput]) -> list[str]:
    prices = np.array([o.predicted_price for o in outputs])
    mu, sigma = prices.mean(), prices.std()
    outlier_ids = []
    for o in outputs:
        z = abs(o.predicted_price - mu) / max(sigma, 1e-9)
        if z > 2.5:
            outlier_ids.append(o.engine_id)  # weight halved in aggregation
    return outlier_ids
```

### 5.4 · Disagreement Penalty

```python
def agreement_score(outputs: list[EngineOutput]) -> float:
    prices = np.array([o.predicted_price for o in outputs])
    return float(1 - (np.std(prices) / np.mean(prices)))
    # if < 0.5: all confidence scores dampened × 0.6
```

### 5.5 · Kelly-Weighted Engine Scoring

```python
def kelly_weight(engine_id: str, confidence: float, backtest_gain_ratio: float) -> float:
    b = backtest_gain_ratio    # from walk-forward backtest results
    p = confidence
    q = 1 - p
    f_star = (b * p - q) / b
    return max(0.0, f_star * 0.25)  # quarter-Kelly
```

### 5.6 · Signal TTL with Hysteresis (Gap G-04 Fix)

```python
# Dead-band: only transition state when crossing threshold ± 0.05
LOW_ENTROPY_THRESHOLD = 0.3
HIGH_ENTROPY_THRESHOLD = 0.7
HYSTERESIS = 0.05

def compute_ttl_hours(entropy_score: float, current_state: str) -> int:
    if current_state == "low" and entropy_score > LOW_ENTROPY_THRESHOLD + HYSTERESIS:
        new_state = "high"
    elif current_state == "high" and entropy_score < HIGH_ENTROPY_THRESHOLD - HYSTERESIS:
        new_state = "low"
    else:
        new_state = current_state
    return (24 if new_state == "low" else 1), new_state
```

---

## Category 6 · Risk & Uncertainty Quantification Layer

**Goal:** New layer between Consensus and signal output in `src/engines/risk_quantifier.py`.

### 6.1 · Prediction Uncertainty Score

```python
def uncertainty_score(ci_low: float, ci_high: float, consensus: float) -> tuple[float, str]:
    width_pct = (ci_high - ci_low) / consensus
    if width_pct < 0.02:
        return width_pct, "high_confidence"
    elif width_pct < 0.08:
        return width_pct, "moderate"
    else:
        return width_pct, "suppress"  # suppress directional signal
```

### 6.2 · Tail Risk Flag

```python
def tail_risk_score(jump_prob: float, liquidity_score: float) -> float:
    # From E-11 jump probability and E-17 liquidity score
    score = jump_prob * (1 / max(liquidity_score, 0.01))
    return min(score, 1.0)
    # > 0.3 → tail_risk_active → output explicit warning
```

### 6.3 · Max Adverse Excursion Estimate

```python
def mae_estimate(consensus: float, yz_vol: float, horizon_hours: int, z_99=2.576) -> float:
    return consensus * yz_vol * np.sqrt(horizon_hours / 8760) * z_99
```

### 6.4 · Circuit Breaker (Gap G-14 Fix)

```python
# In src/engines/consensus.py
class CircuitBreaker:
    def check(self, e16_output: EngineOutput) -> bool:
        if e16_output.metadata.get('manipulation_flag'):
            # Pause all signals, log to audit_trail.py, alert user
            return True
        return False
```

### 6.5 · Consensus-to-Trade-Signal Gate (Gap G-09 Fix)

```python
# New file: src/engines/signal_gate.py
# Bridges Crypto-Box consensus → Trade-Bot strategy layer

@dataclass
class TradeSignal:
    symbol: str
    direction: int          # +1/-1/0
    confidence: float       # 0-1 (dampened by uncertainty)
    kelly_multiplier: float # 0-1 (scales Kelly fraction)
    regime: str
    ttl_hours: int
    warnings: list[str]     # tail_risk, manipulation, suppress flags

def consensus_to_signal(
    consensus: float,
    spot: float,
    uncertainty: str,
    agreement: float,
    tail_risk: float,
    e16_flag: bool,
) -> TradeSignal:
    # Gap G-09 fix: threshold-based direction
    pct_diff = (consensus - spot) / spot
    if uncertainty == "suppress" or e16_flag:
        direction = 0
    elif pct_diff > 0.005:   # > 0.5% predicted upside
        direction = 1
    elif pct_diff < -0.005:
        direction = -1
    else:
        direction = 0

    # Gap G-10 fix: confidence-scaled Kelly multiplier
    kelly_mult = agreement * (1 - tail_risk) * (0.0 if uncertainty == "suppress" else 1.0)
    return TradeSignal(...)
```

---

## Category 7 · Backtesting Framework

**Goal:** Implement walk-forward backtest in `src/tuning/` (extends existing tuning infrastructure).

### 7.1 · Walk-Forward Protocol

**New file:** `src/tuning/engine_backtest.py`
```
Train window: rolling 180 candles (1h) = 7.5 days
Test window:  next 30 candles (out-of-sample)
Step:         advance 30 candles, retrain, re-test
Gap G-07 fix: 1-candle gap between train-end and feature computation window
              (prevents look-ahead from features that use centered windows)
```

### 7.2 · Per-Engine Metrics

```python
METRICS = {
    'directional_accuracy': lambda pred, actual: (pred * actual > 0).mean(),  # threshold > 0.55
    'rmse_pct': lambda pred, actual: np.sqrt(np.mean((pred-actual)**2)) / actual.mean(),  # < 2%
    'signal_sharpe': lambda rets: rets.mean() / rets.std() * np.sqrt(8760),  # > 1.0
    'max_drawdown': lambda rets: max_drawdown(rets),  # < 30%
}
```

### 7.3 · Regime Accuracy Matrix

```python
# 9 regimes × 18 engines → accuracy_weight[engine][regime]
# After Phase-6 calibration: override static REGIME_WEIGHTS with backtest-derived values
accuracy_weight[engine][regime] = (
    historical_accuracy[engine][regime]
    / sum(historical_accuracy[e][regime] for e in all_engines)
)
```

### 7.4 · Performance Gate (Gap G-08 Fix)

Per-engine SLA (enforced via asyncio timeout):
```
E-01, E-07, E-10:  ≤ 2s   (pure math, no I/O)
E-02, E-03, E-06:  ≤ 3s   (local data, vectorized)
E-04, E-08:        ≤ 5s   (FFT + TDA)
E-05, E-13, E-14:  ≤ 5s   (cached API)
E-11:              ≤ 8s   (Monte Carlo N=2000)
E-09:              ≤ 10s  (XGBoost inference)
E-12, E-17, E-18:  ≤ 5s   (local parquet reads)
E-15, E-16:        ≤ 5s   (DQN inference + statistical)
Full cycle target:  ≤ 30s (all engines parallel via asyncio.gather)
```

---

## Category 8 · Wiring & Integration

**Goal:** Wire Crypto-Box engine layer into existing Trade-Bot infrastructure.

### 8.1 · Engine Orchestrator

**New file:** `src/engines/__init__.py` + `src/engines/orchestrator.py`
```python
# Parallel engine execution
async def run_all_engines(symbol: str, data: dict) -> list[EngineOutput]:
    tasks = [
        asyncio.wait_for(engine.run(symbol, data), timeout=engine.sla_seconds)
        for engine in REGISTERED_ENGINES
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    # Engines that timeout/error → removed from consensus (graceful degradation)
    return [r for r in results if isinstance(r, EngineOutput)]
```

### 8.2 · Integration with Existing Signal Engine

**File:** `src/engine/signal_engine.py`
- Add `CryptoBoxSignalAdapter` that calls `consensus_to_signal()` and returns a `Signal` in the existing schema
- Wire into existing orchestrator.py alongside current strategies

### 8.3 · Diagnostics / Audit Trail Integration (Gap G-13 Fix)

**File:** `src/diagnostics/audit_trail.py`
- Extend to log `engine_outputs` parquet record per cycle
- Log circuit breaker events, TTL expirations, manipulation flags

### 8.4 · Existing Regime Detector Transition

**File:** `src/regime/detector.py`
- Add backward-compatible `v2` method that calls `DepthDetectorV2` when `CRYPTO_BOX=true` env flag
- Old 5-regime behavior preserved as fallback

### 8.5 · Test Coverage Requirements

All new `src/engines/*.py` files require tests in `tests/engines/`:
- Schema validation test (EngineOutput dataclass contract)
- Unit test: REGIME_WEIGHTS rows sum to 1.0 (Gap G-11 fix)
- Walk-forward: directional accuracy > 0.5 (random baseline) on synthetic data
- Circuit breaker: manipulation_flag=True → direction=0 always
- TTL hysteresis: no oscillation when entropy_score within dead-band

Coverage floor for `src/engines/`: 90% per CLAUDE.md convention.

---

## Implementation Order

Execute phases in sequence. Each phase is CI-gate-able independently.

| Phase | Deliverables | Key Files | CI Gate |
|---|---|---|---|
| **P1** | Data pipeline | `src/data/orderbook_stream.py`, `deribit_provider.py`, `sentiment_provider.py`, `macro_provider.py`, `rss_provider.py`, `quality_gate.py` | Tests for schema + quality gates |
| **P2** | Reuse engines E-02,04,05,06,07,08 | `src/engines/e0[2456780]_*.py` | Unit + schema tests |
| **P3** | Math utilities | Yang-Zhang, DFA Hurst, Transfer Entropy, Wasserstein | Math correctness tests |
| **P4** | Custom engines E-03,10,11,12,13,14,15,16,17,18 | `src/engines/e1[012345678]_*.py` + `e03_*.py` | Unit + schema tests |
| **P5** | Depth Detector v2 (9 regimes) | `src/regime/detector.py` extended | Regime transition tests |
| **P6** | Consensus Layer v2 | `src/engines/consensus.py` | Bootstrap CI, outlier, agreement tests |
| **P7** | Risk Quantification + Signal Gate | `src/engines/risk_quantifier.py`, `signal_gate.py` | Circuit breaker test |
| **P8** | Wiring + Backtest | `src/engines/orchestrator.py`, `src/tuning/engine_backtest.py` | Walk-forward, coverage floor |

---

## Dependencies to Add

```toml
# pyproject.toml additions
giotto-tda = ">=0.6"          # E-08 persistent homology
statsmodels = ">=0.14"        # E-01 ARIMA, E-13 Granger causality
pmdarima = ">=2.0"            # auto_arima
vaderSentiment = ">=3.3"      # E-14 NLP
yfinance = ">=0.2"            # E-13 macro
networkx = ">=3.0"            # E-18 graph centrality
stable-baselines3 = ">=2.0"   # E-15 DQN
```

All free, no paid APIs required. On-chain data uses existing DeFi Llama + CoinGecko providers in `src/intelligence/onchain/`.

---

## What This Plan Intentionally Excludes

- **Live on-chain blockchain node**: G-12 fix uses free APIs instead.

---

# PART II · Cryptographic Foundations Integration

> Every section of Part II maps to one or more concrete implementation areas.
> Six integration pillars: (A) Security hardening, (B) Market signals from crypto-economic math,
> (C) On-chain analysis upgrades, (D) Mathematical engine upgrades, (E) Attack defense layer,
> (F) Future ZK/FHE computation capability + DIR-1/2/3 research tasks.

---

## Category 9 · Security Hardening (Infrastructure Layer)

**Goal:** Apply Part II cryptographic primitives to Trade-Bot's own API, auth, and key management.

### 9.1 · secp256k1 / ECDSA / Schnorr — API Request Signing

**Source:** P1 (secp256k1 + ECDSA + Schnorr + BIP-32)
**New file:** `src/security/api_signer.py`

```python
# Replace HMAC-SHA256 API signing with Ed25519 (see §9.2) or at minimum enforce RFC 6979
# RFC 6979: deterministic nonce k = HMAC-DRBG(sk, message_hash)
# Prevents k-reuse attack (A1): two sigs with same k → full private key recovery

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

class ApiSigner:
    # deterministic: no random k → immune to entropy failures (Android 2013 / PS3 2010)
    def sign_request(self, method: str, path: str, body: str, timestamp: int) -> str:
        payload = f"{timestamp}{method}{path}{body}".encode()
        return self._key.sign(payload).hex()
```

**Attack defended (from Cross-Layer Attack Surface table):**
- `Entropy → k=const in ECDSA` (Sony PS3 2010, Android BTC wallets 2013)
- `Modular Arith. → ECDSA k-reuse` → RFC 6979 deterministic k

### 9.2 · Ed25519 / X25519 — Authentication Tokens

**Source:** P2 (Ed25519 / X25519), P.II §4 Elliptic Curve Theory
**New file:** `src/security/auth_keys.py`

```python
# Ed25519: complete addition law → no exceptional cases → constant-time guaranteed
# Cofactor h=8: verify 8·S·B = 8·R + 8·H·A (cofactor clearing prevents small-subgroup)
# Replace JWT HS256 → JWT EdDSA in src/api/auth.py

# X25519: Diffie-Hellman for future mTLS between Trade-Bot services
# Montgomery ladder → u-coordinate only → no branch on secret bit
```

**File change:** `src/api/auth.py` — swap HS256 to EdDSA via `python-jose[ed25519]`.

### 9.3 · BIP-32 HD Key Derivation — Exchange API Credential Management

**Source:** P1 BIP-32 HD KEYS section
**New file:** `src/security/credential_vault.py`

```python
# Per-exchange API keys derived from a single master seed via BIP-32
# Hardened derivation only (index >= 2^31): no normal child derivation
# Prevents: "Normal child + parent xpub + any child xprv → parent xprv" (risk noted in P1)

# m/44'/coin_type'/exchange_index'/0/key_index
# Each exchange gets its own derivation path → compromise of one key ≠ all keys
```

### 9.4 · Constant-Time Implementations — Side-Channel Defense

**Source:** §8 Statistics (DPA/CPA), Cross-Layer Attack Surface: Timing/DPA rows
**New file:** `src/security/constant_time.py`

```python
# All secret comparisons must use hmac.compare_digest (not ==)
# API key validation: hmac.compare_digest(provided, stored)
# No branching on secret bytes: prevents Kocher timing attack (1996)
# No early-exit loops over secret data

import hmac

def safe_compare(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())
```

**File changes:** audit all comparisons in `src/api/auth.py`, `src/api/access_control.py`.

### 9.5 · Kyber-768 / ML-KEM — Quantum-Safe Key Exchange (Future Layer)

**Source:** P6 (Kyber-768 Full Spec), §5 Lattice Theory, Quantum Threat Table
**New file:** `src/security/pq_transport.py`

```python
# Not for immediate deployment — infrastructure stub + documentation
# Quantum threat: ECDH/X25519 broken by Shor's algorithm on CRQC
# HNDL risk: attacker stores Trade-Bot API traffic today, decrypts when CRQC exists
# Timeline: consensus ~2030-2040

# Kyber-768: n=256, k=3, q=3329; IND-CCA2 under Module-LWE in QROM
# Implementation: liboqs Python bindings (open-quantum-safe.org)
# Hybrid mode: X25519 + Kyber-768 (NIST recommendation during transition)

# Stub:
class PqTransport:
    """Placeholder for Kyber hybrid key exchange. Wire when liboqs stable."""
    MODE = "classical"  # flip to "hybrid" when ready
```

### 9.6 · Dilithium3 / ML-DSA — Quantum-Safe Signatures

**Source:** §5 Lattice Theory (SIS hardness → Dilithium), Quantum Threat Table
**New file:** (extend `src/security/api_signer.py`)

```python
# Dilithium3: Module-LWE+SIS; NIST ML-DSA standard; 128-bit PQ security
# Future swap: Ed25519 signing → Dilithium3 signing when CRQC timeline firms up
# Stub class with mode flag: "ed25519" | "dilithium3"
```

---

## Category 10 · Market Signals from Crypto-Economic Math

**Goal:** Extract price-predictive signals from the mathematical properties of blockchain protocols.
These feed into existing engines (especially E-05, E-10, E-13, E-16) as additional features.

### 10.1 · 51% Attack Cost Model → Network Security Signal

**Source:** A6 (51% Attack Markov Chain Model), §12 Game Theory
**New file:** `src/engines/signals/network_security.py`

```python
# 51% attack cost = hashrate_needed × time × electricity_price
# Higher cost → more secure network → bullish long-term signal
# Sudden cost drop (hashrate crash) → security risk → bearish signal

# Nakamoto double-spend model:
# Pr[attacker catches up from z deficit] = (q/p)^z if q < p
# q = attacker fraction, p = 1-q

def double_spend_risk(q: float, confirmations: int) -> float:
    """Returns probability attacker successfully double-spends after k confirms."""
    if q >= 0.5:
        return 1.0
    p = 1 - q
    return (q / p) ** confirmations

def network_security_score(hashrate_7d: float, hashrate_90d_avg: float,
                            electricity_usd_per_kwh: float = 0.05) -> dict:
    # hashrate drop > 20% vs 90d avg → security deteriorating
    hashrate_ratio = hashrate_7d / max(hashrate_90d_avg, 1)
    q_approx = max(0.0, 0.5 * (1 - hashrate_ratio))  # naive attacker share proxy
    return {
        "security_score": hashrate_ratio,  # > 1.0 = improving, < 0.8 = warning
        "double_spend_risk_6conf": double_spend_risk(q_approx, 6),
        "direction": 1 if hashrate_ratio > 1.05 else (-1 if hashrate_ratio < 0.8 else 0),
    }
```

Wire into E-05 (on-chain) `metadata` field.

### 10.2 · Selfish Mining Detection → MEV/Manipulation Signal

**Source:** §12 Game Theory (Eyal-Sirer), A6, Cross-Layer Attack Surface
**New file:** `src/engines/signals/selfish_mining.py`

```python
# Selfish mining profitable when pool α > 1/3 (γ=0 propagation)
# Revenue ratio formula from A6: α(1-α)²(4α+γ(1-2α)) / (1-α(1+(2-α)α))
# Signal: orphan rate spike → possible selfish mining → miner centralization risk
# Feeds E-16 (adversarial detection) as additional manipulation vector

def selfish_mining_threshold(gamma: float = 0.0) -> float:
    """Minimum pool fraction at which selfish mining becomes profitable."""
    return 1 / (3 + gamma)

def orphan_rate_signal(orphan_rate_7d: float, baseline: float = 0.005) -> int:
    # orphan_rate > 3× baseline → anomaly → manipulation flag
    return -1 if orphan_rate_7d > 3 * baseline else 0
```

### 10.3 · MEV Quantification → E-02 Microstructure Upgrade

**Source:** §12 Game Theory (MEV formula), Cross-Layer Attack Surface
**Extend:** `src/engines/e02_microstructure.py`

```python
# MEV_block = Σ(arb + liquidation + sandwich profits)
# MEV spike → validator ordering power → price impact unpredictable → E-02 confidence dampened
# Source: public MEV-boost data (https://mevboost.pics API, no auth)

def mev_signal(mev_usd_24h: float, mev_baseline_30d: float) -> dict:
    ratio = mev_usd_24h / max(mev_baseline_30d, 1)
    return {
        "mev_ratio": ratio,
        "confidence_penalty": min(0.5, (ratio - 1) * 0.2) if ratio > 1.5 else 0.0,
        # high MEV → orderbook signals less reliable → dampen E-02 confidence
    }
```

### 10.4 · Merkle Proof Integrity → On-Chain Data Verification

**Source:** §10 Graph Theory (Merkle Tree: `h = H(hₗ ‖ hᵣ)`; O(log n) proofs)
**New file:** `src/security/merkle_verify.py`

```python
# Verify on-chain data claims from providers using Merkle inclusion proofs
# Prevents: feed manipulation of DeFi Llama / CoinGecko data
# Applies to E-05, E-10 data integrity checks

import hashlib

def merkle_verify(leaf: bytes, proof: list[tuple[bytes, str]], root: bytes) -> bool:
    current = hashlib.sha256(leaf).digest()
    for sibling, direction in proof:
        if direction == "left":
            current = hashlib.sha256(sibling + current).digest()
        else:
            current = hashlib.sha256(current + sibling).digest()
    return current == root
```

### 10.5 · Birthday Bound → Hash Collision Risk in Storage Keys

**Source:** §11 Combinatorics (Birthday Bound), §7 Probability Theory
**Existing file audit:** `src/data/storage.py`

```
Audit: any dict/cache key constructed from hash(symbol + timestamp)
Birthday bound: collision at ≈ 1.17 × √n where n = keyspace size
Risk: SHA-256 truncated to 64 bits → collision at 2^32 ≈ 4B entries
Fix: use full SHA-256 (256-bit) as storage key — never truncate
```

### 10.6 · Chernoff Bound → Mining Difficulty / Block Time Analysis

**Source:** §7 Probability Theory (Chernoff Bound: `Pr[X≥(1+δ)μ] ≤ e^{-δ²μ/3}`)
**Extend:** `src/engines/e10_supply.py`

```python
# Bitcoin block time: 10 min target, recalibrates every 2016 blocks
# Chernoff bound: probability of N consecutive fast blocks
# Anomalous block timing → hashrate spike → difficulty adjustment incoming → price signal

def block_time_anomaly(recent_block_times_sec: list[float]) -> dict:
    mu = 600.0  # target 10 min
    observed_mean = np.mean(recent_block_times_sec)
    delta = abs(observed_mean - mu) / mu
    chernoff_prob = np.exp(-delta**2 * len(recent_block_times_sec) / 3)
    return {
        "anomaly_score": 1 - chernoff_prob,  # high → significant deviation
        "direction": 1 if observed_mean > mu * 1.2 else (-1 if observed_mean < mu * 0.8 else 0),
        # slow blocks → hashrate drop → miner capitulation → bearish short-term
    }
```

### 10.7 · HNDL Risk → BTC Address Reuse Signal

**Source:** Quantum Threat Table (HNDL section), P1 BIP-32
**New file:** `src/engines/signals/quantum_exposure.py`

```python
# P2PK outputs: pubkey exposed on-chain → vulnerable to future CRQC (Shor's)
# P2PKH/P2WPKH: pubkey hidden until spend → safe until spent
# Address reuse: spent P2PKH exposes pubkey → HNDL risk

# Signal: if large BTC holder is reusing P2PK/spent P2PKH addresses →
#         quantum exposure score elevated → long-term bearish overlay

# Data source: public blockchain (via BlockCypher free API or local node)
def quantum_exposure_score(p2pk_utxo_pct: float, reused_address_pct: float) -> float:
    # Higher score → more BTC exposed to future quantum attack → risk factor
    return 0.6 * p2pk_utxo_pct + 0.4 * reused_address_pct
```

Wire as metadata into E-05 and E-13 (long-horizon only).

---

## Category 11 · On-Chain Cryptographic Analysis Upgrades

### 11.1 · Ring Signature Analysis → Monero/Privacy Coin Engine

**Source:** P7 (Monero: CLSAG + Stealth Addresses + RingCT)
**Extend:** `src/engines/e05_onchain.py`

```python
# XMR-specific on-chain signals despite confidential transactions:
# - Ring size distribution: larger rings → more privacy-conscious activity → accumulation signal
# - Transaction fee spikes → demand surge
# - CLSAG vs MLSAG migration rate → protocol upgrade adoption
# - Stealth address generation rate (proxy for new wallets)
# Data: XMR explorer public APIs (xmrchain.net)

def xmr_privacy_signal(avg_ring_size: float, fee_usd_24h: float) -> dict:
    # ring_size < 11 (minimum) never seen post-2022; spikes indicate premium privacy demand
    return {
        "ring_size_signal": 1 if avg_ring_size > 16 else 0,  # above avg → accumulation
        "fee_pressure": fee_usd_24h,
    }
```

### 11.2 · UTXO Graph Analysis → Whale Movement Detection

**Source:** §10 Graph Theory (DAG Blockchain, P2P Network), P7 Monero TX graph
**Extend:** `src/engines/e05_onchain.py`

```python
# UTXO graph: directed edges (tx_in → tx_out)
# Whale detection: UTXO cluster with value > 1000 BTC moving
# Taint analysis: follow coinbase outputs through graph (chain depth ≤ 5)
# Source: BlockCypher free API (limited), public mempool.space API

def utxo_whale_signal(large_utxo_movements: list[dict]) -> dict:
    total_btc_moving = sum(u['value_btc'] for u in large_utxo_movements if u['value_btc'] > 100)
    # Large whale outflow from exchanges → accumulation → +1
    # Large inflow to exchanges → distribution → -1
    exchange_inflow = sum(u['value_btc'] for u in large_utxo_movements if u['to_exchange'])
    exchange_outflow = sum(u['value_btc'] for u in large_utxo_movements if u['from_exchange'])
    net = exchange_outflow - exchange_inflow
    return {"net_exchange_flow_btc": net, "direction": 1 if net > 0 else (-1 if net < 0 else 0)}
```

### 11.3 · Nakamoto Consensus Markov Chain → Fork Risk Signal

**Source:** §7 Probability Theory (Nakamoto double-spend model, random walk, martingale)
**New file:** `src/engines/signals/fork_risk.py`

```python
# Stale block rate from public mempool data → approximates orphan rate
# High orphan rate → network propagation issues → fork risk → price volatility signal
# Martingale property: E[chain_length | current] = current (fair process)
# Deviation from martingale → manipulation or network partition

def fork_risk_score(orphan_count_24h: int, total_blocks_24h: int) -> float:
    orphan_rate = orphan_count_24h / max(total_blocks_24h, 1)
    # BTC normal orphan rate ≈ 0.1–0.5%; > 1% → concern
    return min(orphan_rate / 0.01, 1.0)  # normalized 0-1
```

---

## Category 12 · Mathematical Engine Upgrades from Part II

### 12.1 · NTT (Number Theoretic Transform) → Polynomial Multiplication Upgrade

**Source:** §9 Linear Algebra (NTT: O(n log n) polynomial multiplication)
**Upgrade target:** `src/engines/e04_fourier.py`

```python
# Current FFT: scipy.fft (floating point) — numerical errors accumulate
# NTT upgrade: exact arithmetic over finite field ℤq
# Use q = NTT-friendly prime (e.g., q = 3329 from Kyber, or custom for signal length)
# Benefit: exact cycle detection, no floating-point spectral leakage

# NTT: â_k = Σ_{j=0}^{n-1} aⱼ·ωʲᵏ mod q; ω = primitive n-th root mod q
# O(n log n) — same as FFT but exact

def ntt(a: list[int], q: int, omega: int) -> list[int]:
    n = len(a)
    if n == 1:
        return a
    even = ntt(a[::2], q, omega * omega % q)
    odd  = ntt(a[1::2], q, omega * omega % q)
    factor = 1
    result = [0] * n
    for i in range(n // 2):
        result[i]         = (even[i] + factor * odd[i]) % q
        result[i + n//2]  = (even[i] - factor * odd[i]) % q
        factor = factor * omega % q
    return result
```

### 12.2 · DFA Hurst Exponent → Replace R/S in E-06

**Source:** Advanced Calculations section of Crypto_Box.md (already in Part I plan)
Already covered in §2.5 · E-06. No duplication needed.

### 12.3 · LLL / BKZ → Lattice Basis Analysis for Correlation Matrix

**Source:** §9 Linear Algebra (LLL Algorithm), §5 Lattice Theory
**Extend:** `src/engines/e07_linear_algebra.py`

```python
# LLL application to correlation matrix:
# Model correlation matrix as a lattice basis → LLL-reduce → find "short vectors"
# Short vectors in correlation space = most independent factor directions
# Superior to vanilla PCA: exploits integer structure of co-movement clusters

# Note: fpylll library (Python LLL binding)
# Apply: if correlation matrix eigenvalue gap > 0.3 → use LLL factors; else PCA
```

### 12.4 · Persistent Homology Wasserstein Distance → E-08 Upgrade

**Source:** Advanced Calculations section, §18 Algebraic Topology
Already covered in §2.7 · E-08 (Wasserstein distance signal). No duplication.

### 12.5 · Transfer Entropy Upgrade → E-03

**Source:** Advanced Calculations (Transfer Entropy formula), §6 Information Theory
Already covered in §3.1 · E-03. No duplication.

### 12.6 · Martingale / Optional Stopping → Signal Validity Test

**Source:** §17 Measure Theory, §7 Probability Theory
**New file:** `src/engines/signals/martingale_test.py`

```python
# Test whether price series exhibits martingale property (fair/unpredictable)
# vs sub/super martingale (trending drift)
# E[P_{t+1} | ℱ_t] = P_t → no predictability (suppress signals)
# E[P_{t+1} | ℱ_t] > P_t → upward drift (bullish signal)

# Statistical test: variance ratio test (Lo-MacKinlay)
def variance_ratio_test(prices: np.ndarray, q: int = 5) -> dict:
    # VR(q) = Var(q-period return) / (q × Var(1-period return))
    # VR ≈ 1 → martingale (random walk)
    # VR > 1 → positive autocorrelation (momentum)
    # VR < 1 → negative autocorrelation (mean-reversion)
    ret1 = np.diff(np.log(prices))
    retq = np.log(prices[q:] / prices[:-q])
    vr = np.var(retq) / (q * np.var(ret1))
    return {"vr": vr, "signal": 1 if vr > 1.1 else (-1 if vr < 0.9 else 0)}
```

Wire into E-01 as additional feature.

### 12.7 · Hybrid Argument → Security Proof Logging

**Source:** §7 Probability Theory (Hybrid Argument), §6 Information Theory (Semantic Security)
**Not code — documentation requirement:**

`src/security/SECURITY_PROOFS.md` — document the security argument for each
cryptographic component added (api_signer, auth_keys, credential_vault) using
hybrid argument structure: Game₀ (real) → Game₁ → ... → Gameₙ (ideal).
Follows the same structure as IND-CPA/IND-CCA proofs in Part II.

---

## Category 13 · Attack Defense Layer

**Goal:** Implement defensive countermeasures for every attack in Part II §A1–A6 and
the Cross-Layer Attack Surface table that applies to Trade-Bot.

### 13.1 · A1 Defense — ECDSA k-Reuse Prevention

**Already covered:** §9.1 (RFC 6979 deterministic k). No extra file needed.

### 13.2 · A2 Defense — Pohlig-Hellman / Small Subgroup

**Source:** A2 (Pohlig-Hellman in Detail), Cross-Layer Attack Surface (Algebra row)
**New file:** `src/security/group_validation.py`

```python
# Validate that any DH key exchange uses prime-order groups
# Small subgroup attack: if cofactor h > 1 → clear cofactor before use
# secp256k1: h=1 → immune; Ed25519: h=8 → cofactor clearing required

def validate_prime_order_group(group_order: int) -> bool:
    """Reject if group order is not prime (Pohlig-Hellman exploits smooth order)."""
    from sympy import isprime
    return isprime(group_order)

def cofactor_clear(point: tuple, cofactor: int, group_order: int) -> tuple:
    """Multiply point by cofactor to land in prime-order subgroup."""
    # Applied to Ed25519 verify: 8·S·B = 8·R + 8·H·A
    return scalar_mul(point, cofactor, group_order)
```

### 13.3 · A3 Defense — Coppersmith / Weak RSA

**Source:** A3 (Coppersmith/LLL → Weak RSA)
**New file:** `src/security/key_validation.py`

```python
# Validate any RSA keys used (e.g., exchange webhook signatures)
# Wiener attack: reject if d < n^0.25/3 (check via e/n continued fraction)
# Fermat factoring: reject if |p-q| < 2^(n/4 - 100)
# ROCA (CVE-2017-15361): reject Infineon-structured primes

def validate_rsa_key(n: int, e: int) -> dict:
    issues = []
    # Check e not too small (low exponent attack)
    if e < 65537:
        issues.append("e too small — Hastad broadcast attack risk")
    # Check n bit length
    if n.bit_length() < 2048:
        issues.append("n < 2048 bits — GNFS feasible")
    return {"valid": len(issues) == 0, "issues": issues}
```

### 13.4 · A4 Defense — Differential Power Analysis (DPA)

**Source:** A4 (DPA Full), Cross-Layer Attack Surface (Side-channel rows)
**Applies to:** Any hardware wallet integration or HSM usage (future)
**New file:** `src/security/SIDE_CHANNEL_NOTES.md`

```
- All secret operations: use constant-time libraries (cryptography, nacl)
- No Hamming-weight-correlated branching on key bytes
- Power analysis: not applicable to software-only bot (no physical side-channel access)
- Timing: covered by §9.4 (hmac.compare_digest everywhere)
- Hertzbleed (CVE-2022-27459): CPU frequency scaling leaks Kyber internals
  → Mitigation: disable frequency scaling on signing machines when using PQ crypto
```

### 13.5 · A5 Defense — Lattice Sieving Parameter Floors

**Source:** A5 (Lattice Sieving BKZ/BDGL), §5 Lattice Theory
**New file:** `src/security/pq_parameter_check.py`

```python
# When E-15 RL training uses lattice-based operations, enforce parameter floors
# BKZ-β cost: 2^{0.292β}; for 128-bit security → β ≥ 245
# Kyber-768: Core-SVP β=245; concrete security ~161 bits (with primal/dual attacks)

PQ_SECURITY_FLOORS = {
    "kyber_512": 100,    # bits; below threshold → reject
    "kyber_768": 161,    # NIST recommended level 3
    "dilithium3": 128,
    "falcon_512": 103,
}

def validate_pq_params(scheme: str, claimed_security_bits: int) -> bool:
    floor = PQ_SECURITY_FLOORS.get(scheme, 128)
    return claimed_security_bits >= floor
```

### 13.6 · A6 Defense — 51% Attack Monitoring

**Source:** A6 (51% Attack Markov Chain), already partially covered in §10.1
**Extend:** `src/engines/signals/network_security.py`

```python
# Selfish mining revenue surface: α(1-α)²(4α+γ(1-2α)) / (1-α(1+(2-α)α))
# Monitor: if estimated attacker fraction α > 0.33 → alert + reduce position size

def selfish_mining_revenue_ratio(alpha: float, gamma: float = 0.5) -> float:
    num = alpha * (1-alpha)**2 * (4*alpha + gamma*(1 - 2*alpha))
    den = 1 - alpha * (1 + (2-alpha)*alpha)
    return num / max(den, 1e-9)

def position_size_penalty(alpha: float) -> float:
    # Scale down position when 51% risk elevated
    if alpha > 0.33:
        return max(0.0, 1.0 - (alpha - 0.33) * 3.0)
    return 1.0
```

### 13.7 · Reentrancy / Integer Overflow — Smart Contract Audit (DeFi Data Sources)

**Source:** Cross-Layer Attack Surface (Formal Logic rows: The DAO, BECToken CVE-2018-10299)
**New file:** `src/data/defi_data_validator.py`

```python
# DeFi Llama TVL data can be corrupted by reentrancy exploits or integer overflow hacks
# Validate: TVL change > 50% in single block → possible exploit → reject data point
# Validate: TVL goes negative → integer overflow in protocol → reject

def validate_defi_tvl(tvl_current: float, tvl_prev: float) -> bool:
    if tvl_current < 0:
        return False  # integer overflow in protocol
    change_pct = abs(tvl_current - tvl_prev) / max(tvl_prev, 1)
    if change_pct > 0.5:
        return False  # > 50% single-block change → exploit signal, reject
    return True
```

---

## Category 14 · ZK / FHE Future Computation Layer

**Goal:** Stub infrastructure for future private signal computation and ZK proof generation.
Implement research items from DIR-1, DIR-2, DIR-3.

### 14.1 · DIR-1A — secp256k1 from Axioms (Educational Implementation)

**Source:** DIR-1 `1A`, P1, §4 Elliptic Curve Theory
**New file:** `src/research/secp256k1_from_axioms.py`

```python
# Pure Python implementation of secp256k1 field → group → ECDSA → Schnorr → BIP-32
# Purpose: verified understanding of the math underlying every BTC/ETH transaction
# Not used in production — educational / audit reference

P  = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEFFFFFC2F
N  = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
Gx = 0x79BE667EF9DCBBAC55A06295CE870B07029BFCDB2DCE28D959F2815B16F81798
Gy = 0x483ADA7726A3C4655DA4FBFC0E1108A8FD17B448A68554199C47D08FFB10D4B8

class FieldElement:
    def __init__(self, val: int, mod: int = P): ...
    def __add__(self, other): ...
    def __mul__(self, other): ...
    def inv(self): return pow(self.val, self.mod - 2, self.mod)  # Fermat's little theorem

class ECPoint:
    def __add__(self, other): ...   # chord-tangent law
    def __rmul__(self, k: int): ... # Montgomery ladder (constant-time)

def ecdsa_sign(msg_hash: int, privkey: int) -> tuple[int, int]: ...  # RFC 6979 k
def ecdsa_verify(msg_hash: int, sig: tuple, pubkey: ECPoint) -> bool: ...
def schnorr_sign(msg: bytes, privkey: int) -> tuple[int, int]: ...   # BIP-340
def bip32_derive(seed: bytes, path: str) -> tuple[int, ECPoint]: ... # hardened only
```

### 14.2 · DIR-1B — Groth16 R1CS → QAP → Prove/Verify

**Source:** DIR-1 `1B`, P3 (Groth16 → PLONK → STARK), §13 Algebraic Geometry
**New file:** `src/research/groth16_minimal.py`

```python
# Minimal Groth16 over BN254 (not production — educational)
# R1CS: Az∘Bz = Cz; z = (public‖witness‖1)
# QAP: Lagrange interpolation → polynomial h(x)t(x)
# CRS: trusted setup (simulated; τ←$ random for research)
# Prove/Verify: 3 pairing equations
# Purpose: understand ZK-SNARK math that underlies ZCash, StarkEx, Tornado
```

### 14.3 · DIR-1C — Kyber-768 NTT Ring → Enc/Dec → FO Transform

**Source:** DIR-1 `1C`, P6 (Kyber-768 Full Spec), §5 Lattice Theory
**New file:** `src/research/kyber768_reference.py`

```python
# Reference implementation of Kyber-768:
# NTT ring: Rq = ℤ₃₃₂₉[x]/(x²⁵⁶+1); NTT via ω=17
# KeyGen: A = Sam(ρ); s,e ← CBD_η₁; t = A∘NTT(s)+e
# Encapsulate: u,v computed; K = H(m‖H(pk))
# Decapsulate: m' recovered; FO re-encryption check
# Purpose: verify parameter choices; future swap from X25519
```

### 14.4 · DIR-1D — BLS12-381 Miller Loop → BLS Sign/Aggregate/Verify

**Source:** DIR-1 `1D`, P5 (BLS Signatures), §4 Elliptic Curve Theory (Optimal Ate)
**New file:** `src/research/bls12381_aggregate.py`

```python
# BLS12-381: G₁ (381-bit), G₂ (twist), Gₜ (𝔽p¹²*); embedding degree k=12
# Sign:      σ = sk·H(m) ∈ G₁
# Verify:    e(σ,G₂) = e(H(m),pk)
# Aggregate: σ_agg = Σσᵢ; verify Πᵢe(H(mᵢ),pkᵢ)
# Application: verify ETH2 validator attestation aggregates in on-chain analysis
# Library: py_ecc (Ethereum Foundation)
```

### 14.5 · DIR-1E — FROST t-of-n Threshold Signature

**Source:** DIR-1 `1E`, P5 BLS + CLSAG concepts
**New file:** `src/research/frost_threshold.py`

```python
# FROST (Flexible Round-Optimized Schnorr Threshold):
# Pedersen DKG → share signing → aggregate
# Application: multi-sig API key management (t-of-n signers must approve live orders)
# Future production use in §9.3 credential vault
```

### 14.6 · DIR-1F — BGV FHE: Key Gen → Enc → Add/Mult → Bootstrapping

**Source:** DIR-1 `1F`, P4 (FHE Full Mathematics)
**New file:** `src/research/bgv_fhe_stub.py`

```python
# BGV parameters: Rq = ℤq[x]/(xⁿ+1); n=4096; q = product of primes
# Enc: c = (a'b+e'+Δm, a'a+e'')
# Add: pointwise; noise grows linearly
# Mult: requires relinearization via eval keys
# Bootstrap: refresh noise budget (costly: ~10⁴ NTTs)
# Future use: compute ensemble consensus on encrypted engine outputs (private trading)
# Library: OpenFHE Python bindings (open-fhe.org)
```

### 14.7 · DIR-1G — Bulletproof Range Proof

**Source:** DIR-1 `1G`, P7 (Monero Bulletproofs)
**New file:** `src/research/bulletproof_range.py`

```python
# Bulletproof: prove v ∈ [0, 2⁶⁴) without revealing v
# Inner product argument: ⟨aL,aR⟩=0, aL∘aR=aL-1ⁿ
# Proof size: 2·log₂(64)+13 = 25 group elements ≈ 675B
# Application: prove position size is within risk limits without revealing exact size
# (private order sizing — future ZK-based risk disclosure)
```

### 14.8 · DIR-1H — STARK: AIR → FRI → Merkle → Proof → Verify

**Source:** DIR-1 `1H`, P3 STARK section
**New file:** `src/research/stark_minimal.py`

```python
# AIR (Algebraic IOP): boundary + transition constraints as polynomials
# FRI: prove degree < d; domain halves each round; O(log²n) hashes
# Post-quantum: ✅ hash-based (Poseidon); no trusted setup
# Application: verifiable computation proofs for backtesting results
# (prove backtest was computed correctly without revealing strategy parameters)
```

---

## Category 15 · DIR-2 Attack Implementations (Security Research)

**Purpose:** Implement every attack from DIR-2 as a self-contained script in `scripts/security/`.
These are audit tools — used to verify Trade-Bot's own implementations are not vulnerable.

### 15.1 · 2A — ECDSA k-Reuse: Algebraic Recovery + LLL Partial Nonce

**New file:** `scripts/security/ecdsa_krecovery.py`
```
Given two (r,s) signatures with same k:
  k = (z₁-z₂)·(s₁-s₂)⁻¹ mod n
  d = (s₁k-z₁)·r⁻¹ mod n
LLL extension: partial nonce leakage (m=128 bits known, 256-bit key)
  Lattice attack recovers d with O(n/m) signatures
Test: verify Trade-Bot signing never produces same r value (RFC 6979 invariant)
```

### 15.2 · 2B — Pohlig-Hellman: Smooth Order Group DLP

**New file:** `scripts/security/pohlig_hellman.py`
```
Group G order N = Π pᵢᵉⁱ
BSGS in each prime-power subgroup: O(√pᵢ) per factor
CRT reconstruction: x mod N
Test: confirm all groups used in Trade-Bot have prime order (h=1)
```

### 15.3 · 2C — Coppersmith: LLL Small Roots → Wiener → Boneh-Durfee

**New file:** `scripts/security/coppersmith_rsa.py`
```
Wiener: continued fraction attack on e/n → recover d if d < n^0.25/3
Boneh-Durfee: d < n^0.292 via polynomial system + LLL
Test: validate any RSA key used (exchange webhook pubkeys) against Wiener bound
```

### 15.4 · 2D — DPA on AES: Power Trace CPA Key Recovery

**New file:** `scripts/security/dpa_aes_sim.py`
```
Simulated DPA: generate synthetic power traces T = HW(Sbox[p⊕k]) + noise
CPA: ρ(k) = corr(H(d,k), T matrix column)
k* = argmax ρ(k)
Required traces: SNR⁻² (simulate at various SNR levels)
Purpose: educational; verify AES masking countermeasures work on simulated traces
```

### 15.5 · 2E — Differential Cryptanalysis: Reduced-Round AES DDT

**New file:** `scripts/security/differential_aes.py`
```
DDT[α][β] = #{x: Sbox[x⊕α]⊕Sbox[x] = β}
AES S-box: δ=4 (optimal differential uniformity for 8-bit bijection)
Compute full DDT, verify max entry = 4
Compute 2-round differential characteristic probability
Purpose: verify AES S-box properties; educational reference
```

### 15.6 · 2F — LLL on NTRU: Lattice Embedding Key Recovery

**New file:** `scripts/security/ntru_lll.py`
```
NTRU: f·h ≡ g (mod q); privkey = f
Lattice embedding: [[I, H], [0, qI]] where H = rot(h)
LLL on embedding → recover f,g if parameters below security floor
Test: demonstrate attack fails on Falcon-512 parameters (β=245 floor)
```

### 15.7 · 2G — 51% Markov Model: Profitability Surface

**New file:** `scripts/security/selfish_mining_surface.py`
```
Compute revenue_ratio(α, γ) over α ∈ [0, 0.5], γ ∈ [0, 1]
Plot 3D surface: where is selfish mining profitable?
Profitable region: α > 1/(3+γ)
For γ=0: α > 0.333; for γ=1: α > 0.25
Output: PNG heatmap to scripts/security/selfish_mining_surface.png
```

### 15.8 · 2H — Fault Attack: RSA-CRT Glitch → Bellcore → Factor n

**New file:** `scripts/security/rsa_crt_fault.py`
```
Bellcore attack: one correct sig s, one faulty sig s'
  gcd(s - s', n) = p (one factor recovered)
Requires: ability to inject fault during CRT signing
Test: verify Trade-Bot never uses RSA-CRT for signing
Mitigation documented: use Ed25519 (no CRT path exists)
```

---

## Category 16 · DIR-3 Research Analysis (Notebooks + Scripts)

**Purpose:** Implement DIR-3 research tasks as analysis scripts in `scripts/research/`.

### 16.1 · 3A — SNARK Comparison Matrix

**New file:** `scripts/research/snark_comparison.py`
```
Compare: Groth16 / PLONK / STARK / Bulletproof / Nova
Dimensions: proof_size_bytes, prover_time_ms, verifier_time_ms, trusted_setup, post_quantum
Output: markdown table + CSV
Data sourced from: benchmarks.ethereum.org, zka.lc, public papers
```

### 16.2 · 3B — Quantum Timeline: Logical Qubit Estimates

**New file:** `scripts/research/quantum_timeline.py`
```
Reproduce Roetteler et al. 2017 qubit estimates:
  ECDSA-256: 9n+2⌈log n⌉+10 ≈ 2330 logical qubits; T-gates ≈ 2^40
  RSA-2048: ~4096 logical qubits; T-gates ~10^10
Error correction overhead: surface code, ~10^3:1 physical:logical ratio
Timeline scenarios: optimistic (2030), consensus (2035), conservative (2040+)
Output: table + risk calendar for Trade-Bot's cryptographic migration
```

### 16.3 · 3C — HNDL: BTC Address Reuse Exposure Model

**New file:** `scripts/research/hndl_btc_exposure.py`
```
P2PK UTXO set (exposed pubkeys today): query via public API
P2PKH reused addresses: scan public blockchain
Compute: total BTC at HNDL risk by type
Model: if CRQC arrives year Y, fraction of BTC supply attackable
Output: risk table per address type + year
```

### 16.4 · 3D — SQISign Viability Analysis

**New file:** `scripts/research/sqisign_analysis.py`
```
SQISign: quaternion algebra → Deuring correspondence → signing
Signature size: 204 bytes (smallest PQ sig)
Signing speed: ~seconds on modern CPU (vs ms for Ed25519)
Verification: fast (~1ms)
Analysis: when does signing latency become acceptable for Trade-Bot API auth?
Threshold: if batch signing ≥ N orders → SQISign amortized cost acceptable
```

### 16.5 · 3E — Nova/Supernova: Folding → Recursive Proof → zkEVM

**New file:** `scripts/research/nova_folding.py`
```
Relaxed R1CS folding: (u₁,w₁)+(u₂,w₂)→(u,w) with slack vector E
Accumulate n steps in O(n) field ops; prove once at end
Use case for Trade-Bot: recursively prove all trades in a session are valid
Without revealing individual trade details (private P&L reporting)
```

### 16.6 · 3F — FHE On-Chain: CKKS Noise Budget Analysis

**New file:** `scripts/research/ckks_noise_analysis.py`
```
CKKS: message space ℝ^{n/2}; encode → scale by Δ
Noise budget after k multiplications: log₂Δ - k·log₂q_level
Bootstrap cost: O(n log n) NTTs; ~10⁴ NTTs per bootstrap
Analysis: can E-09 (ML inference) run under FHE with acceptable precision?
Circuit depth needed for XGBoost inference → noise budget estimate
```

### 16.7 · 3G — iO from Lattices: Current Status 2026

**New file:** `scripts/research/indistinguishability_obfuscation.md`
```
iO = "holy grail": all cryptography from one primitive
Current constructions: multilinear maps (Lin-Matt, DQMS)
Security status 2026: theoretical; no practical construction
Crypto impact: iO → one-way permutations, PKE, FHE, ZK — all from iO
For Trade-Bot: academic reference only; no near-term implementation path
```

---

## Updated Implementation Order (All Categories)

| Phase | Categories | Deliverables |
|---|---|---|
| P1 | CAT-1 | Data pipeline (orderbook, Deribit, sentiment, macro, RSS, quality gate) |
| P2 | CAT-2 | Reuse engines E-01,02,04,05,06,07,08,09 |
| P3 | CAT-5 math | Advanced math utilities (YZ vol, DFA, Transfer Entropy, Wasserstein) |
| P4 | CAT-3 | Custom engines E-03,10,11,12,13,14,15,16,17,18 |
| P5 | CAT-4 | Depth Detector v2 (9 regimes) |
| P6 | CAT-5 | Consensus Layer v2 (Bayesian CI, Chauvenet, Kelly, TTL) |
| P7 | CAT-6 | Risk Quantification + Signal Gate + Circuit Breaker |
| P8 | CAT-7 | Backtesting framework (walk-forward, 9×18 accuracy matrix) |
| P9 | CAT-8 | Wiring + orchestrator + audit trail integration |
| P10 | CAT-9 | Security hardening (Ed25519, BIP-32, constant-time, Kyber stub) |
| P11 | CAT-10+11 | Market signals from crypto-economic math (MEV, 51%, HNDL, UTXO) |
| P12 | CAT-12 | Engine upgrades (NTT, LLL correlation, variance ratio test) |
| P13 | CAT-13 | Attack defense layer (A1–A6 defenses, DeFi data validator) |
| P14 | CAT-14 | ZK/FHE stubs + DIR-1 research implementations |
| P15 | CAT-15 | DIR-2 attack scripts (security audit tools) |
| P16 | CAT-16 | DIR-3 research analysis scripts + notebooks |

---

## Additional Dependencies (Part II)

```toml
# pyproject.toml additions for Part II
cryptography = ">=42.0"        # Ed25519, X25519, constant-time ops
python-jose = {extras=["ed25519"], version=">=3.3"}  # JWT EdDSA
py_ecc = ">=7.0"               # BLS12-381, BN254 pairing
fpylll = ">=0.6"               # LLL lattice reduction (CAT-12)
sympy = ">=1.12"               # prime validation, number theory (CAT-13)
# Optional / research only:
open-quantum-safe = ">=0.9"    # liboqs: Kyber, Dilithium (CAT-9)
open-fhe = ">=1.1"             # BGV/BFV/CKKS FHE (CAT-14)
```
- **giotto-tda training**: only inference on pre-computed complexes (keeps P8 < 10s SLA).
