# HMM Regime Detection
**Domain**: quant_finance | **Tags**: hmm, regime, hidden, markov, gaussian, state, ranging, trending, volatile

## GaussianHMM Regime Detection — Architecture Reference

### Model: 3-state GaussianHMM (Hamilton 1989)
States: ranging (μ≈0, σ_low), trending (μ≠0, σ_med), volatile (μ≈0, σ_high)
Features: returns, realized_vol, volume_z_score (minimum viable feature set)
Fitting: Baum-Welch EM algorithm — batch, not online

### Key limitations (GAP-002)
predict() returns argmax state — loses posterior probability information
Entropy of posterior = -Σ p_i × log(p_i):
  Low entropy (< 0.3 nats): high confidence → full position scalar
  High entropy (> 0.8 nats): ambiguous → 0.5× position scalar

### Transition matrix interpretation
P(volatile→trending) should be low — sudden volatility rarely resolves to trend
If transition matrix shows P(v→t) > 0.3: model is overfit or insufficient data

### Refitting strategy
Time-based: refit weekly (stable markets)
Event-based: refit when 20-bar rolling vol crosses 2× median (regime break signal)
Never refit intraday — HMM needs 500+ samples minimum

### Entropy gate implementation (TASK for GAP-002)
probs = hmm.predict_proba(features)  # shape (T, 3)
entropy = -np.sum(probs * np.log(probs + 1e-9), axis=1)
scalar = np.where(entropy > 0.8, 0.5, 1.0)
