# Kelly Criterion & Sizing
**Domain**: quant_finance | **Tags**: kelly, sizing, bet, fraction, position, capital, edge, odds

## Kelly Criterion — Architecture Reference

Full Kelly: f* = (p*b - q) / b  where p=win_prob, b=net_odds, q=1-p
For continuous returns: f* = μ / σ²  (mean/variance)

### This project uses Half-Kelly (multiplier=0.5)
- Rationale: Full Kelly causes 50% drawdowns in practice (Thorp 2006)
- Ceiling: 25% of capital max — prevents single position dominance
- Thorp variance-adjusted: f = f* × (1 - portfolio_variance_contribution)

### Multi-strategy Kelly (when adding symbols)
f_combined = Σ(f_i × ρ_ij) where ρ is correlation matrix
Correlated positions: effective Kelly shrinks by sqrt(correlation)

### Kelly failure modes to watch
- Estimating p wrong by 1% → 4% sizing error (quadratic sensitivity)
- Fat tails: realized Kelly assumes Gaussian — crypto violates this
- Solution: use log-normal Kelly or reduce multiplier to 0.25× in volatile regime

### Sizing pipeline in this project
Kelly → Carver forecast scalar → AFML bet-size → Thorp variance-adj → regime scalar → gate
