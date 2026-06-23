# Bayesian Risk Scoring
**Domain**: probability | **Tags**: bayesian, prior, posterior, likelihood, risk, probability, inference, update

## Bayesian Risk Scoring — Architecture Reference

### Framework for this project
Prior: P(trade_profitable) based on historical win rate
Likelihood: P(signal_fired | trade_profitable) from meta-label model
Posterior: P(profitable | signal) = likelihood × prior / evidence

### Practical Bayesian risk gate
score = P(bet) × P(long) × regime_confidence × (1 - drawdown_penalty)
- P(bet): meta-label output (already Bayesian update on direction signal)
- P(long): direction model output
- regime_confidence: 1 - entropy_normalized
- drawdown_penalty: current_dd / daily_dd_limit

Threshold: score > 0.6 → allow trade (tune via CPCV on paper data)

### Sequential Bayesian updating (for model drift detection GAP-003)
After each trade: update win_rate estimate
posterior_alpha = prior_alpha + n_wins
posterior_beta  = prior_beta  + n_losses
Expected win rate = alpha / (alpha + beta)
95% CI: Beta(alpha, beta).ppf([0.025, 0.975])
When CI lower bound < 0.48: trigger retrain alert

### Monte Carlo for position sizing uncertainty
Simulate 10,000 Kelly paths with sampled (p, b) from posterior
Use 5th percentile Kelly as conservative bet size
Eliminates parameter uncertainty risk
