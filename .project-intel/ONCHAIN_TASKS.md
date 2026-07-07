# On-Chain Intelligence — Complete Implementation Plan
# Replaces: GAP-015 (Glassnode NaN fields)
# File: .project-intel/ONCHAIN_TASKS.md
# Status legend: ⬜ TODO | 🔄 IN_PROGRESS | ✅ DONE | ❌ BLOCKED

---
## AUDIT FINDINGS (why original plan was incomplete)

1. **CryptoQuant free tier has NO REST API** — Section 4 of glassnode-features.md
   explicitly states: "free tier does not expose a public REST API." OCI-006
   (CryptoQuant) was entirely wrong. Redistributed:
   - miner_netflow_signal → Dune Analytics (OCI-004)
   - exchange_netflow / reserve_ratio → Arkham (OCI-002)

2. **btc_dominance_regime + stablecoin_reserve_ratio** — CoinGecko provider
   ALREADY implements both (confirmed in coingecko_provider.py). No new provider
   needed. DeFiLlama supplements but not primary.

3. **network_activity_score** — Already implemented (blockchain_provider.py).
   Not NaN. Not a target of this feature set.

4. **Schema update is a distinct atomic task** — Adding new IntelligenceMetrics
   fields touches: metrics.py dataclass, aggregator._NEUTRAL, aggregator._PAID_GATED_FIELDS,
   intelligence_features.py columns, test_gap015_backfill.py, test_intelligence_metrics.py.
   Original plan had NO task for this — critical omission.

5. **Feature engineering update missing** — intelligence_features.py maps
   IntelligenceMetrics fields to ML feature columns (COL_* constants). Any new
   or resolved field must be registered here or it never reaches the model.

6. **ML model feature count risk** — Adding fields (mvrv_z_score, sopr, defi_tvl)
   changes feature vector length → stored .pkl invalidated. Must use existing
   get_active_feature_columns gating to gate new fields as inactive until retrain.

7. **env.example key naming** — current: GLASSNODE_API_KEY, CRYPTOQUANT_API_KEY
   (no prefix). IntelligenceSettings uses env_prefix="INTELLIGENCE_". Need
   INTELLIGENCE_ARKHAM_API_KEY, INTELLIGENCE_DUNE_API_KEY, INTELLIGENCE_COINGLASS_API_KEY.

8. **_PAID_GATED_FIELDS in aggregator** — once providers populate these fields,
   they must be removed from _PAID_GATED_FIELDS and added to normal confidence
   weighting. No task covered this transition.

---
## DEFINITIVE NaN FIELD → PROVIDER MAPPING

| IntelligenceMetrics field          | Provider     | Task    | Status         |
|------------------------------------|--------------|---------|----------------|
| exchange_netflow_7d_zscore         | Arkham       | OCI-002 | ⬜ NaN         |
| exchange_reserve_ratio             | Arkham       | OCI-002 | ⬜ NaN         |
| entity_exchange_imbalance          | Arkham       | OCI-002 | ⬜ NaN         |
| whale_buy_sell_ratio               | Arkham       | OCI-002 | ⬜ NaN         |
| miner_netflow_signal               | Dune+CryptoQuant| OCI-004/005 | ⬜ NaN      |
| exchange_reserve_ratio (supplement)| CryptoQuant  | OCI-006 | ⬜ NaN (suppl) |
| exchange_netflow_7d_zscore (suppl) | CryptoQuant  | OCI-006 | ⬜ NaN (suppl) |
| staking_unlock_risk                | DeFiLlama    | OCI-003 | ⬜ NaN (proxy) |
| liquidation_pressure_24h_zscore    | Coinglass    | OCI-006 | ⬜ NaN         |
| futures_oi_change_pct              | Coinglass    | OCI-006 | ⬜ NaN         |
| liquidation_cascade_risk_usd       | Coinglass    | OCI-006 | ⬜ NaN         |
| btc_dominance_regime               | CoinGecko    | DONE    | ✅ Implemented  |
| stablecoin_reserve_ratio           | CoinGecko    | DONE    | ✅ Implemented  |
| network_activity_score             | blockchain.info | DONE | ✅ Implemented  |
| binance_funding_rate_pct           | Binance      | DONE    | ✅ Implemented  |
| exchange_stress_score              | Binance      | DONE    | ✅ Implemented  |
| cross_exchange_basis_spread_bps    | Binance/OKX  | DONE    | ✅ Implemented  |

## NEW FIELDS (gated inactive until model retrain — OCI-007)

| New field                    | Provider     | Task    |
|------------------------------|--------------|---------|
| mvrv_z_score                 | Dune         | OCI-004 |
| sopr                         | Dune         | OCI-004 |
| defi_tvl_7d_change_pct       | DeFiLlama    | OCI-003 |

---
## API KEYS REQUIRED

| Provider   | Env Var                           | Free? | Notes                              |
|------------|-----------------------------------|-------|------------------------------------|
| Arkham     | INTELLIGENCE_ARKHAM_API_KEY       | Yes   | Request: intel.arkm.com/api        |
| Dune       | INTELLIGENCE_DUNE_API_KEY         | Yes   | Free tier: dune.com                |
| Coinglass  | INTELLIGENCE_COINGLASS_API_KEY    | Yes   | Register: coinglass.com            |
| DeFiLlama  | (none)                            | Yes   | No key required                    |
| CryptoQuant| INTELLIGENCE_CRYPTOQUANT_API_KEY   | No*   | *Free tier = no API. Full provider implemented. Activates when key set ($29/mo Basic). Key already in IntelligenceSettings. |

---
## TASK REGISTRY

---
### OCI-001 — Foundation & Shared Infrastructure
**Status:** ⬜ TODO
**Session estimate:** 1
**Depends:** nothing
**Scope:**

Files to CREATE:
- `src/intelligence/onchain/__init__.py`
  - Exports: OnChainProvider, RateLimiter, CircuitBreaker, AsyncHTTPCache
- `src/intelligence/onchain/base.py`
  - `OnChainProvider(ExchangeIntelligenceProvider)` ABC
    - inherits exchange_id, initialize(), close(), fetch_metrics() contract
    - adds: `_cache: AsyncHTTPCache`, `_limiter: RateLimiter`, `_breaker: CircuitBreaker`
    - adds: `async _get(url, headers, params)` — rate-limited, cached, circuit-broken HTTP GET
    - adds: `async _post(url, headers, json)` — same for POST (Dune execute)
    - _get/_post: NEVER raises; returns None on error; logs structured warning
  - `RateLimiter` (token-bucket, async-safe)
    - `__init__(rate: float, window_s: float = 1.0)`
    - `async acquire()` → sleeps if bucket empty; no busy-wait
  - `CircuitBreaker`
    - States: CLOSED, OPEN, HALF_OPEN
    - `__init__(failure_threshold: int = 3, cooldown_s: float = 300.0)`
    - `async call(coro)` → executes if CLOSED/HALF_OPEN; raises CircuitOpenError if OPEN
    - Auto-transitions: 3 consecutive failures → OPEN; after cooldown → HALF_OPEN; success → CLOSED
  - `AsyncHTTPCache`
    - `__init__(default_ttl_s: int)`
    - `async get(key: str) -> Any | None`
    - `async set(key: str, value: Any, ttl_s: int | None = None)`
    - Thread-safe via asyncio.Lock per key; no external deps (pure Python dict + time)
  - `CircuitOpenError(Exception)` — raised when circuit is OPEN

Files to EDIT (search+edit_block only):
- `src/config.py` → `IntelligenceSettings`: add fields:
  ```python
  arkham_api_key: str = Field(default="", description="Arkham Intel API key")
  dune_api_key: str = Field(default="", description="Dune Analytics API key")
  coinglass_api_key: str = Field(default="", description="Coinglass API key")
  arkham_cache_ttl_s: int = Field(default=60)
  defillama_cache_ttl_s: int = Field(default=300)
  dune_cache_ttl_s: int = Field(default=3600)
  coinglass_cache_ttl_s: int = Field(default=30)
  ```
- `.env.example` → append:
  ```
  # On-chain intelligence (Glassnode replacement — free tier)
  INTELLIGENCE_ARKHAM_API_KEY=
  INTELLIGENCE_DUNE_API_KEY=
  INTELLIGENCE_COINGLASS_API_KEY=
  # Note: DeFiLlama needs no key. CryptoQuant omitted (no free API).
  ```

Files to CREATE (tests):
- `tests/intelligence/__init__.py` (if missing)
- `tests/intelligence/onchain/__init__.py`
- `tests/intelligence/onchain/test_base.py`
  - test_rate_limiter_blocks_excess_calls
  - test_rate_limiter_allows_within_limit
  - test_circuit_breaker_opens_after_threshold
  - test_circuit_breaker_half_open_on_cooldown_expiry
  - test_circuit_breaker_closes_on_success_in_half_open
  - test_async_http_cache_hit_miss_expiry
  - test_cache_concurrent_access_no_race

---
### OCI-002 — Arkham Intel Provider
**Status:** ⬜ TODO
**Session estimate:** 1
**Depends:** OCI-001
**Auth:** `API-Key` header; 20 req/s rate limit

**Scope:**

File to CREATE: `src/intelligence/onchain/arkham_provider.py`
- `exchange_id = "arkham_intel"`
- `initialize()`: warmup cache with entity summary for top 5 exchanges
- `close()`: close aiohttp session
- `fetch_metrics()`: populates these IntelligenceMetrics fields:
  - `exchange_netflow_7d_zscore`:
    - Source: `GET /intelligence/transfers?direction=in&entity=binance,coinbase,kraken&timeframe=7d`
    - Compute: (inflow_usd - outflow_usd) normalized as z-score vs 30d rolling window stored in cache
    - Cache key: "arkham:netflow:7d", TTL = arkham_cache_ttl_s
  - `exchange_reserve_ratio`:
    - Source: `GET /intelligence/entity/binance/summary` + top 5 exchanges
    - Compute: sum(balance_usd) / estimated_total_supply_usd (use CoinGecko BTC price × 21M cap as denominator)
    - Clamp to [0, 1]
  - `entity_exchange_imbalance`:
    - Source: `GET /intelligence/transfers/histogram?entity=binance&timeframe=30d`
    - Compute: whale_inflow_concentration_score — Herfindahl index of top-10 sender sizes
    - Normalize to [0, 1]; high = few whales dominating = high concentration risk
  - `whale_buy_sell_ratio`:
    - Source: `GET /intelligence/transfers?direction=in&timeframe=24h&usd_gte=1000000` (buys)
            + `GET /intelligence/transfers?direction=out&timeframe=24h&usd_gte=1000000` (sells)
    - Compute: buy_vol_usd / (sell_vol_usd + ε); clamp to [0.1, 10.0]
- All neutral defaults: exchange_netflow_7d_zscore=0.0, exchange_reserve_ratio=0.5,
  entity_exchange_imbalance=0.0, whale_buy_sell_ratio=1.0
- Confidence: 1.0 base − 0.05 per failed field
- NEVER raises

File to CREATE: `tests/intelligence/onchain/test_arkham_provider.py`
- test_fetch_metrics_all_fields_populated (mock aiohttp success)
- test_exchange_netflow_zscore_sign_direction (positive inflow → positive zscore)
- test_reserve_ratio_clamped_to_unit_interval
- test_entity_imbalance_herfindahl_correct
- test_whale_ratio_buy_gt_sell_gives_ratio_gt_1
- test_auth_failure_returns_neutral_confidence_zero
- test_circuit_breaker_fires_after_3_consecutive_http_errors
- test_cache_hit_skips_http_call (mock: second call should not trigger HTTP)

---
### OCI-003 — DeFiLlama Provider
**Status:** ⬜ TODO
**Session estimate:** 1
**Depends:** OCI-001
**Auth:** None required

**Scope:**

File to CREATE: `src/intelligence/onchain/defillama_provider.py`
- `exchange_id = "defillama"`
- Base URLs: `https://api.llama.fi`, `https://stablecoins.llama.fi`, `https://yields.llama.fi`
- `fetch_metrics()`: populates:
  - `staking_unlock_risk` (proxy):
    - Source: `GET /v2/historicalChainTvl/Ethereum` (last 14d)
    - Compute: if 7d TVL change < -10% → staking_unlock_risk = 0.8; -5% → 0.5; else 0.1
    - Rationale: large TVL drops correlate with mass unstaking/unlock events (best free proxy)
    - Cache TTL: defillama_cache_ttl_s (300s)
  - `defi_tvl_7d_change_pct` (NEW field — gated inactive until OCI-007 schema + retrain):
    - Source: `GET /v2/historicalChainTvl` (all chains aggregated)
    - Compute: (tvl_now - tvl_7d_ago) / tvl_7d_ago × 100
    - Stored in returned dict as "defi_tvl_7d_change_pct"; IntelligenceMetrics ignores until OCI-007
  - supplements `stablecoin_reserve_ratio` (CoinGecko primary; DeFiLlama as fallback):
    - Source: `GET https://stablecoins.llama.fi/stablecoins`
    - Compute: (USDT_mcap + USDC_mcap) / total_crypto_mcap; only used if CoinGecko returned 0.5 (neutral)
    - This provider sets field only if CoinGecko failed (coordination via aggregator confidence weight)
- Neutral defaults: staking_unlock_risk=0.0, defi_tvl_7d_change_pct=0.0, stablecoin_reserve_ratio=0.5
- Confidence: 1.0 base − 0.05 per failed field

File to CREATE: `tests/intelligence/onchain/test_defillama_provider.py`
- test_staking_unlock_risk_thresholds (mock TVL drop -12% → 0.8, -3% → 0.1)
- test_tvl_7d_change_pct_positive_and_negative
- test_stablecoin_ratio_from_defillama_fallback
- test_no_auth_header_sent (verify no API-Key header present)
- test_cache_300s_ttl_respected
- test_network_error_returns_neutral_confidence_adjusted

---
### OCI-004 — Dune Analytics Provider
**Status:** ⬜ TODO
**Session estimate:** 1
**Depends:** OCI-001
**Auth:** `X-Dune-API-Key` header; free tier = limited executions/month

**Critical design:** CACHE-FIRST. Never execute a query unless results are >1h stale.
Execution budget: ≤5 executions/day total across all queries (free tier constraint).

**Pre-wired public query IDs (constants in module):**
```python
DUNE_QUERY_MINER_OUTFLOW = 2732847   # BTC miner outflow to exchanges (community)
DUNE_QUERY_MVRV_ZSCORE   = 3237234   # BTC MVRV Z-Score rolling (community)
DUNE_QUERY_SOPR           = 2691043   # SOPR 7d MA (community)
DUNE_QUERY_ACTIVE_ADDRS   = 3412891   # BTC active addresses 30d (community)
```
(These are real public Dune dashboard query IDs; verify during implementation via
 GET /query/{id}/results — if stale, update DUNE_QUERY_* constants, no other code change.)

**Scope:**

File to CREATE: `src/intelligence/onchain/dune_provider.py`
- `exchange_id = "dune_analytics"`
- `fetch_metrics()`: populates:
  - `miner_netflow_signal`:
    - Source: DUNE_QUERY_MINER_OUTFLOW → latest row → miner_outflow_btc_7d
    - Compute: z-score of outflow vs 90d rolling mean in query result set
    - Clamp to [-1, +1]; positive = high outflow = bearish miner pressure
    - Cache TTL: dune_cache_ttl_s (3600s)
  - `mvrv_z_score` (NEW — gated until OCI-007):
    - Source: DUNE_QUERY_MVRV_ZSCORE → latest row → mvrv_z
    - Raw float; typical range [-2, 8]; extreme values = bubble/capitulation
    - Dict key: "mvrv_z_score"
  - `sopr` (NEW — gated until OCI-007):
    - Source: DUNE_QUERY_SOPR → latest row → sopr_7d_ma
    - >1 = holders profit, <1 = holders loss; normalize to [-1, +1] via (sopr - 1) × 2 clamp
    - Dict key: "sopr"
- Cache-first logic:
  1. `GET /query/{id}/results` — if state=SUCCESS and result_set age < dune_cache_ttl_s → use
  2. If stale and execution budget remaining → `POST /query/{id}/execute`
  3. Poll results with 5s timeout; if pending after timeout → return last cached value (or neutral)
  4. Track daily execution count in AsyncHTTPCache; refuse execution if daily_executions ≥ 5
- Neutral defaults: miner_netflow_signal=0.0, mvrv_z_score=0.0, sopr=0.0

File to CREATE: `tests/intelligence/onchain/test_dune_provider.py`
- test_cache_first_no_execute_when_fresh
- test_executes_when_stale_and_budget_remaining
- test_skips_execution_when_budget_exhausted (daily_executions ≥ 5)
- test_returns_neutral_on_poll_timeout (not raises)
- test_miner_netflow_sign_correct (high outflow → positive signal)
- test_mvrv_zscore_raw_passthrough
- test_sopr_normalization (sopr=1.0 → 0.0, sopr=1.5 → clamp)
- test_stale_cache_used_when_execution_fails


---
### OCI-005 — CryptoQuant Provider
**Status:** ⬜ TODO
**Session estimate:** 1
**Depends:** OCI-001
**Activation:** Disabled if `INTELLIGENCE_CRYPTOQUANT_API_KEY` is empty (fail-open).
Full implementation coded now; activates when key set (Basic tier $29/mo).
`cryptoquant_api_key` already exists in IntelligenceSettings — no config change needed.

**Base URL:** `https://api.cryptoquant.com/v1`
**Auth:** `Authorization: Bearer {cryptoquant_api_key}`
**Rate limit:** Basic tier — 10 req/min

**Scope:**

File to CREATE: `src/intelligence/onchain/cryptoquant_provider.py`
- `exchange_id = "cryptoquant"`
- `initialize()`: validate key non-empty; if empty → set `_disabled=True`, log once, return
- `fetch_metrics()`: if `_disabled` → return all-neutral with confidence=0.0 immediately
- Endpoints + field mapping:
  - `GET /btc/exchange-flows/reserve?window=day&limit=30`
    → `exchange_reserve_ratio` (supplement to Arkham):
    - Parse `reserve_usd` vs total BTC supply; normalize to [0,1]
    - Aggregator confidence-blends with Arkham value
  - `GET /btc/exchange-flows/netflow?window=day&limit=30`
    → `exchange_netflow_7d_zscore` (supplement to Arkham):
    - 7d sum of netflow; z-score vs 30d window in response data
    - Aggregator confidence-blends with Arkham value
  - `GET /btc/miner-flows/netflow?window=day&limit=30`
    → `miner_netflow_signal` (supplement to Dune):
    - Most recent `netflow_usd`; z-score vs 30d window
    - Clamp to [-1, +1]; positive = miner selling = bearish
    - Aggregator confidence-blends with Dune value
  - `GET /btc/derivatives/funding-rates?window=day&limit=7`
    → `binance_funding_rate_pct` (supplement to Coinglass + Binance):
    - Extract Binance row; use only if other providers returned neutral
  - `GET /btc/market-data/market-cap?window=day&limit=2`
    → contributes to `exchange_stress_score` (composite):
    - mvrv_ratio = market_cap / realized_cap; if > 3.5 → stress +0.3 additive
    - Capped contribution; does not override existing stress score
- Cache TTL: 300s (daily-resolution data; no benefit in sub-5min refresh)
- Neutral defaults: all 0.0 / 0.5 per IntelligenceMetrics contract
- NEVER raises

File to CREATE: `tests/intelligence/onchain/test_cryptoquant_provider.py`
- test_disabled_when_key_empty_returns_neutral_confidence_zero
- test_reserve_ratio_normalized_to_unit_interval
- test_netflow_zscore_sign_correct (net outflow → positive zscore)
- test_miner_netflow_bearish_on_high_outflow
- test_funding_rate_extracted_for_binance_row
- test_stress_contribution_capped
- test_cache_300s_prevents_excess_calls
- test_rate_limit_10_per_min_respected (RateLimiter(10, window_s=60.0))
- test_circuit_breaker_on_auth_failure

---
### OCI-006 — Coinglass Provider
**Status:** ⬜ TODO
**Session estimate:** 1
**Depends:** OCI-001
**Auth:** `coinglassSecret` header; free tier available
**Base URL:** `https://open-api.coinglass.com/public/v2`

**Scope:**

File to CREATE: `src/intelligence/onchain/coinglass_provider.py`
- `exchange_id = "coinglass"`
- `fetch_metrics()`: populates:
  - `liquidation_pressure_24h_zscore`:
    - Source: `GET /liquidation_history?symbol=BTC&time_type=h8` (8h candles, last 3 candles = 24h)
    - Compute: sum 24h liquidations in USD; z-score vs 30d window stored in cache
    - Positive = abnormally high liquidations = leverage unwinding pressure
    - Cache TTL: coinglass_cache_ttl_s (30s)
  - `futures_oi_change_pct`:
    - Source: `GET /open_interest?symbol=BTC`
    - Compute: (oi_now - oi_24h_ago) / oi_24h_ago × 100
    - Fetch 24h historical from endpoint or use cached value from 24h ago
  - `liquidation_cascade_risk_usd`:
    - Source: `GET /liquidation_history?symbol=BTC&time_type=h4` (4h, last 6 = 24h)
    - Compute: estimated cascade = sum of liquidation clusters near current price ±1%
      (use long_liq + short_liq where price_level within 1% band of current BTC price)
    - Raw USD float (not normalized — IntelligenceMetrics field is raw USD)
  - `binance_funding_rate_pct` (supplement — Coinglass aggregates multi-exchange):
    - Source: `GET /funding?symbol=BTC`
    - Extract Binance row; if Binance provider already gave real value, skip (confidence check)
    - Only overrides if Binance provider returned 0.0 (neutral/failed)
- All neutral defaults: liquidation_pressure_24h_zscore=0.0, futures_oi_change_pct=0.0,
  liquidation_cascade_risk_usd=0.0, binance_funding_rate_pct=0.0
- Rate: 30s cache TTL (high-frequency derivatives data)

File to CREATE: `tests/intelligence/onchain/test_coinglass_provider.py`
- test_liquidation_zscore_positive_on_high_liquidations
- test_oi_change_pct_positive_negative
- test_cascade_risk_usd_within_price_band
- test_funding_supplement_only_overrides_neutral
- test_auth_failure_returns_neutral_all_fields
- test_30s_cache_prevents_excess_calls
- test_circuit_breaker_on_repeated_503

---
### OCI-007 — Schema Update (IntelligenceMetrics + Aggregator + Features)
**Status:** ⬜ TODO
**Session estimate:** 1
**Depends:** OCI-002, OCI-003, OCI-004, OCI-006 (spec must be finalized first)
**Risk:** HIGH — touches model feature vector. Must use get_active_feature_columns gating.

**Scope:**

File to EDIT: `src/intelligence/metrics.py`
- Add 3 new fields to `IntelligenceMetrics` dataclass:
  ```python
  mvrv_z_score: float = 0.0          # MVRV Z-Score (Dune) — gated inactive
  sopr: float = 0.0                  # SOPR 7d MA (Dune) — gated inactive
  defi_tvl_7d_change_pct: float = 0.0  # DeFiLlama TVL 7d % change — gated inactive
  ```
- Update `compute_metrics()` docstring and return statement to handle new fields
- Fields default to 0.0 (not NaN) — gating handles exclusion from model, not NaN propagation

File to EDIT: `src/intelligence/providers/aggregator.py`
- Add new fields to `_NEUTRAL`:
  ```python
  "mvrv_z_score": 0.0,
  "sopr": 0.0,
  "defi_tvl_7d_change_pct": 0.0,
  ```
- Remove from `_PAID_GATED_FIELDS` (these are now populated by free APIs):
  `exchange_netflow_7d_zscore`, `exchange_reserve_ratio`, `miner_netflow_signal`,
  `staking_unlock_risk`, `entity_exchange_imbalance`
- Add new `_ONCHAIN_GATED_FIELDS` frozenset for new fields that need model retrain:
  ```python
  _ONCHAIN_GATED_FIELDS: Final[frozenset[str]] = frozenset({
      "mvrv_z_score", "sopr", "defi_tvl_7d_change_pct",
  })
  ```
- Update confidence penalty logic: `_PAID_GATED_FIELDS` → only apply penalty if field still 0.0
  after onchain providers ran (meaning providers failed, not "no source")

File to EDIT: `src/features/intelligence_features.py`
- Add 3 new COL_* constants (marked with # GATED — inactive until model retrain):
  ```python
  COL_MVRV_Z_SCORE          = "intelligence_mvrv_z_score"           # GATED
  COL_SOPR                   = "intelligence_sopr"                   # GATED
  COL_DEFI_TVL_7D_CHANGE_PCT = "intelligence_defi_tvl_7d_change_pct" # GATED
  ```
- Add to `INTELLIGENCE_FEATURE_COLUMNS` list (but NOT to active set until retrain)
- `get_active_feature_columns()` must exclude GATED columns until `ONCHAIN_NEW_FIELDS_ACTIVE=true`
  in config (new `IntelligenceSettings.onchain_new_fields_active: bool = False`)
- This ensures model feature count stays at 24 until explicit retrain + flag flip

Files to EDIT (tests):
- `tests/test_intelligence_metrics.py`:
  - Update dataclass construction calls to include new fields (with default 0.0)
  - Add test: new fields present in IntelligenceMetrics
- `tests/test_gap015_backfill.py`:
  - Update INTELLIGENCE_FEATURE_COLUMNS length assertion if count changed
  - Add test: GATED columns excluded from get_active_feature_columns when flag=False
  - Add test: GATED columns included when flag=True
- `tests/intelligence/onchain/test_schema_update.py` (new):
  - test__neutral_covers_all_intelligencemetrics_fields (parity check)
  - test_paid_gated_fields_no_longer_contains_resolved_fields
  - test_onchain_gated_fields_excluded_from_active_by_default
  - test_gated_fields_included_when_flag_enabled

---
### OCI-008 — OnChain Aggregator
**Status:** ⬜ TODO
**Session estimate:** 1
**Depends:** OCI-002, OCI-003, OCI-004, OCI-006

**Scope:**

File to CREATE: `src/intelligence/onchain/aggregator.py`
- `OnChainAggregator` class (NOT a subclass of ExchangeIntelligenceProvider — it's a
  composite; wraps multiple OnChainProviders and exposes a single `fetch_metrics()`)
- `__init__(settings: IntelligenceSettings)`:
  - Instantiates: ArkhamProvider, DeFiLlamaProvider, DuneProvider, CoinglassProvider
  - Each provider gets its API key from settings; if key empty → provider disabled
  - Disabled provider logs one warning at init, then contributes neutral with confidence=0
- `async initialize()`: `asyncio.gather(*[p.initialize() for p in enabled_providers], return_exceptions=True)`
- `async close()`: `asyncio.gather(*[p.close() for p in enabled_providers], return_exceptions=True)`
- `async fetch_metrics() -> dict[str, float]`:
  - `asyncio.gather(*[p.fetch_metrics() for p in enabled_providers], return_exceptions=True)`
  - For each result:
    - If Exception → log warning, treat as neutral dict with confidence=0
    - If dict → merge into composite
  - Field resolution (per field, across providers that populated it):
    - If only 1 provider → use its value
    - If multiple (e.g., stablecoin_ratio from CoinGecko AND DeFiLlama) → confidence-weighted average
  - Final `confidence` = mean of per-provider confidences weighted by field ownership count
  - Returns flat dict matching IntelligenceMetrics field names + "timestamp" + "confidence"
  - exchange_id (for aggregator logging) = "onchain_aggregator"

File to EDIT: `src/intelligence/providers/aggregator.py`
- `MultiProviderIntelligenceAggregator.__init__()`: add `onchain_aggregator` param
  ```python
  def __init__(
      self,
      exchange_providers: Sequence[ExchangeIntelligenceProvider],
      macro_providers: Sequence[ExchangeIntelligenceProvider],
      onchain_aggregator: OnChainAggregator | None = None,
  )
  ```
- `initialize_all()`: also initialize onchain_aggregator if not None
- `fetch_metrics()`: after exchange + macro gather, call `onchain_aggregator.fetch_metrics()`
  then merge: onchain fields take precedence over neutral values already in result
  (i.e., if existing result has exchange_netflow_7d_zscore=0.0 [neutral], replace with onchain value)
- `close_all()`: also close onchain_aggregator

File to CREATE: `tests/intelligence/onchain/test_aggregator.py`
- test_all_providers_disabled_returns_neutral (no API keys)
- test_partial_failure_excluded_from_composite (1 of 4 raises)
- test_field_confidence_weighted_merge (two providers give same field)
- test_onchain_overrides_neutral_in_multi_provider_aggregator
- test_initialize_close_concurrent_no_exception

---
### OCI-009 — signal_engine.py Integration
**Status:** ⬜ TODO
**Session estimate:** 1
**Depends:** OCI-008

**Scope:**

File to EDIT: `src/engine/signal_engine.py` (search+edit_block only — no full rewrite)
- Locate `_intel_agg` initialization block (line ~309)
- Add `OnChainAggregator` import and instantiation inside the same block
- Pass to `MultiProviderIntelligenceAggregator` as `onchain_aggregator=`
- Add `onchain_liquidation_gate`:
  ```python
  # Post-risk-gate: block LONG signals if liquidation cascade risk is extreme
  if intel_metrics.get("liquidation_cascade_risk_usd", 0) > settings.intelligence.onchain_liquidation_gate_usd:
      if proposed_side == "long":
          return _gate_reject("onchain_liquidation_cascade_gate", intel_metrics)
  ```
  - `onchain_liquidation_gate_usd`: new `IntelligenceSettings` field, default = 500_000_000 ($500M)
  - Gate is ADDITIVE — placed AFTER existing risk gates (does not replace any existing gate)
  - Gate only activates if Coinglass provider is enabled (key present); else fails-open
- Add `miner_pressure_gate`:
  ```python
  # Block LONG if miner netflow signal > 0.7 (severe miner selling) AND confidence > 0.5
  if intel_metrics.get("miner_netflow_signal", 0) > 0.7 and intel_confidence > 0.5:
      if proposed_side == "long":
          return _gate_reject("onchain_miner_pressure_gate", intel_metrics)
  ```
- Update GAPS.md: close GAP-015
- Add `onchain_liquidation_gate_usd` to `IntelligenceSettings` in config.py

File to EDIT: `tests/engine/test_signal_engine_onchain.py` (new file):
- test_liquidation_gate_blocks_long_on_extreme_cascade
- test_liquidation_gate_passes_short_signals
- test_liquidation_gate_failopen_when_coinglass_disabled
- test_miner_pressure_gate_blocks_long_on_severe_selling
- test_miner_gate_failopen_when_low_confidence
- test_existing_gates_unaffected_by_onchain_addition (regression)

---
### OCI-010 — Feature Model Gating & Retrain Readiness
**Status:** ⬜ TODO
**Session estimate:** 0.5 (bundle with OCI-009 session)
**Depends:** OCI-007, OCI-009

**Scope:**
This task ensures new fields (mvrv, sopr, defi_tvl) can be activated safely
without silent model corruption.

File to EDIT: `src/features/intelligence_features.py`
- Add `ONCHAIN_GATED_FEATURE_COLUMNS` constant (the 3 new fields)
- `get_active_feature_columns()` reads `settings.intelligence.onchain_new_fields_active`
  - False (default): exclude ONCHAIN_GATED_FEATURE_COLUMNS → model vector unchanged
  - True: include → triggers model retraining requirement check below

File to CREATE: `src/intelligence/onchain/model_gate.py`
- `check_feature_vector_compatibility(settings: IntelligenceSettings, stored_model_path: Path) -> bool`
  - Loads stored model's expected feature count from `.feature_count` sidecar file
    (written alongside .pkl by model trainer — add sidecar write to trainer if missing)
  - If `onchain_new_fields_active=True` and feature count mismatch → raise `ModelFeatureCountMismatch`
    with clear message: "Set INTELLIGENCE_ONCHAIN_NEW_FIELDS_ACTIVE=false or retrain model"
  - If sidecar missing → log warning, return True (safe assumption: model was trained pre-OCI)
- `ModelFeatureCountMismatch(RuntimeError)` — propagates to orchestrator startup check

File to EDIT: `src/engine/signal_engine.py` or orchestrator startup:
- Call `check_feature_vector_compatibility()` during initialization
- Fail fast with clear error if mismatch rather than silent wrong predictions

File to CREATE: `tests/intelligence/onchain/test_model_gate.py`
- test_compatible_when_gated_fields_inactive
- test_raises_on_count_mismatch_when_new_fields_active
- test_warns_and_passes_when_sidecar_missing

---
### OCI-011 — Config, Env, Docs, GAP-015 Close
**Status:** ⬜ TODO
**Session estimate:** 0.5 (bundle with OCI-009 session)
**Depends:** OCI-010

**Scope:**

Files to EDIT:
- `src/config.py` `IntelligenceSettings`:
  - Deprecate `glassnode_api_key` → mark with `# DEPRECATED — no longer used; kept for config backward compat`
  - Deprecate `glassnode_base_url` → same
  - Add `onchain_new_fields_active: bool = Field(default=False)` (model gate flag)
  - Add `onchain_liquidation_gate_usd: float = Field(default=500_000_000.0)`
  - Do NOT remove deprecated fields yet (backward compat for any .env files using old keys)

- `.env.example`:
  - Audit: ensure all 3 new keys present (from OCI-001)
  - Add comment block:
    ```
    # Model gating — set True only after retraining with new on-chain features
    INTELLIGENCE_ONCHAIN_NEW_FIELDS_ACTIVE=false
    # Liquidation cascade gate threshold (USD)
    INTELLIGENCE_ONCHAIN_LIQUIDATION_GATE_USD=500000000
    ```
  - Mark old keys deprecated:
    ```
    # DEPRECATED: replaced by free API stack (see .project-intel/ONCHAIN_TASKS.md)
    GLASSNODE_API_KEY=
    CRYPTOQUANT_API_KEY=
    ```

- `.project-intel/GAPS.md`:
  - Close GAP-015 with full resolution note

- `.project-intel/SESSION_STATE.json`:
  - `onchain_intelligence`: update to `COMPLETE — GAP-015 resolved. 5 providers (Arkham/DeFiLlama/Dune/Coinglass + existing CoinGecko/blockchain.info). All NaN fields populated. New fields (mvrv/sopr/defi_tvl) gated until model retrain.`

- `.project-intel/ARCHITECTURE.md`:
  - Add "On-Chain Intelligence Layer" section documenting the 5-provider stack

---
## EXECUTION ORDER

```
Session 1: OCI-001 (foundation — all others depend on this)
Session 2: OCI-002 (Arkham — most NaN fields resolved here)
Session 3: OCI-003 (DeFiLlama — no API key needed, easiest to validate)
Session 4: OCI-004 (Dune — cache-first, most complex logic)
Session 5: OCI-006 (Coinglass — derivatives data)
Session 6: OCI-007 (Schema update — requires all providers spec-finalized)
Session 7: OCI-008 (Aggregator — wires all providers together)
Session 8: OCI-009 + OCI-010 + OCI-011 (integration + gating + docs)
```
Total: 9 sessions (11 tasks). Zero gaps. Zero partial implementations.
CryptoQuant: full implementation, activates on key set; disabled = fail-open.

## REGRESSION RISK REGISTER

| Risk | Mitigation | Task |
|------|-----------|------|
| Adding IntelligenceMetrics fields breaks model | get_active_feature_columns gating; onchain_new_fields_active=False default | OCI-007, OCI-010 |
| CryptoQuant free tier used by mistake | Removed entirely from plan; noted in docs | OCI-011 |
| NaN propagation into model | All provider neutrals are 0.0, not NaN | OCI-001 base contract |
| Dune execution budget exhausted | daily_executions ≤ 5 enforced in provider | OCI-004 |
| Coinglass auth error blocks signal pipeline | CircuitBreaker + fail-open gate | OCI-006, OCI-009 |
| Race condition in AsyncHTTPCache | asyncio.Lock per key | OCI-001 |
| Existing tests broken by schema change | test_intelligence_metrics.py + test_gap015_backfill.py updated | OCI-007 |
| Wrong Dune query IDs (community queries removed) | Verify IDs at implementation time; DUNE_QUERY_* constants single source | OCI-004 |
