# Trade-Bot — Claude Desktop Session Entry
<!-- Regenerate: bash .ai/scripts/context-refresh -->

## ⚡ SESSION START — ONE COMMAND, THEN STOP
```bash
python3 .project-intel/scripts/resume.py /home/fujitsu/Projects/Trade-Bot-main
```
That single output is your **complete context**. Do NOT read any other file to orient.
Ask the user what to work on, then read ONLY the 1–3 files the task requires.

## If resume.py fails
```bash
# Stack: Python 3.11 | uv | FastAPI | XGBoost+HMM | ccxt(Binance/OKX) | pytest | ruff+mypy
# Entrypoint: src/api/main.py | Engine: src/engine/orchestrator.py
# Sizing: src/risk/kelly.py | Regime: src/regime/detector.py
uv run pytest tests/ -x -q          # validate health
git log --oneline -3                 # last known state
cat .project-intel/SESSION_STATE.json | python3 -c "import json,sys; s=json.load(sys.stdin); print('NEXT:', s['next_recommended_task'][:160])"
```

## Absolute rules
- **Never** read files to orient — resume.py is the context
- **Never** read: `.env`, `GAPS.md`, `ARCHITECTURE.md`, `MODULE_MAP.json`, `RAW_SCAN.json`, `rag.db`, `requirements.lock`, `node_modules/`, `.venv/`, `data/`, `logs/`, `models/`
- **Never** `cat` any file >100 lines — use `grep`/`sed`/`head`/`tail` + line ranges
- **Always** `uv run` not bare `python3`/`pip`
- **Always** run `uv run ruff check --fix src/ && uv run mypy src/ && uv run pytest tests/ -x -q` after code changes
- **Commit**: `bash scripts/claude-commit.sh --msg "type(scope): desc [claude]"`

---

## On-Chain Intelligence (OCI) — Implementation Log

### OCI-001: Foundation (`src/intelligence/onchain/base.py`)
- `RateLimiter` (token-bucket), `CircuitBreaker` (3-state), `AsyncHTTPCache` (per-key TTL)
- `OnChainProvider` ABC: `fetch_metrics() -> dict[str, float]`, fail-open `_get`/`_post`

### OCI-002: ArkhamProvider (`src/intelligence/onchain/arkham_provider.py`)
- Exchange flows, whale buy/sell ratio, exchange reserve ratio
- Auth: `arkham-api-key` header | Rate: 10 req/min | Cache: 300s

### OCI-003: DefiLlamaProvider (`src/intelligence/onchain/defillama_provider.py`)
- Staking unlock risk, TVL cross-chain
- Auth: none (public API) | Rate: 120 req/min | Cache: 300s

### OCI-004: DuneProvider (`src/intelligence/onchain/dune_provider.py`)
- Miner netflow signal, entity exchange imbalance
- Auth: `X-Dune-API-Key` header | Rate: 40 req/min | Cache: 300s

### OCI-005: CryptoQuantProvider (`src/intelligence/onchain/cryptoquant_provider.py`)
- exchange_reserve_ratio, exchange_netflow_7d_zscore, miner_netflow_signal
- binance_funding_rate_pct (fallback), exchange_stress_score (MVRV contrib)
- Auth: `Authorization: Bearer` | Rate: 10 req/min | Cache: 300s
- Fail-open: all fields neutral, confidence=0.0 when key empty

### OCI-006: CoinglassProvider (`src/intelligence/onchain/coinglass_provider.py`)
- futures_oi_change_pct, liquidation_pressure_24h_zscore, liquidation_cascade_risk_usd
- binance_funding_rate_pct (fallback), whale_buy_sell_ratio
- Auth: `CG-API-KEY` header | Rate: 30 req/min | Cache: 60s
- Fail-open: all fields neutral, confidence=0.0 when key empty

### OCI-007: Schema (`src/intelligence/onchain/schema.py`)
- `ONCHAIN_NEUTRAL`: canonical neutral defaults for all on-chain fields
- `GATED_FIELDS`: fields requiring paid API keys (exchange_netflow, reserve_ratio, miner_netflow, staking_unlock, entity_exchange_imbalance)
- `validate_provider_result()`: sanitize NaN/Inf, clamp confidence, strip internal fields
- `merge_onchain_results()`: confidence-weighted merge + MVRV additive contribution

### OCI-008: Aggregator Integration (`src/intelligence/providers/aggregator.py`)
- `OnChainAwareAggregator(exchange_providers, macro_providers, onchain_providers)`
- Extends `MultiProviderIntelligenceAggregator` with on-chain provider fan-out
- Blends validated on-chain results into exchange/macro merged dict

### OCI-009: Integration Tests (`tests/intelligence/onchain/test_onchain_aggregator_integration.py`)
- Full blending pipeline: exchange + on-chain providers merged
- Tests: no-op without providers, blending, gating, failure isolation, multi-provider

### OCI-010: Gating Tests (`tests/intelligence/onchain/test_onchain_gating.py`)
- Verifies GATED_FIELDS stay neutral when confidence=0
- Verifies all providers fail-open correctly
- Gate policy: aggregator blocks gated fields when oc_conf <= 0.0

### OCI-011: Documentation (this section)

---

### Key Design Decisions

**Fail-open pattern**: Every provider is disabled (not broken) without an API key. All fields return neutral + confidence=0.0. No exceptions propagate to the aggregator.

**Gate policy**: `GATED_FIELDS` (exchange_netflow, reserve_ratio, miner_netflow, staking_unlock, entity_exchange_imbalance) are only written into the final merged dict when the on-chain provider returns `confidence > 0`. This prevents spurious neutral values from overwriting exchange-derived signals.

**MVRV additive**: `exchange_stress_score_mvrv_contrib` (internal field from CryptoQuantProvider) is stripped during schema validation and summed additively into `exchange_stress_score` during merge. Capped at 1.0.

**Confidence-weighted blending**: When multiple providers report a field, the result is `sum(val * conf) / sum(conf)`. If all providers return neutral, the field remains neutral.
