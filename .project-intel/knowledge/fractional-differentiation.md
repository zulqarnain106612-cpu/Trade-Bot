# Fractional Differentiation
**Domain**: quant_finance | **Tags**: fractional, diff, differentiation, stationarity, memory, afml, d, integration

## Fractional Differentiation — Architecture Reference

### Problem it solves
Integer differencing (d=1): achieves stationarity but destroys memory
No differencing: non-stationary, model learns spurious correlations
Frac-diff (d=0.4): stationarity + memory preservation

### Mathematical basis
X_t^d = Σ_{k=0}^{∞} w_k × X_{t-k}
w_k = -w_{k-1} × (d-k+1) / k  (binomial series weights)

### d=0.4 in this project
Chosen by AFML recommendation: smallest d where ADF test passes
Typical range: 0.3-0.5 for financial returns series
Crypto is more non-stationary than equities — d=0.4 may need tuning to 0.5

### Implementation gotchas
Weights decay slowly — need 50+ lags for d=0.4 (use threshold 1e-4)
Boundary correction: first ~50 bars have incomplete weight sums — drop from training
Memory cost: O(T×L) where L=lag_threshold — manageable at 50 lags
