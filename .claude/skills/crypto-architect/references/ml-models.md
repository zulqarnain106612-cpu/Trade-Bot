# ML Model Governance Reference

## Model Registry Schema

Every model in production requires a registry entry:
```json
{
  "model_id": "momentum-lstm-v3",
  "version": "3.2.1",
  "architecture": "LSTM",
  "training_data_hash": "sha256:abc...",
  "feature_schema_version": "2025-Q1",
  "trained_at": "2025-04-01T00:00:00Z",
  "training_window": {"start": "2022-01-01", "end": "2025-02-28"},
  "oos_sharpe": 1.87,
  "oos_period": {"start": "2025-01-01", "end": "2025-02-28"},
  "conformal_coverage": 0.91,
  "conformal_target": 0.90,
  "drift_thresholds": {"psi_warn": 0.2, "psi_halt": 0.25, "kl_warn": 0.1},
  "kill_switch": true,
  "approver": "architect@firm.com",
  "capital_limit_usd": 100000,
  "input_validation": "strict",
  "adversarial_tested": true
}
```

---

## Transformer Architectures for Time Series

### Temporal Fusion Transformer (TFT)
- Architecture: multi-head attention + gating + variable selection; handles mixed
  static/temporal inputs and multi-horizon forecasting natively
- Key advantage: variable selection network outputs feature importance per time step —
  audit-ready explainability without post-hoc SHAP
- Use: swing and day-trade horizon (1h–7d); portfolio-level forecasting
- Hyperparameters: hidden size 64–256; attention heads 4–8; dropout 0.1 on live data

### PatchTST (2023)
- Architecture: tokenize time series into patches → Vision Transformer (ViT) style attention
- Key advantage: captures long-range dependencies; self-supervised pre-training on
  many assets → fine-tune per strategy (transfer learning)
- Patch length: 16–64 time steps; affects receptive field
- Pre-training: Masked Patch Modeling (like BERT for time series)
- Use: intraday with sufficient history (> 2000 bars per asset)

### iTransformer (2024)
- Inverts the transformer: attention across variates (channels) not time steps
- Advantage: captures cross-asset correlations explicitly; better for multi-variate
  forecasting than PatchTST in portfolio contexts
- Use: cross-asset correlation-aware signal generation

### TimesNet / TimeMixer (2024)
- Multi-period decomposition: splits signal into trend, seasonal components; processes
  each via 2D convolutions or mixing
- Strong baselines for financial time series benchmarks; evaluate vs TFT before committing

### Architect Rules for Transformer Models
- Sequence length: must respect causality strictly; no shuffling
- Attention masking: causal mask mandatory in autoregressive mode
- Positional encoding: learnable (not fixed sinusoidal) generally better for financial
  series with irregular seasonality
- Pre-trained checkpoints: validate training data overlap with live data period before
  using any public pre-trained model; data leakage risk is high for crypto (continuous markets)

---

## Tabular Foundation Models

### TabPFN v2 (2024)
- Prior-fitted network: trained on millions of synthetic tabular datasets; in-context
  learning for new tasks without fine-tuning
- Use: small dataset regimes (< 10K rows); rapid prototyping of factor signals
- Not for production on large datasets; test against LightGBM; often loses for > 10K rows
- Latency: inference slow (seconds); not suitable for real-time signal generation

### XGBoost / LightGBM (still gold standard for tabular)
- LightGBM: faster than XGBoost on large data; GOSS (gradient-based one-side sampling)
- SHAP: native TreeExplainer is fast (O(n × features × depth)); use for audit trail
- Feature importance: SHAP preferred over built-in gain importance (less biased)
- Hyperparameter tuning: Optuna with pruning; 50–200 trials sufficient

---

## LLM-Generated Signals (High Risk Category)

### Use Cases (evaluated risk)
- Sentiment analysis: news headline → sentiment score; well-validated, moderate risk
- Earnings call NLP: structured information extraction from transcripts; moderate risk
- On-chain event interpretation: translate contract events to plain language; moderate risk
- Direct price prediction from LLM: HIGH risk — hallucination, overconfidence, no causality

### Mandatory Safeguards for LLM Signals
- **Adversarial input validation**: before feeding market data or news to LLM,
  validate that inputs are not injection attempts (adversarial prompts embedded in
  financial news, price data with unusual formatting designed to alter model behavior)
- **Output schema validation**: LLM output must match strict JSON schema; free-form
  text outputs rejected
- **Confidence gate**: LLM sentiment signals must have calibrated confidence;
  raw LLM output is not calibrated — apply Platt scaling or isotonic regression
  on out-of-sample data
- **Drift monitoring**: the same PSI/KL framework applies; monitor input distribution
  (news embedding drift) and output distribution (sentiment score drift)
- **Kill switch**: same as any other model; LLM-generated signal must be disableable
  independently of other signals
- **Hallucination surface**: LLM may fabricate ticker symbols, prices, events —
  validate every factual claim in LLM output against authoritative data source
  before using in decision logic
- **Prompt injection detection**: if LLM is given market data, sanitize input for
  adversarial prompt patterns (check for instruction-override attempts embedded in
  news text); log any detected attempts as SECURITY_EVENT

---

## Conformal Prediction (Mandatory for Sizing Models)

### Coverage Guarantee
Conformal prediction provides distribution-free, guaranteed coverage:
```
P(y ∈ C(x)) ≥ 1 - α
```
where `C(x)` is the prediction interval and `α` is the error rate (e.g., 0.1 for 90% coverage).

### Implementation (Split Conformal)
```python
from sklearn.model_selection import train_test_split
import numpy as np

# Step 1: Fit model on training data; compute residuals on calibration set
y_pred_cal = model.predict(X_cal)
residuals = np.abs(y_cal - y_pred_cal)

# Step 2: Compute conformal quantile
alpha = 0.10  # 90% coverage
n = len(residuals)
q_level = np.ceil((n + 1) * (1 - alpha)) / n
q_hat = np.quantile(residuals, q_level)

# Step 3: Form prediction interval on new data
y_pred = model.predict(X_test)
lower = y_pred - q_hat
upper = y_pred + q_hat
# Empirical coverage on held-out data must be ≥ 1 - alpha
```

### Registry Requirements
- `conformal_coverage`: actual coverage on held-out data
- `conformal_target`: 1 - alpha (e.g., 0.90)
- Coverage must be ≥ target; if not, model does not meet sizing requirement
- Recalibrate conformal quantile on rolling 30-day window; market regime shift → recalibrate

---

## Feature Engineering Standards

### Causality Rules
- All features must be causal — no look-ahead; use `pandas.shift(1)` minimum
- NaN handling: explicit fill policy per feature; never silently forward-fill price
  features without staleness cap
- Outlier clipping: winsorize at (1st, 99th) percentile using training-set bounds;
  never refit bounds on inference data
- Normalization: z-score using training mean/std; freeze scaler — never refit on live data
- Feature correlation audit: |ρ| > 0.95 pair → drop less interpretable one

### Feature Store Architecture
```
Raw Market Data → Feature Pipeline → Feature Store → Model Serving
                                          ↑
                             Point-in-time correctness enforced
```
- Point-in-time correctness: feature values reconstructable for any past timestamp
- Online/offline consistency: offline training features must be bit-identical to online
  inference features — validated via shadow mode comparison

---

## Drift Detection

### PSI (Population Stability Index)
```
PSI = Σ (A_i - E_i) × ln(A_i / E_i)
```
- PSI < 0.1: stable; 0.1–0.2: monitor; > 0.2: warn; > 0.25: halt and retrain

### Concept Drift
- ADWIN (Adaptive Windowing): detects change in mean of performance metric
- Page-Hinkley: sequential drift detection; lower false alarm rate than CUSUM
- Rolling Sharpe: 30-day OOS Sharpe < 0.5 → auto-suspend

---

## Champion / Challenger Deployment

```
Challenger → Shadow Mode (log predictions, no orders; min 2000 samples or 30d)
          → Statistical test (p < 0.05 improvement)
          → Canary (5% capital, 14 days; Sharpe improvement ≥ 0.2)
          → Champion (full capital allocation)
```
- Blue-green: old model kept warm 24h post-promotion
- Rollback trigger: Champion metric regression > 1 std from 90-day baseline

---

## Training Pipeline Requirements

### Data Quality Gates
- Missing data < 0.1% per feature
- Survivorship bias check: include delisted assets
- Regime coverage: bear + bull cycle; COVID crash, LUNA, FTX, 2024 ETF flows
- Minimum bars: 100,000 HFT; 2,000 daily for swing
- Test set: last 20% of time; never touched until final eval

### Stress Tests (Mandatory)
Replay through: COVID crash (Mar 2020), LUNA depeg (May 2022), FTX collapse (Nov 2022),
3AC liquidation (Jun 2022), Bybit hack market impact (Feb 2025), Bitcoin ETF approval
volatility (Jan 2024), ETH ETF launch (Jul 2024).

---

## Online Learning Safety Rules

- Gradient clipping: ‖∇‖ > threshold → skip update (not clip) — prevents poisoning
- Rollback buffer: keep last N model states; revert if rolling Sharpe degrades
- Poison detection: update improves training loss but degrades validation → reject
- LLM in online learning: never update LLM weights online in production;
  only prompt engineering and retrieval allowed for live systems
