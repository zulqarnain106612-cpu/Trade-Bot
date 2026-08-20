# Probability & Statistical Methods Reference

## Kelly Criterion

### Formula
```
f* = (bp - q) / b = edge / odds
```
- **Never use full Kelly on live systems** — estimation error in p and b is always present
- Use **fractional Kelly**: `f = 0.25 × f*` to `f = 0.5 × f*`
- Negative Kelly (f* < 0) = do not trade
- Kelly is optimal only for i.i.d. outcomes; financial returns are not i.i.d.
  → treat fractional Kelly as upper bound; combine with volatility scaling

### Regime-Conditional Kelly (v3)
Different regimes have different win rates and odds; Kelly must condition on regime:
```python
def regime_kelly(regime: str, win_rate_by_regime: dict, b: float) -> float:
    p = win_rate_by_regime.get(regime, 0.5)
    q = 1 - p
    f_star = (b * p - q) / b
    return max(0.0, 0.25 * f_star)  # fractional Kelly; never negative
```
- Regime detection (HMM, see below) must precede Kelly calculation
- If regime is UNCERTAIN: use minimum Kelly across all known regimes

### Multi-Asset Kelly
```
f* = Σ^{-1} · μ
```
- `Σ` = covariance matrix; `μ` = expected excess returns
- Solve as constrained QP: long-only, leverage limits, concentration limits
- Minimum-variance solution: penalize covariance matrix with shrinkage
  (Ledoit-Wolf optimal shrinkage); raw sample covariance is noisy for crypto

---

## Bayesian Inference

### Regime Detection (HMM)
```
Regimes: TRENDING (autocorrelated returns), RANGING (mean-reverting),
         VOLATILE (fat-tailed, low autocorrelation), CRASH (extreme drawdown)
```
- Hidden Markov Model: latent regime → emission (observed returns/vol)
- Forward algorithm: O(N × K²) for K ≤ 10 regimes — real-time feasible
- Online EM: update params incrementally — required for non-stationary markets
- Viterbi: most likely regime sequence for post-hoc analysis only
- Alternative: Gaussian Mixture + change-point detection (BOCPD — Bayesian Online
  Change Point Detection); exact posterior over run lengths; good for structural breaks

### Regime Detection Features (crypto-specific)
- Realized volatility ratio (short/long window)
- BTC dominance trend direction and velocity
- Funding rate composite (cross-exchange weighted average)
- Stablecoin supply growth rate
- Exchange net flows (whale deposit/withdrawal pattern)

---

## Monte Carlo Simulation

- Minimum 10,000 paths for VaR; 100,000 for CVaR tail estimation
- Antithetic variates + control variates: 10–50× variance reduction
- Random seed recorded for reproducibility; separate seed per simulation run
- Cholesky decomposition for correlated paths; fat tails: Student-t (ν = 3–5)
- GBM + Jump process for crypto: `dS = μS dt + σS dW + J dN`
- Variance of estimator: check with pilot run (1000 paths) before full run;
  high variance → use more antithetic variates

---

## Conformal Prediction (see also ml-models.md)

### Exchangeability
- Conformal validity requires exchangeable samples; financial time series are not
  exchangeable → use **sequential conformal prediction** or **weighted conformal**
  with recency weighting

### Locally Weighted Conformal (time-series safe)
```python
def weighted_conformal_quantile(
    residuals: np.ndarray,
    weights: np.ndarray,  # recency weights; recent = higher weight
    alpha: float = 0.10,
) -> float:
    """Compute weighted quantile of |residuals| for conformal interval."""
    sorted_idx = np.argsort(residuals)
    sorted_r = residuals[sorted_idx]
    sorted_w = weights[sorted_idx]
    cum_w = np.cumsum(sorted_w) / sorted_w.sum()
    idx = np.searchsorted(cum_w, 1 - alpha)
    return sorted_r[idx]
```

### Coverage Guarantee
- Empirical coverage must be validated on held-out test data before deployment
- Coverage monitoring: on rolling 30-day window; if coverage < target → recalibrate
- Interval width monitoring: interval widening signals model uncertainty increase;
  large widening → review drift metrics

---

## Factor Models

### Cross-Sectional Factor Model
```
r_i = α_i + Σ_k β_{ik} × F_k + ε_i
```
- Crypto factors: BTC beta, ETH beta, DeFi factor, L1 factor, stablecoin yield,
  restaking yield factor (new in v3 — EigenLayer flows)

### Crypto-Specific Factors (updated v3)
| Factor | Construction | Interpretation |
|---|---|---|
| BTC Beta | Rolling β vs BTC | Systematic crypto risk |
| Altcoin Premium | Small-cap vs large-cap spread | Risk appetite |
| DeFi Yield | Protocol yield vs risk-free | DeFi sentiment |
| Funding Rate | Cross-exchange composite | Leverage/sentiment |
| On-chain Activity | Active address growth (normalized) | Adoption |
| Stablecoin Dominance | Stablecoin mktcap / total | Risk-off signal |
| Restaking Flow | EigenLayer net TVL change | New (2024) capital allocation signal |
| LRT Premium | LRT price vs ETH peg | Restaking stress indicator |
| ETF Flow | BTC/ETH spot ETF daily net flow | Institutional demand (2024+) |

### ETF Flow Factor (new, 2024)
- BTC spot ETF (BlackRock IBIT, Fidelity FBTC) daily flows: available from Bloomberg,
  Bloomberg's ETF API, SoSoValue
- Large positive flow → institutional buying signal; large redemption → risk-off
- Correlation with BTC returns: significant positive correlation on 1-day lag
- Include as factor in cross-sectional models; treat as slow-moving institutional signal

---

## Distribution Fitting for Crypto Returns

| Regime | Distribution | Fit Method |
|---|---|---|
| Normal market | Skewed-t | MLE |
| High vol / tail risk | Generalized Pareto (GPD) | Peaks Over Threshold (POT) |
| Jump detection | Variance Gamma | Method of moments |
| Volatility modeling | GJR-GARCH (asymmetric) | MLE |
| Extreme events | Extreme Value Theory (EVT) | GEV or GPD |

### EVT (Extreme Value Theory) for Tail Risk
- Peaks Over Threshold (POT): fit GPD to returns exceeding threshold u
- Mean excess function: linear in u for GPD → validate threshold selection
- Use for VaR at 99%+ levels where historical data is sparse
- Shape parameter ξ > 0: heavy tail (Pareto); ξ = 0: exponential; ξ < 0: bounded

---

## Signal Calibration

- Platt scaling: fit logistic regression on validation set outputs → calibrated probabilities
- Isotonic regression: more flexible; requires more data; non-parametric monotone mapping
- Temperature scaling: single-parameter Platt scaling; often sufficient for neural nets
- ECE < 0.05 required; reliability diagram validation before deployment
- Recalibrate on rolling 30-day window; regime shift = recalibrate immediately

---

## Reinforcement Learning for Trading

### Algorithms
| Algorithm | Use Case | Key Risk |
|---|---|---|
| PPO | Position sizing, execution | Reward hacking |
| SAC | Continuous action (size) | Sample efficiency |
| DQN | Discrete (buy/sell/hold) | Overestimation bias |
| Offline RL (IQL, TD3+BC) | Learning from historical fills | Distribution shift |
| Distributional RL (QR-DQN, C51) | Return distribution modeling | Training stability |

### Distributional RL for Risk-Aware Sizing
- Model full return distribution, not just mean; quantile regression DQN (QR-DQN)
- Use conditional VaR from distribution as sizing constraint: if CVaR(5%) > limit → size down
- Better risk awareness than mean-only RL; recommended for sizing agents
- Implicit Risk-Aware RL (DSAC): maximize expected Sharpe under distribution constraints

### Architect Rules
- Never use online RL directly on live capital; train offline, deploy frozen policy
- Sim-to-real gap: backtest environment must model slippage, latency, partial fills
- Kill switch: RL policy must be overridable by deterministic risk gate at all times
- Reward shaping: Sharpe-penalized; penalize drawdown in reward function
- Conservative start: constrained action space; expand with confidence

---

## Confidence Intervals and Significance

- Bootstrap CI preferred over parametric for non-normal returns
- Sharpe ratio CI: Lo (2002) autocorrelation correction mandatory
- Multiple testing: Bonferroni or BH correction for multiple signals
- Minimum sample: 252 daily obs for annual stats; 1000+ for tail estimates
- Out-of-sample validation mandatory; in-sample only = disqualified
- Deflated Sharpe Ratio (Bailey & Lopez de Prado): adjusts for multiple trials;
  use when evaluating many strategies in a search — prevents false discovery
