# VaR and CVaR Risk Metrics
**Domain**: probability | **Tags**: var, cvar, value, risk, tail, expected, shortfall, drawdown, loss

## VaR & CVaR — Architecture Reference

### Value at Risk (VaR)
VaR_95 = μ - 1.645×σ  (parametric, Gaussian assumption)
Historical VaR_95: 5th percentile of return distribution
For crypto: historical >> parametric (fat tails violate Gaussian)

### CVaR (Expected Shortfall) — better than VaR
CVaR_95 = E[loss | loss > VaR_95]
= mean of worst 5% of outcomes
CVaR is coherent (subadditive) — VaR is not

### Application to this project
Daily DD limit = 2% = informal VaR_100 (hard stop not probability)
To compute true CVaR: use rolling 252-bar return window
CVaR_95 on 15m bars → annualized → compare to Sharpe for risk-adjusted view

### Connecting to Kelly ceiling
Kelly ceiling 25% implicitly caps CVaR:
Max loss per trade ≈ 25% × stop_loss_pct × capital
CVaR of portfolio ≈ Kelly_fraction × σ × √(holding_period)
