#!/usr/bin/env python3
"""
Cognitive Architecture Layer
==============================
Persistent domain knowledge across ALL sessions and ALL agents.
Covers every domain from your first message:
  - Quant finance (Kelly, AFML, regime, sizing)
  - Probability & statistics (Bayesian, Monte Carlo, VaR)
  - Risk assessment (STRIDE, CVaR, drawdown)
  - Blockchain & crypto algorithms (ECC, ECDSA, consensus)
  - Cryptocurrency (DeFi risk, market microstructure)
  - DevOps & architecture (CAP, 12-factor, SRE)

Stored in: .project-intel/knowledge/
Loaded selectively by context_builder.py based on query topic.
Never loads all knowledge at once — only what's relevant.
"""

import json
import re
from pathlib import Path

KNOWLEDGE_DIR = Path(__file__).parent.parent / "knowledge"

# ── Domain knowledge definitions ─────────────────────────────────────────────
# Each entry: {id, domain, topic, tags, content}
# Tags drive selective loading — only load what matches the query.

KNOWLEDGE_BASE = [

# ════════════════════════════════════════════════════════════════════
# QUANT FINANCE
# ════════════════════════════════════════════════════════════════════
{
"id": "kelly-criterion",
"domain": "quant_finance",
"topic": "Kelly Criterion & Sizing",
"tags": ["kelly","sizing","bet","fraction","position","capital","edge","odds"],
"content": """
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
"""},

{
"id": "triple-barrier-cpcv",
"domain": "quant_finance",
"topic": "Triple Barrier Labeling + CPCV",
"tags": ["triple","barrier","label","cpcv","cross","validation","purged","combinatorial","afml"],
"content": """
## Triple Barrier + CPCV — Architecture Reference

### Triple Barrier (AFML Ch.3)
Labels: +1 (upper hit first), -1 (lower hit first), 0 (time barrier)
Parameters: profit_take=2×ATR, stop_loss=1×ATR, time_barrier=20 bars
Prevents: look-ahead bias via event-driven labeling

### CPCV — Combinatorial Purged Cross-Validation (AFML Ch.7)
Solves: standard k-fold leaks future data into training for financial series
Mechanism:
  1. Split into N groups
  2. Purge: remove samples whose labels overlap with test period
  3. Embargo: remove k bars after each test period
  4. Combinatorial: test all C(N,k) combinations — unbiased Sharpe estimate

### Why this matters for this project
Without CPCV: reported Sharpe inflated by 0.5-2× due to serial correlation
With CPCV: OOS Sharpe is the real number — live gate Sharpe>1.5 is meaningful

### Common mistakes
- Forgetting embargo period: leaks momentum into adjacent bars
- Using accuracy not F1: class imbalance (more 0 labels than ±1) inflates accuracy
- Not checking for concurrent labels: overlapping events need deduplication
"""},

{
"id": "hmm-regime",
"domain": "quant_finance",
"topic": "HMM Regime Detection",
"tags": ["hmm","regime","hidden","markov","gaussian","state","ranging","trending","volatile"],
"content": """
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
"""},

{
"id": "fractional-differentiation",
"domain": "quant_finance",
"topic": "Fractional Differentiation",
"tags": ["fractional","diff","differentiation","stationarity","memory","afml","d","integration"],
"content": """
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
"""},

# ════════════════════════════════════════════════════════════════════
# PROBABILITY & STATISTICS
# ════════════════════════════════════════════════════════════════════
{
"id": "bayesian-risk",
"domain": "probability",
"topic": "Bayesian Risk Scoring",
"tags": ["bayesian","prior","posterior","likelihood","risk","probability","inference","update"],
"content": """
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
"""},

{
"id": "var-cvar",
"domain": "probability",
"topic": "VaR and CVaR Risk Metrics",
"tags": ["var","cvar","value","risk","tail","expected","shortfall","drawdown","loss"],
"content": """
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
"""},

{
"id": "hurst-exponent",
"domain": "probability",
"topic": "Hurst Exponent & Fractal Markets",
"tags": ["hurst","fractal","market","hypothesis","persistence","memory","trending","mean","revert"],
"content": """
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
"""},

# ════════════════════════════════════════════════════════════════════
# RISK ASSESSMENT
# ════════════════════════════════════════════════════════════════════
{
"id": "stride-threat-model",
"domain": "risk",
"topic": "STRIDE Threat Modeling",
"tags": ["stride","threat","security","model","spoofing","tampering","repudiation","disclosure","denial","elevation"],
"content": """
## STRIDE Threat Model — Trade Bot Risk Assessment

### S — Spoofing
Risk: Fake API responses from exchange (MITM)
Mitigation: TLS pinning on ccxt requests; verify exchange SSL cert
Status: OPEN — ccxt handles TLS but no pinning configured

### T — Tampering
Risk: ORDER_SECRET or API_KEY leaked → tampered orders
Mitigation: detect-secrets baseline + bandit SAST (CI enforced)
Risk: SESSION_STATE.json modified externally → wrong trade context
Mitigation: checksum SESSION_STATE on load

### R — Repudiation
Risk: Trade audit log tampered after the fact
Status: TradeAuditor logs to SQLite — not append-only
Mitigation: consider SQLite write-ahead log + periodic hash chain

### I — Information Disclosure
Risk: API keys in .env exposed via /debug endpoints
Mitigation: /debug/* requires X-API-Key (implemented in auth.py)
Gap: /debug/audit exposes full trade decisions — add operator-only gate

### D — Denial of Service
Risk: WebSocket flood → memory exhaustion (512MB alert)
Mitigation: RuntimeMonitor watches memory; restart on 1GB
Gap: No rate limiting on WebSocket connections

### E — Elevation of Privilege
Risk: EXECUTION_MODE=live without operator approval
Mitigation: OPERATOR_SECRET required for mode switch (implemented)
"""},

{
"id": "slippage-market-impact",
"domain": "risk",
"topic": "Slippage & Market Impact Model",
"tags": ["slippage","impact","market","spread","almgren","chriss","execution","cost","fill"],
"content": """
## Slippage & Market Impact — Architecture Reference (GAP-001)

### Almgren-Chriss Model
Expected slippage = spread_cost + market_impact
spread_cost   = 0.5 × bid_ask_spread × order_size
market_impact = η × σ × sqrt(order_size / ADV)

where:
  η   = impact coefficient (≈ 0.1 for liquid markets)
  σ   = daily volatility
  ADV = average daily volume (20-day)

### BTC/USDT on Binance (typical values)
Spread: 0.5-2 bps (tight, liquid)
Market impact: 1-5 bps for orders < 0.1% of ADV
ADV (BTC/USDT): ~$2B/day → orders < $2M negligible impact

### Implementation for this project
class SlippageModel:
    def estimate(self, qty_usd, adv_20d, volatility, spread_bps):
        impact = 0.1 * volatility * sqrt(qty_usd / adv_20d)
        total  = spread_bps/10000 + impact
        return total  # as fraction of trade value

    def veto(self, expected_return, slippage, threshold=0.5):
        # Veto if slippage > threshold × expected_return
        return slippage > threshold * abs(expected_return)

### Wire into gates.py as Gate 0 (before all other gates)
- Fetch ADV from fetcher.py (already has order book data)
- Estimate per-signal before position sizing
- Reduce Kelly fraction by (1 - slippage_fraction)
"""},

# ════════════════════════════════════════════════════════════════════
# BLOCKCHAIN & CRYPTO ALGORITHMS
# ════════════════════════════════════════════════════════════════════
{
"id": "crypto-algorithms",
"domain": "blockchain",
"topic": "Cryptographic Algorithms for Trading Systems",
"tags": ["crypto","ecc","ecdsa","ed25519","hmac","sha256","signature","key","hash","secp256k1"],
"content": """
## Cryptographic Algorithms — Architecture Reference

### Used in this project (implicitly via ccxt/exchange APIs)

#### API Authentication (HMAC-SHA256)
Binance: HMAC-SHA256 of query_string with API_SECRET
OKX: HMAC-SHA256 of timestamp+method+path+body
Security: HMAC is MAC not signature — requires shared secret → never expose API_SECRET

#### WebSocket Authentication
WS key derived from API key + timestamp + HMAC
Replay attack window: 5s (Binance) — ensure clock sync (NTP)

### If extending to on-chain settlement
secp256k1 (Bitcoin/Ethereum): ECDSA signatures
Ed25519 (Solana/Cardano): faster, smaller signatures, no malleability
Key storage: never in .env for on-chain keys → hardware wallet or HSM

### Secure random for API key generation
# Current recommendation in README: openssl rand -hex 32
# Python equivalent (used in auth.py):
import secrets
key = secrets.token_hex(32)  # cryptographically secure

### Hash chain for audit log integrity
Each audit entry: hash = SHA256(previous_hash + entry_data)
Tamper detection: recompute chain and compare to stored hashes
Lightweight: adds ~0.1ms per entry
"""},

{
"id": "defi-risk",
"domain": "blockchain",
"topic": "DeFi & Cryptocurrency Risk Models",
"tags": ["defi","liquidity","amm","impermanent","loss","funding","rate","perpetual","basis","crypto"],
"content": """
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
"""},

# ════════════════════════════════════════════════════════════════════
# DEVOPS & ARCHITECTURE
# ════════════════════════════════════════════════════════════════════
{
"id": "cap-theorem",
"domain": "devops",
"topic": "CAP Theorem & Distributed Systems",
"tags": ["cap","consistency","availability","partition","distributed","database","eventual","strong"],
"content": """
## CAP Theorem — Architecture Reference

### For this project's storage choices
SQLite (current): CP — consistent, partition-tolerant, not available under write lock
TimescaleDB (migration target): CP — PostgreSQL consistency model
QuestDB: AP — available, eventually consistent — acceptable for market data

### Decision matrix for Trade Bot
Trade execution data: requires C (consistency) — wrong position size is worse than downtime
Market data (bars): AP acceptable — stale bar is recoverable
Audit log: CP required — incomplete audit is a compliance risk

### Practical implication: SQLite WAL mode (current)
WAL: readers don't block writers, writers don't block readers
Bottleneck: single writer at a time → contention under 3 concurrent timeframes
Mitigation: async queue for writes (single writer coroutine) — already in storage.py pattern

### Migration trigger (GAP-006)
When: adding 2nd symbol OR live trading with >100 trades/day
To: TimescaleDB (PostgreSQL) — drop-in replace for storage.py with asyncpg
"""},

{
"id": "sre-observability",
"domain": "devops",
"topic": "SRE & Observability Patterns",
"tags": ["sre","observability","metrics","prometheus","grafana","alert","monitor","health","golden","signal"],
"content": """
## SRE & Observability — Architecture Reference

### Google's 4 Golden Signals (apply to Trade Bot)
1. Latency:   signal_engine cycle time (target: < 500ms per 15m bar)
2. Traffic:   trades per hour, signals per hour
3. Errors:    gate rejections, API errors, HMM fit failures
4. Saturation: memory usage, SQLite write queue depth

### Current implementation (src/diagnostics/)
RuntimeMonitor: covers saturation (memory) + errors (dead tasks, tick stall)
TradeAuditor: covers traffic (trade decisions logged)
SignalDebugger: covers errors (model drift)
Missing: latency tracking, Prometheus export

### Recommended metrics to add (TASK-007)
# In orchestrator.py signal loop:
signal_latency_ms = (time.time() - bar_receive_time) * 1000
regime_state_gauge = current_regime  # 0/1/2
kelly_fraction_gauge = computed_kelly
gate_pass_rate = gates_passed / gates_evaluated (rolling 100)

### Alert thresholds
signal_latency > 2000ms: WARN (missing bar window)
memory > 512MB: WARN (RuntimeMonitor already handles)
gate_pass_rate < 0.05: WARN (model degraded or over-filtered)
gate_pass_rate > 0.8: WARN (gates too loose — risk of overtrading)
"""},

]


# ── Writer ────────────────────────────────────────────────────────────────────
def build_knowledge_base(output_dir: Path):
    output_dir.mkdir(parents=True, exist_ok=True)

    # Write individual knowledge files
    index = []
    for entry in KNOWLEDGE_BASE:
        fname = f"{entry['id']}.md"
        fpath = output_dir / fname
        content = (
            f"# {entry['topic']}\n"
            f"**Domain**: {entry['domain']} | "
            f"**Tags**: {', '.join(entry['tags'])}\n\n"
            f"{entry['content'].strip()}\n"
        )
        fpath.write_text(content)
        index.append({
            "id":     entry["id"],
            "domain": entry["domain"],
            "topic":  entry["topic"],
            "tags":   entry["tags"],
            "file":   fname,
            "tokens": len(content) // 4,
        })

    # Write index
    (output_dir / "INDEX.json").write_text(json.dumps(index, indent=2))

    total_tokens = sum(e["tokens"] for e in index)
    print(f"✓ Knowledge base: {len(index)} entries | ~{total_tokens} tokens total")
    print(f"  Location: {output_dir}")
    return index


# ── Selective loader ──────────────────────────────────────────────────────────
def load_relevant(query: str, knowledge_dir: Path, max_tokens: int = 1500) -> str:
    """Load only knowledge entries relevant to the query. Token-budget aware."""
    index_file = knowledge_dir / "INDEX.json"
    if not index_file.exists():
        return ""

    index = json.loads(index_file.read_text())
    query_words = set(re.findall(r'\b\w+\b', query.lower()))

    # Score each entry by tag overlap
    scored = []
    for entry in index:
        overlap = len(query_words & set(entry["tags"]))
        if overlap > 0:
            scored.append((overlap, entry))

    scored.sort(key=lambda x: x[0], reverse=True)

    # Load top entries within token budget
    loaded = []
    used_tokens = 0
    for _, entry in scored:
        if used_tokens + entry["tokens"] > max_tokens:
            break
        fpath = knowledge_dir / entry["file"]
        if fpath.exists():
            loaded.append(fpath.read_text())
            used_tokens += entry["tokens"]

    return "\n\n---\n\n".join(loaded) if loaded else ""


def main():
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--build",  help="Build knowledge base to this dir")
    p.add_argument("--query",  help="Query for relevant knowledge")
    p.add_argument("--dir",    help="Knowledge dir to query")
    args = p.parse_args()

    if args.build:
        build_knowledge_base(Path(args.build))
        return
    if args.query and args.dir:
        result = load_relevant(args.query, Path(args.dir))
        print(result or "No relevant knowledge found.")
        return
    p.print_help()

if __name__ == "__main__":
    main()
