# DeFi & Cryptocurrency Risk Models
**Domain**: blockchain | **Tags**: defi, liquidity, amm, impermanent, loss, funding, rate, perpetual, basis, crypto

## DeFi & Crypto Risk — Architecture Reference

### Funding rate risk (relevant for perpetual futures)
Funding = position_size × funding_rate × holding_hours/8
Typical BTC funding: ±0.01% per 8h (neutral) → ±0.03% in extreme
At 25% position: daily funding drag up to 0.09% of capital
Gate: if |funding_rate| > 0.05% per 8h → reduce position by 50%

### Exchange counterparty risk
Binance: largest by volume, SAFU fund ($1B), but centralized
OKX: secondary — good as failover but not as primary large-position venue
Mitigation: never keep > 10% of capital on exchange — withdraw profits

### Basis risk (spot vs futures divergence)
BTC spot vs perpetual premium/discount = basis
Basis > 0.5%: contango (longs pay) — unfavorable for long bias
Basis < -0.5%: backwardation (shorts pay) — unfavorable for short bias
Current fetcher only gets spot — add basis fetch for risk awareness

### Liquidity risk on rapid exit
BTC/USDT orderbook depth: typically $5-20M within 0.1%
At $10K capital × 25% position = $2.5K order: negligible impact
Scales: at $1M capital, market impact becomes significant (→ GAP-001)

### Volatility regime memory
Crypto vol clusters: GARCH(1,1) captures ~60% of vol autocorrelation
ATR-based vol explosion gate (2× median ATR) = informal GARCH threshold
Consider: EGARCH for asymmetric vol (down moves more persistent)
