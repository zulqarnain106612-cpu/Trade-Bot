# Crypto Intelligence Integration — Implementation Guide

**Status**: Code skeleton complete, API integrations pending  
**Phase**: P2 (non-blocking for P1 live trading)  
**Timeline**: 3-4 weeks (phased approach recommended)

---

## FILES CREATED

### Core Intelligence Module
```
src/intelligence/
  __init__.py              ✅ Module export
  client.py                ✅ Multi-provider aggregator (abstraction layer)
  metrics.py               ✅ Intelligence metric computation
  providers/
    __init__.py            ✅ Provider registry
    glassnode.py           ❌ TODO: HTTP client + API wrapper
    cryptoquant.py         ❌ TODO: HTTP client + API wrapper
    sentiment.py           ❌ TODO: Optional, future phase
```

### Feature Engineering Extensions
```
src/features/
  intelligence_features.py ✅ 15 new intelligence-aware features
  pipeline.py              ⚠️  NEEDS UPDATE: Import + wire intelligence
```

### Risk Gates
```
src/risk/
  intelligence_gates.py    ✅ Gate 7 (exchange stress) + Gate 8 (whale)
                              Enhanced Gate 6 (drift + regime)
```

---

## IMPLEMENTATION ROADMAP (Phased)

### Phase 2.1: Infrastructure (Week 1)
**Goal**: Functional API clients + caching layer

**Tasks**:
1. Implement `src/intelligence/providers/glassnode.py`
   - [ ] HTTP client using `aiohttp` or `httpx`
   - [ ] Endpoints:
     - `GET /v1/metrics/addresses/active_count` → netflow
     - `GET /v1/transactions/large` → whale activity
     - `GET /v1/entities/...` → entity classification
   - [ ] Rate limit: 20 req/min (standard plan)
   - [ ] Error handling + exponential backoff
   - [ ] Tests: Mock Glassnode responses

2. Implement `src/intelligence/providers/cryptoquant.py`
   - [ ] HTTP client
   - [ ] Endpoints:
     - `GET /api/v1/busd/exchange_flows/binance` → netflow
     - `GET /api/v1/futures/funding_rate` → funding rate
     - `GET /api/v1/futures/liquidation` → cascade risk
   - [ ] Rate limit: 10 req/sec (plan-based)
   - [ ] Tests: Mock CryptoQuant responses

3. Enhance caching (`src/intelligence/client.py`)
   - [ ] Upgrade from dict cache to Redis (optional, or SQLite fallback)
   - [ ] TTL management per metric type
   - [ ] Cache invalidation on API error (use stale data)
   - [ ] Tests: Cache hit/miss logic

**Effort**: ~40 hours  
**Deliverable**: `IntelligenceAggregator` fully functional, unit tests passing

---

### Phase 2.2: Feature Integration (Week 2)
**Goal**: Intelligence metrics flowing into feature pipeline

**Tasks**:
1. Update `src/features/pipeline.py`
   - [ ] Add import: `from src.intelligence.metrics import IntelligenceAnalyzer`
   - [ ] Call intelligence layer after fetching price data
   - [ ] Pass metrics to `add_intelligence_features()`
   - [ ] Update feature matrix to 24 columns (9 + 15)
   - [ ] Tests: Verify feature shape, no NaN values

2. Integrate historical data backfill
   - [ ] Pull Glassnode historical data (available via API)
   - [ ] Store in `data/intelligence_history.db` (or extend trade_bot.db)
   - [ ] Use for z-score normalization baseline
   - [ ] Tests: Verify z-scores are reasonable (-3 to +3)

3. Update model training
   - [ ] Retrain XGBoost with 24 features (vs 9)
   - [ ] Measure feature importance (intelligence features should rank high)
   - [ ] A/B test: old model (9 features) vs new (24 features)
   - [ ] Verify Sharpe improvement (target: +0.5 to +1.0)

**Effort**: ~30 hours  
**Deliverable**: Feature pipeline produces 24-column matrix, model retrains successfully

---

### Phase 2.3: Risk Gates (Week 3)
**Goal**: Gate 7 and 8 actively fire, enhance Gate 6

**Tasks**:
1. Integrate Gate 7 (Exchange Stress)
   - [ ] Wire into `src/engine/orchestrator.py` gate stack
   - [ ] Add metrics to `/risk-gates` API endpoint
   - [ ] Test: Verify HALT when stress_score > 0.75
   - [ ] Logging: Every gate evaluation (for monitoring)

2. Integrate Gate 8 (Whale Activity)
   - [ ] Wire into orchestrator
   - [ ] Add position sizing adjustment logic
   - [ ] Test: Verify REDUCE when whale_ratio < 1.0
   - [ ] Edge case: Handle missing whale data gracefully

3. Enhance Gate 6 (Drift + Regime)
   - [ ] Update `src/risk/performance_drift.py`
   - [ ] Add regime shift detection
   - [ ] Adjust thresholds dynamically
   - [ ] Test: Verify thresholds relax during regime change

4. Integration tests
   - [ ] Full pipeline: data → intelligence → features → gates → order
   - [ ] Test with real Glassnode/CryptoQuant sandbox data
   - [ ] Verify gate evaluations are deterministic

**Effort**: ~25 hours  
**Deliverable**: All 3 gates firing correctly in paper trading

---

### Phase 2.4: Validation & Monitoring (Week 4)
**Goal**: Production-ready, live trading compatible

**Tasks**:
1. Paper trading baseline (24-48h)
   - [ ] Run orchestrator with intelligence enabled
   - [ ] Capture 50-100 paper trades
   - [ ] Monitor gate firing frequency
   - [ ] Verify no false positives (excessive HALT/REDUCE)

2. Intelligence metrics monitoring dashboard
   - [ ] Add `/intelligence/metrics` API endpoint
   - [ ] Export: netflow_zscore, whale_ratio, stress_score, etc.
   - [ ] Alert thresholds: log WARNING if metrics stale (API down)
   - [ ] Tests: Dashboard rendering, no missing values

3. Performance analysis
   - [ ] Compare paper trading Sharpe: with intelligence vs without
   - [ ] Measure: drawdown reduction, win rate improvement
   - [ ] Root cause: which gates prevent biggest losses?
   - [ ] Decision: proceed to live or further tuning?

4. Documentation + runbook
   - [ ] Update OPERATOR_RUNBOOK.md with intelligence metrics
   - [ ] Add troubleshooting: what if Glassnode is down?
   - [ ] Emergency: fallback to gate-disabled mode

**Effort**: ~20 hours  
**Deliverable**: Live trading ready, operator manual complete

---

## PHASED APPROACH (RECOMMENDED)

**Why phased?**
- Risk: Glassnode/CryptoQuant could have API issues, want to validate before commit
- Cost: Start with Glassnode ($600/mo), add CryptoQuant ($200/mo) after validation
- Learning: Build confidence with each phase, adjust strategy if needed

**Phase 2.1a (Week 1)**: Glassnode only (5 features)
- Exchange netflow, whale ratio, reserve ratio, miner flow, macro regime
- Cost: $600/mo
- Impact: ~60% of full benefit

**Phase 2.1b (Week 2)**: CryptoQuant (4 features)
- Funding rate, liquidation pressure, OI change, cascade risk
- Cost: +$200/mo
- Impact: +30% (total ~90%)

**Phase 2.2 (Week 3)**: Sentiment (5 features, optional)
- LunarCrush or alternative
- Cost: +$100/mo
- Impact: +10% (total ~95%, but complexity increase)

---

## CONFIGURATION (.env updates)

Add to `.env`:

```bash
# Glassnode on-chain intelligence
GLASSNODE_API_KEY=<your-api-key>
GLASSNODE_PLAN=basic|pro|enterprise  # Determines rate limit

# CryptoQuant exchange flows + leverage
CRYPTOQUANT_API_KEY=<your-api-key>
CRYPTOQUANT_PLAN=starter|pro  # Determines endpoint access

# Intelligence feature settings
INTELLIGENCE_CACHE_TTL_ONCHAIN_SECONDS=3600    # 1h for slow data
INTELLIGENCE_CACHE_TTL_EXCHANGE_SECONDS=300    # 5min for fast data
INTELLIGENCE_ENABLED=true                       # Feature flag
INTELLIGENCE_FALLBACK_MODE=degraded             # What to do if API down?
                                                # Options: degraded, halt, skip
```

---

## TESTING STRATEGY

### Unit Tests
```python
# tests/test_intelligence_client.py
def test_cache_ttl_expiry():
    """Verify cache entries expire correctly."""

def test_rate_limiting():
    """Verify requests are rate-limited per provider."""

# tests/test_intelligence_metrics.py
def test_zscore_computation():
    """Verify z-scores computed correctly."""

def test_stress_score_composite():
    """Verify stress score aggregates correctly."""

# tests/test_intelligence_gates.py
def test_exchange_stress_gate_halt():
    """Gate 7 halts when stress > 0.75."""

def test_whale_activity_gate_reduce():
    """Gate 8 reduces when whale_ratio < 1.0."""
```

### Integration Tests
```python
# tests/test_intelligence_pipeline.py
def test_end_to_end_with_mock_data():
    """Full pipeline: intelligence → features → gates."""

def test_with_real_glassnode_sandbox():
    """Hit Glassnode testnet (if available)."""
```

### Performance Tests
```python
# Ensure intelligence doesn't slow down orchestrator
def test_intelligence_latency():
    """Intelligence retrieval must complete < 100ms (cached)."""
```

---

## API ENDPOINTS (additions to src/api/main.py)

```python
@router.get("/intelligence/metrics")
async def get_intelligence_metrics() -> dict:
    """Current intelligence metrics snapshot."""
    return {
        "exchange_netflow_7d_zscore": ...,
        "whale_buy_sell_ratio": ...,
        "binance_funding_rate_pct": ...,
        "exchange_stress_score": ...,
        "timestamp": ...,
        "data_freshness_sec": ...,
    }

@router.get("/intelligence/historical")
async def get_intelligence_historical(
    metric: str,  # e.g., "exchange_stress_score"
    days: int = 7,
) -> dict:
    """Historical intelligence metric (for charting)."""

@router.get("/intelligence/health")
async def get_intelligence_health() -> dict:
    """Provider health status."""
    return {
        "glassnode_status": "healthy|degraded|down",
        "glassnode_last_call": ...,
        "cryptoquant_status": "...",
        "cache_hit_rate": 0.95,  # % of requests served from cache
    }
```

---

## SUCCESS CRITERIA

**Phase 2.1**: ✅
- [ ] Glassnode client fetches data without errors
- [ ] CryptoQuant client fetches data without errors
- [ ] Cache is working (hit rate > 90%)
- [ ] 10 unit tests passing

**Phase 2.2**: ✅
- [ ] Feature pipeline produces 24 columns
- [ ] No NaN values in intelligence features
- [ ] Model retrains with new features
- [ ] Feature importance: top 5 features include at least 2 intelligence features

**Phase 2.3**: ✅
- [ ] Gate 7 fires when expected (stress > 0.75)
- [ ] Gate 8 fires when expected (whale_ratio < 1.0)
- [ ] Gate 6 adjusted for regime shifts
- [ ] Full integration test passing

**Phase 2.4**: ✅
- [ ] Paper trading 24-48h without crashes
- [ ] Sharpe improvement: +0.5 to +1.0 (target: 5.19 → 6.2)
- [ ] Drawdown reduction: 20-30% lower max DD
- [ ] Operator manual complete

---

## ROLLBACK PLAN

If intelligence integration causes degradation:

1. **Feature flag**: `INTELLIGENCE_ENABLED=false` in .env
   - System falls back to 9-feature model
   - 5 minutes to disable, no code changes

2. **Gate fallback**: Set `INTELLIGENCE_FALLBACK_MODE=skip`
   - Skip Gate 7/8 evaluations
   - Keep existing 5 gates active

3. **API failure**: If Glassnode/CryptoQuant down
   - Use stale cached data (up to 1h old)
   - Or use default neutral metrics (0.0 for all scores)
   - Log WARNING every 15min if API unavailable

---

## NEXT DECISION POINT

**Ready to start Phase 2.1?**

Decisions needed:
1. Which provider to start with?
   - ✅ **Glassnode** (recommended): Best on-chain intelligence
   - **CryptoQuant**: Binance-specific, more exchange data
   - Both (ideal but higher cost)

2. Budget constraint?
   - **Full** ($1000/mo): Start with both providers
   - **Conservative** ($600/mo): Glassnode only, add CryptoQuant later

3. Timeline?
   - **Fast** (3 weeks): Full integration
   - **Measured** (5-6 weeks): Phased, validate each phase

**Recommendation**: Start with Glassnode (Week 1), validate, add CryptoQuant (Week 2), test gates (Week 3), validate on paper (Week 4).

