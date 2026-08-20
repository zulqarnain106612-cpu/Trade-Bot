# Market Microstructure Reference (New in v3)

## Core Concepts

### Bid-Ask Spread Decomposition
```
Quoted Spread = Inventory Component + Adverse Selection Component + Order Processing Cost
Effective Spread = 2 × |Trade Price - Midpoint|
Realized Spread = 2 × side × (Trade Price - Future Midpoint_{t+Δ})
Price Impact = Effective Spread - Realized Spread
```
- Realized spread: dealer's profit; price impact: information content of trade
- Adverse selection = price impact component; measures how informed the order flow is
- High adverse selection spread → informed trading is occurring → signal opportunity
  but also execution cost amplification

---

## Order Flow Imbalance (OFI)

### Construction
```python
def order_flow_imbalance(bids: pd.DataFrame, asks: pd.DataFrame,
                          level: int = 1) -> pd.Series:
    """
    OFI at level L: change in bid quantity - change in ask quantity at best L levels.
    Positive = buying pressure; Negative = selling pressure.
    Cont et al. (2014), updated by Kolm, Turiel & Westray (2023).
    """
    bid_change = bids[f"L{level}_qty"].diff().fillna(0)
    ask_change = asks[f"L{level}_qty"].diff().fillna(0)
    # Positive bid change (more bids) = buying pressure
    bid_pressure = bid_change.clip(lower=0)
    # Negative bid change (bids removed) = buying pressure leaving
    bid_leave = bid_change.clip(upper=0)
    ask_pressure = ask_change.clip(upper=0)  # More asks = selling pressure
    ask_leave = ask_change.clip(lower=0)
    return (bid_pressure + bid_leave) - (ask_pressure + ask_leave)

def multi_level_ofi(bids, asks, levels=5, weights=None) -> pd.Series:
    """Weighted sum of OFI across L levels (Kolm et al. 2023)."""
    if weights is None:
        weights = [1 / (i + 1) for i in range(levels)]  # Depth decay
    ofi = sum(w * order_flow_imbalance(bids, asks, i + 1)
              for i, w in enumerate(weights))
    return ofi / sum(weights)
```

### OFI as Trading Signal
- Short-horizon return prediction: OFI predicts mid-price returns at horizons 10s–5min
- Normalize: z-score vs rolling 30-minute window; raw OFI not comparable across assets
- OFI on multiple exchanges: composite OFI captures informed flow even if split across venues
- Decay: OFI predictability decays rapidly; effectiveness window typically < 10 minutes

---

## Adverse Selection

### Glosten-Milgrom Model
```
Market Maker sets:
  Ask = E[V | buyer] = E[V | informed buyer with probability μ, uninformed with 1-μ]
  Bid = E[V | seller]

Spread = 2 × μ × (V_high - V_low) / 2
where μ = probability that counterparty is informed
```
- Higher informed trading probability (μ) → wider spread → higher adverse selection cost
- Implementation: estimate μ from PIN model (see below)
- Practical: if you are trading large size on thin books, you are the adverse selection
  for market makers; your expected slippage scales with your information signal quality

### PIN Model (Probability of Informed Trading)
```
Estimation via MLE on daily buy/sell order counts:
  P(buys=B, sells=S) = αμ × Poisson(B; ε+μ) × Poisson(S; ε)      [good news day]
                      + α(1-μ) × Poisson(B; ε) × Poisson(S; ε+μ)  [bad news day]
                      + (1-α) × Poisson(B; ε) × Poisson(S; ε)      [no news day]

PIN = αμ / (αμ + 2ε)
where: α = probability of information event, μ = informed arrival rate, ε = uninformed
```
- PIN > 0.25: significant informed trading; adjust execution strategy
- Adapt for crypto: buy/sell classification via tick-test rule or Lee-Ready algorithm
- Higher PIN assets: wider spread, faster adverse selection on execution

---

## Market Impact

### Almgren-Chriss (Linear Impact)
```
Temporary impact: h(v) = η × σ × (v / V)^0.5     [square root model]
Permanent impact: g(v) = γ × σ × v / V
Total cost = Temporary × shares + Permanent × shares
```
- `v` = trade rate (shares/time); `V` = market volume (shares/time)
- Optimal execution: liquidate Q shares in T time → minimize `E[cost] + λ × Var[cost]`
- Crypto calibration: η and γ estimated from actual fill data; re-calibrate monthly

### Square-Root Market Impact (Empirical)
```
Impact ≈ σ × k × √(Q / ADV)
```
- `k ≈ 0.3–0.5` for crypto (higher than equities due to thinner books)
- `σ` = daily realized volatility; `ADV` = average daily volume
- Use to pre-estimate execution cost before order; compare to edge

### Implementation Shortfall
```
IS = Decision Price - Arrival Price    (opportunity cost of delay)
   + Market Impact                     (price movement from our order)
   + Commissions + Spread              (explicit costs)
```
- Track IS per order; trend up = execution quality degrading
- Compare to VWAP benchmark for regulatory best execution

---

## AMM vs CLOB Microstructure

### AMM (Uniswap v3)
| Property | AMM |
|---|---|
| Liquidity | Continuous (within range) |
| Price determination | Formula: `x × y = k` |
| Adverse selection | LPs adversely selected by informed traders; pay information cost |
| Spread | Implicit in fee tier (0.01%, 0.05%, 0.3%, 1%) |
| Execution | Guaranteed fill (if liquidity exists); deterministic slippage |
| Order types | Market only (atomic); no limit orders natively |
| MEV exposure | High (sandwich attacks, JIT) |

### CLOB (Binance, Hyperliquid)
| Property | CLOB |
|---|---|
| Liquidity | Discrete (order book levels) |
| Price determination | Order matching engine |
| Adverse selection | Maker: adverse selection risk; Taker: explicit spread cost |
| Spread | Quoted bid-ask |
| Execution | Conditional (depends on order book state) |
| Order types | Limit, Market, Stop, OCO, etc. |
| MEV exposure | Low (sequenced matching engine) |

### Hybrid DEX (Hyperliquid, dYdX v4)
- On-chain CLOB: order book lives on-chain (or dedicated chain); matching is deterministic
- Benefits: CLOB price discovery, on-chain settlement, no exchange counterparty risk
- Limitations: network latency affects order placement; validator risk instead of exchange risk

---

## Tick Size and Microstructure

### Tick Size Effects
- Small tick relative to spread: many price levels; continuous liquidity
- Large tick relative to spread: clustering at round numbers; queue priority matters

### Queue Position
- Pro-rata matching: fill proportional to size at price level → size advantage
  (common on CME, Deribit options)
- FIFO (first-in first-out): time priority → speed advantage (crypto spot/perps)
- For FIFO venues: post-only orders placed early; refresh before cancellation to
  maintain queue position

---

## Trade Classification

### Lee-Ready Algorithm (limit order data)
```python
def classify_trade(price: float, bid: float, ask: float,
                   prev_price: float = None) -> int:
    """Returns +1 (buy) or -1 (sell)"""
    midpoint = (bid + ask) / 2
    if price > midpoint + 0.001:
        return +1  # Buyer-initiated
    elif price < midpoint - 0.001:
        return -1  # Seller-initiated
    else:
        # Midpoint trade: use tick rule
        if prev_price is not None:
            return +1 if price > prev_price else -1 if price < prev_price else 0
        return 0  # Indeterminate
```

### Bulk Volume Classification (BVC — Easley et al.)
```
P(buy | volume) = Φ(ΔP / σ_ΔP)  where Φ = standard normal CDF
```
- Simpler; no tick data required; works on OHLCV bars
- Used in VPIN construction (Volume-synchronized PIN)

---

## VPIN (Volume-synchronized PIN)

```
VPIN = Σ|V_buy - V_sell| / (n × V_bar)
```
- Measures order flow toxicity; high VPIN = informed trading; precedes flash crashes
- May 6, 2010 Flash Crash: VPIN elevated before crash (Easley, López de Prado, O'Hara)
- Crypto VPIN: use 50-bar rolling window; threshold at 85th percentile → elevated toxicity
- Integration with Law 13: VPIN spike + thin books + large OFI → LAW13 signal suppression

---

## Liquidity Metrics for Execution Planning

```python
def amihud_illiquidity(returns: pd.Series, volume_usd: pd.Series,
                        window: int = 30) -> pd.Series:
    """
    Amihud (2002): |return| / volume. Higher = more illiquid.
    Suitable for daily data; not for HFT.
    """
    return (returns.abs() / volume_usd).rolling(window).mean()

def kyle_lambda(price_changes: pd.Series, signed_volume: pd.Series,
                window: int = 100) -> float:
    """
    Kyle's lambda: OLS regression ΔP = λ × signed_volume + ε
    Higher lambda = price more sensitive to order flow = less liquid
    """
    from sklearn.linear_model import LinearRegression
    X = signed_volume.values.reshape(-1, 1)
    y = price_changes.values
    model = LinearRegression().fit(X, y)
    return float(model.coef_[0])  # λ
```

### Liquidity Tiers for Position Sizing
| Tier | Amihud Score | Max Single Order |
|---|---|---|
| L1 (highly liquid, BTC/ETH) | < 0.001 | 5% of ADV |
| L2 (liquid, majors) | 0.001–0.01 | 2% of ADV |
| L3 (moderate, mid-cap) | 0.01–0.1 | 1% of ADV |
| L4 (illiquid, small-cap) | > 0.1 | 0.5% of ADV; use TWAP |

---

## High-Frequency Trading Mechanics

### Latency Arbitrage
- Cross-venue price discrepancy: if Binance price leads OKX by N ms
  → statistical lead-lag relationship exploitable if execution RTT < lag duration
- Alpha decay: typically decays 50% within one exchange-to-exchange latency round-trip
- Profitability: only viable at co-location distance (< 1ms RTT)

### Market Making Mechanics
```
Avellaneda-Stoikov inventory model:
r(s, q) = s - q × γ × σ² × T   (reservation price adjusted for inventory)
δ_bid = 1/γ × ln(1 + γ/k) + (2q+1)/2 × √(σ²γ/k × (1 + γ/k)^(1+k))
δ_ask = similar, opposing direction
```
- `q` = current inventory (negative = short); `γ` = risk aversion; `k` = order arrival rate
- Key insight: adjust spread asymmetrically based on inventory position
- For crypto: add funding rate as inventory carry cost in perpetuals market making
