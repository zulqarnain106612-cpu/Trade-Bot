# Hurst Exponent & Fractal Markets
**Domain**: probability | **Tags**: hurst, fractal, market, hypothesis, persistence, memory, trending, mean, revert

## Hurst Exponent — Architecture Reference (Peters 1994)

### Interpretation
H < 0.5: mean-reverting (anti-persistent)
H = 0.5: random walk (efficient market)
H > 0.5: trending (persistent) ← this project filters on H > 0.55

### Computation (R/S analysis)
1. Divide series into n sub-periods
2. For each: compute range R and std dev S
3. E[R/S] = C × n^H  →  H = log(R/S) / log(n)

### Filter logic in this project (src/strategies/filters.py)
H > 0.55: trending regime — allow momentum positions
H < 0.45: mean-reverting — flip signal direction or skip
0.45 < H < 0.55: near random walk — reduce position size by 0.5×

### Crypto context
BTC Hurst typically 0.55-0.65 in bull markets (persistent)
BTC Hurst 0.45-0.50 in ranging markets (near random)
High-frequency (1m): H → 0.5 (microstructure noise dominates)
15m and 4h: more persistent, better Hurst signal
