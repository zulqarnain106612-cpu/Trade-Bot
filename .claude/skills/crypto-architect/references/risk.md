# Risk Reference

## Value at Risk (VaR)

### Methods
| Method | When to Use | Limitation |
|---|---|---|
| Historical Simulation | Sufficient history, non-normal | Looks backward only |
| Parametric (delta-normal) | Linear, normal returns | Fails for fat tails |
| Monte Carlo | Options, non-linear, complex | Computationally intensive |
| **FHS (Filtered Historical)** | **Default** | Requires GARCH fit |
| EVT-based | Extreme tail (> 99%) | Sparse data; parameter uncertainty |

### FHS Implementation
1. Fit GARCH(1,1) to returns per asset
2. Extract standardized residuals
3. Bootstrap residuals to simulate paths
4. Apply current volatility estimate to residuals
- Lookback: 252 days minimum; 500 recommended
- Re-estimate daily; intraday for HFT
- Backtest: Kupiec test (LR test) — violation rate must match α
  Traffic light: green (< 5 exceedances/250 days), yellow (5–9), red (> 9) → recalibrate

---

## CVaR / Expected Shortfall (ES)

```
CVaR_α = E[R | R ≤ -VaR_α]
```
- CVaR is coherent (subadditive); VaR is not — ES is primary metric (Basel III / FRTB)
- Always compute both; large gap (CVaR >> VaR) = heavy tail → investigate distribution

```python
def cvar(returns: np.ndarray, alpha: float = 0.05) -> float:
    var = np.percentile(returns, alpha * 100)
    return float(returns[returns <= var].mean())
```

---

## Drawdown

```
Drawdown(t) = (Peak(t) - Value(t)) / Peak(t)
Max Drawdown = max over t of Drawdown(t)
Calmar Ratio = Annualized Return / Max Drawdown
Ulcer Index = sqrt(mean(Drawdown(t)^2))  # path-dependent risk
Serenity Ratio = Sharpe / Ulcer Index    # penalizes sustained drawdown
```

### Rules
- Hard drawdown limit per strategy: hit → suspend, manual review required
- System-wide circuit breaker: aggregate drawdown → all auto-execution halted
- Drawdown measured on mark-to-market (open positions count, unrealized)
- Reset policy: explicit; daily/weekly/monthly; ambiguity = override attempts

### Reference Limits
| Strategy Type | Max Drawdown | Circuit Breaker |
|---|---|---|
| Scalping / HFT | 1–2% | 3% system-wide |
| Day trading | 3–5% | 7% system-wide |
| Swing | 8–12% | 15% system-wide |
| Multi-strategy | Weighted average | 10% system-wide |

---

## Options Greeks (mandatory for structured positions)

### First-Order Greeks
```
Delta (Δ): ∂V/∂S  — sensitivity to underlying price; ranges [-1, 1] for vanilla options
Vega (ν):  ∂V/∂σ  — sensitivity to implied volatility
Theta (Θ): ∂V/∂t  — time decay (usually negative for long options)
Rho (ρ):   ∂V/∂r  — interest rate sensitivity (minor for crypto short-dated)
```

### Second-Order Greeks
```
Gamma (Γ): ∂²V/∂S² — rate of change of delta; high near ATM
Vanna:     ∂²V/∂S∂σ — delta sensitivity to vol
Volga:     ∂²V/∂σ²  — vega sensitivity to vol; convexity of option vs vol
```

### Greeks Risk Management
- Net delta exposure: sum across all positions (long/short, calls/puts); cap per strategy
- Gamma threshold: large positive gamma = P&L accelerates favorably; large negative
  gamma = losses accelerate adversely → hard cap on short gamma exposure
- Delta-hedging frequency: for strategies with significant net delta, define
  re-hedge trigger (e.g., delta drift > 5% of notional)
- Volatility surface: monitor term structure (contango vs backwardation in vol);
  skew (put/call ratio); regime shift in vol surface = review all options positions
- Deribit dominance: for crypto options, Deribit is primary price discovery;
  reference Deribit IV surface for all vol-based risk calculations

### Greeks Stress Test
- Apply simultaneous shock: -30% S, +50% σ → compute portfolio P&L via full revaluation
- Crypto-specific: vol can spike 3–5× in crashes (LUNA, FTX); use scenario shock of
  σ_shocked = 3 × current IV for downside stress

---

## Position Sizing Hierarchy

Apply in order — each step can only reduce size, never increase:

1. Kelly fraction → theoretical max
2. Volatility scaling: `size = target_vol / asset_vol`
3. Drawdown constraint: size that keeps expected drawdown < limit
4. Liquidity constraint: max 1–5% of 24h ADV
5. Exchange limit: margin, notional, leverage caps
6. Liquidation buffer: ensure ≥ 2× maintenance margin headroom
7. Conformal interval gate: if prediction interval from conformal model is wide
   (width > k × expected_move), reduce size proportionally

### EWMA Volatility
```
σ²_t = λ × σ²_{t-1} + (1-λ) × r²_t
λ = 0.94 (daily); λ = 0.97 (intraday)
```
- Rebalance trigger: size drift > 20% from target

---

## Liquidation Cascade Modeling

### Hyperliquid Dec 2024 — Reference Scenario
- A single large ETH position (~$200M notional) was opened with extreme leverage
- Position was too large relative to market depth; no sufficient counterpart liquidity
- Funding flipped; position was liquidated; HLP (liquidity pool) absorbed losses
  ($4M+ socialized to vault LPs)
- Lessons:
  1. Monitor OI concentration on perpetual exchanges — if single position > 5% of total OI,
     cascade risk is elevated
  2. HLP/insurance fund depletion risk: if protocol's backstop fund is small, socialized
     loss applies to profitable positions via ADL
  3. Extreme funding before liquidation = early warning signal; reduce exposure

### Cascade Stress Test (mandatory)
```
Scenario:
  - Simulate 30%, 40%, 50% collateral price drop
  - Identify liquidation price of each open position
  - Model market impact of forced liquidations (square-root model)
  - Estimate second-order price impact from concurrent liquidations
  - Assess portfolio survival under full cascade (assume ρ → 1.0)
  - Reference: Hyperliquid Dec 2024, LUNA May 2022, 3AC Jun 2022
```

### ADL (Auto-Deleveraging) Risk
- Profitable positions force-reduced to cover losers when insurance fund depleted
- Monitor ADL rank continuously: rank 1 = highest risk of ADL
- Strategy: if ADL rank is 1 for any position, reduce or close proactively
- Include ADL scenario in stress test: if insurance fund depletes by 50%, what is P&L impact?

---

## Correlation and Portfolio Risk

### Correlation Matrix Maintenance
- Rolling (252-day window) updated daily; DCC-GARCH for time-varying
- Stress assumption: ρ → 1.0 for crash scenarios (empirically validated by LUNA, FTX)
- LRT correlation: wstETH and LRT tokens (weETH, pufETH) highly correlated to ETH;
  treat as same factor for risk aggregation
- ETF inflow correlation: BTC spot ETF flow correlated with BTC price;
  include as factor; monitor for break of correlation

### Concentration Risk
- HHI of position weights: > 0.25 = concentrated; reduce
- Max single-asset weight: 25% of portfolio
- BTC-correlated bucket: sum exposure ≤ 50% of portfolio
- Restaking bucket: EigenLayer + LRT exposure → treat as single correlated cluster;
  cap at 10% of capital given novel slashing risk

### Stress Scenarios (run quarterly + before major changes)
Historical:
- COVID crash (Mar 2020): -50% crypto, -30% equities, vol spike 5×
- LUNA depeg (May 2022): USDT/USDC briefly de-pegged; stablecoin risk crystallized
- FTX collapse (Nov 2022): exchange counterparty risk, ADA/SOL -80%
- 3AC liquidation (Jun 2022): leveraged crypto fund cascade; BTC -40% in weeks
- Bybit hack (Feb 2025): $1.5B ETH drained; ETH price declined; market uncertainty
- Hyperliquid cascade (Dec 2024): perp exchange insurance model stress
- BTC ETF launch vol (Jan 2024): vol spike on approval; reversal trap

Hypothetical:
- 60% BTC drop in 7 days; exchange halt (single primary venue)
- USDT depeg to $0.90; USDC redemption gate activated
- DeFi oracle manipulation of Chainlink price feed
- L1 network outage (Solana-style halt, 4h)
- State-sponsored exchange seizure in top jurisdiction

---

## DeFi-Specific Risk

### Impermanent Loss (LP Positions)
```
IL = 2√(P_ratio) / (1 + P_ratio) - 1
```
- At 2× price move: IL = −5.7%; at 4× = −20%; at 10× = −42%
- Concentrated liquidity (v3): IL amplified relative to range width
- LRT LP positions: compound IL with depeg risk and slashing risk

### Oracle Risk (updated v3)
- Spot price oracle: manipulable within single block (flash loan)
- TWAP (15-min minimum): resistant to single-block manipulation
- Chainlink: dominant; monitor node operator health; check heartbeat deviation
- Pyth (Solana-native, EVM via Wormhole): pull-based, sub-second updates;
  use for high-frequency DeFi; validate publisher count before trusting
- Multi-oracle median: Chainlink + Pyth + Uniswap v3 TWAP → median; alert on > 1% divergence

---

## Risk-Adjusted Performance Metrics

| Metric | Formula | Target |
|---|---|---|
| Sharpe Ratio | (R - Rf) / σ | > 1.5 annualized |
| Sortino Ratio | (R - Rf) / σ_downside | > 2.0 |
| Calmar Ratio | R_annual / MaxDD | > 1.0 |
| Omega Ratio | E[R+] / E[R-] | > 1.5 |
| Information Ratio | (R - Benchmark) / TE | > 0.5 |
| Serenity Ratio | Sharpe / Ulcer Index | > 1.0 |
| Deflated Sharpe | Sharpe adjusted for multiple trials | > 0 (Bailey & Lopez de Prado) |

- Compute on out-of-sample data only
- Minimum 90-day live track record before capital scaling
- Newey-West correction on Sharpe for autocorrelated returns
- Deflated Sharpe: use when ranking strategies from a large search space
