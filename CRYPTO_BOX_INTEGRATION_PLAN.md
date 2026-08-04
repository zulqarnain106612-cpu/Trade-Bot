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

- **Part II of Crypto_Box** (Cryptographic Foundations): purely educational math reference.
  No implementation items. The attack math (ECDSA nonce reuse, DPA, lattice attacks) has
  zero integration points with a trading bot. Excluded deliberately.
- **Live on-chain blockchain node**: G-12 fix uses free APIs instead.
- **giotto-tda training**: only inference on pre-computed complexes (keeps P8 < 10s SLA).
