# Trading Strategies Reference

## Strategy Classification Framework

| Dimension | Options |
|---|---|
| Time horizon | HFT (μs–ms), scalping (s–min), intraday (min–h), swing (1–30d), position (> 30d) |
| Signal source | Statistical, ML, on-chain, fundamental, sentiment, cross-market |
| Execution | Auto (systematic), semi-auto (signal + human confirmation), manual |
| Market type | Spot, perps, options, prediction markets, yield, DeFi |
| Complexity | Single-signal, ensemble, RL, cross-asset, multi-leg |

---

## Momentum and Trend

### Time-Series Momentum (TSMOM)
```
signal(t) = sign(r_{t-h,t}) × f(volatility)
```
- 12-1 month horizon (skip last month — reversal effect)
- Crypto: shorter effective horizon (1–30d); test empirically per asset
- Volatility scaling: `size = target_vol / realized_vol_ewma`
- Downside to TSMOM: whipsaws in ranging markets; combine with regime gate

### Cross-Sectional Momentum (CSMOM)
- Rank assets by recent return; long top quartile, short bottom quartile
- Crypto: BTC dominance momentum often overrides cross-sectional signal
- Rebalance: daily or weekly; transaction costs dominate at higher frequency
- Universe filter: liquidity gate (> $10M daily ADV); exclude stablecoins, wrapped tokens

### Volume-Weighted Momentum
```python
def volume_momentum(closes, volumes, window=20):
    vwap = (closes * volumes).rolling(window).sum() / volumes.rolling(window).sum()
    return closes / vwap - 1  # Positive = price above VWAP (momentum)
```

---

## Mean Reversion

### Statistical Arbitrage
- Cointegration test (Engle-Granger, Johansen): p < 0.05 for pair selection
- Half-life of mean reversion: `τ = -ln(2) / ln(β)` from AR(1) fit
  Trading frequency ∝ 1/τ; very short half-life = HFT; longer = swing
- Hurst exponent: H < 0.5 = mean-reverting; H > 0.5 = trending
- Pairs crypto: BTC/ETH, ETH/BNB, SOL-perp/SOL-spot; validate cointegration quarterly

### Funding Rate Carry (Perps-Specific)
```
If funding > threshold:
  Short perp + Long spot → collect funding
Net yield = funding_rate × 3 × 365 − borrow_cost − execution_cost
```
- Extreme funding (> 0.1% per 8h): signal of pending squeeze; reduce before squeeze
- Composite funding: average across Binance, OKX, Bybit, Hyperliquid
  — single exchange anomaly vs market-wide divergence treated differently
- Risk: perp liquidation during large adverse move; maintain generous margin buffer
- Cross-exchange funding arb: same asset, different venues; delta-neutral net funding

### Basis Trading (Futures-Spot)
```
Basis = Futures_Price - Spot_Price
Carry = Basis / Spot × (Days_to_Expiry / 365)
```
- Positive basis: contango; short futures + long spot → earn roll yield
- Quarterly expiry: basis typically collapses at expiry — position accordingly
- ETF-adjacent basis: post-BTC ETF launch, institutional basis arb compressed annualized
  basis from >10% to 3–5%; model accurately in 2024+ environment

---

## On-Chain Signal Strategies

### Exchange Flow Strategy
- Exchange inflow spike (coins moving to exchange) → sell pressure signal
- Exchange outflow (withdrawal) → long-term holder accumulation signal
- Large wallet transfer: > $50M equivalent to exchange = pre-sell signal
- Sources: CryptoQuant, Glassnode, Nansen (see `on-chain-data.md`)

### NUPL (Net Unrealized Profit / Loss)
```
NUPL = (Market Cap - Realized Cap) / Market Cap
```
- > 0.75: euphoria zone; high probability of distribution top
- < 0: underwater on aggregate; historically strong long-term buy zone
- Crossing zero from below historically marks bull market resumption
- Use as slow regime indicator; not a timing signal

### Miner Flow
- Miner outflow spike → selling pressure (especially at end of month for opex)
- Miner reserve declining → bearish; building → bullish
- Hash ribbon: miner capitulation followed by recovery historically precedes rallies

---

## DeFi / On-Chain Execution Strategies

### Liquidity Provision (AMM LP)
- Select fee tier: 0.01% (stablecoin pairs), 0.05% (correlated), 0.3% (standard), 1% (exotic)
- Concentrated range: set ± 10–20% from current price; tighter = more fee capture,
  more active management, more IL risk
- Rebalancing: reset range when price exits; gas cost + IL from reset must be modeled
- JIT protection: Curve, Balancer pools less susceptible to JIT than Uniswap v3

### Restaking Yield Strategy (EigenLayer, 2024+)
```
Net APY = Staking APY + Restaking Premium - Slashing Probability × Slash Fraction
         - Smart Contract Risk Premium - LRT Liquidity Risk Premium
```
- Operator selection: diversify across ≥ 5 operators; no > 30% in single operator
- AVS selection: only AVSes with completed security audits and known slashing conditions
- LRT vs direct restaking: direct = gas efficiency; LRT = composability + liquidity
  (redeemable via DEX) but adds protocol risk layer
- Exit strategy: EigenLayer 7-day withdrawal delay; LRT DEX exit is instant but
  subject to depeg; model both paths in liquidity planning

### Prediction Market Strategy (Polymarket)
- Information edge: early information, good calibration of event probabilities
- Market inefficiency: markets often mis-price P < 0.05 events (log-odds compression)
- Kelly sizing: apply fractional Kelly (0.25×) to each position; independence assumption
  rarely holds in correlated event clusters
- Liquidity: check order book depth; many markets are thin; limit orders only
- Resolution risk: oracle-settled; disputed resolutions add tail risk

### MEV Strategy (Searcher-level)
- Arbitrage: atomic swap A→B on Uniswap, B→A on SushiSwap; profit net of gas
- Liquidation: detect undercollateralized positions, submit liquidation tx in same block
- Sandwich defense: if operating as LP, monitor for JIT attacks; adjust fee tier
- Private order flow: via MEV-Share/Flashbots; rebate sharing with users
- Gas auctions: priority fee competitive; model EV vs gas cost carefully
  High gas price = competitive arbitrage; walk away when margin negative after gas

---

## Prediction Signal Ensemble

### Signal Architecture
```python
class EnsembleSignal:
    def compute(self, features: Features) -> Signal:
        signals = {
            "momentum": self.momentum_model.predict(features),
            "mean_rev": self.mean_rev_model.predict(features),
            "on_chain": self.on_chain_model.predict(features),
            "sentiment": self.sentiment_model.predict(features),
        }
        # Regime-conditional weighting
        regime = self.regime_detector.current()
        weights = self.regime_weights[regime]  # from calibration
        # Confidence gate: suppress any signal below threshold
        filtered = {k: v for k, v in signals.items()
                    if v.confidence >= self.thresholds[k]}
        if not filtered:
            return Signal(action=HOLD, reason="all_signals_below_threshold")
        ensemble_score = sum(weights[k] * v.value for k, v in filtered.items()
                             if k in weights) / sum(weights[k] for k in filtered)
        return Signal(value=ensemble_score, confidence=min(v.confidence
                      for v in filtered.values()), regime=regime)
```

---

## Strategy Risk Parameters (required on deployment)

```yaml
strategy: momentum_btc_v2
capital_limit_usd: 500000
max_position_pct: 0.05          # Max 5% of capital in single position
max_drawdown_pct: 0.08          # 8% drawdown → suspend
kelly_fraction: 0.25
min_confidence: 0.65
signal_staleness_ms: 5000
max_orders_per_min: 10
exchanges: [binance, okx]       # Approved execution venues
regime_allowlist: [TRENDING, VOLATILE]  # Do NOT trade in RANGING
wash_window_ms: 60000
flash_crash_halt_threshold_pct: 5.0
```

---

## Backtesting Standards

### Minimum Requirements
- Walk-forward validation: rolling 252-day train, 63-day test; minimum 10 folds
- No lookahead: all features shifted by 1 bar minimum; audit with point-in-time data
- Transaction costs: include spread (1/2 bid-ask), fee tier, slippage model (square-root)
- Funding costs (for leveraged): include borrow rate and funding payments
- Partial fills: Monte Carlo fill simulation; 100% fill assumption = overstated returns
- Realistic position limits: liquidity constraint (max 1–5% ADV)

### Red Flags in Backtest
- Sharpe > 4.0: likely data leakage or overfitting
- Max drawdown < 2% on volatile assets: suspicious; check position sizing
- Win rate > 75% on trend-following: suspicious; verify fee model
- Works on all market regimes without regime gating: suspicious; regime testing required
- No degradation after 2020: model likely fitted on low-rate / COVID-recovery environment;
  validate on 2022 bear, 2024 consolidation, 2025 data separately

### Deflated Sharpe Ratio
- When testing N strategies: true SR threshold = `SR* × sqrt(1 + (1-γ)log(N))`
- Prevents false discovery from running many variations
- Required when strategy came from a search of > 20 parameter combinations
