# Trade-Bot — Claude Desktop Project Memory
<!-- Auto-maintained. Regenerate: bash .ai/scripts/context-refresh -->
<!-- Last updated: 2026-07-09 -->

## ⚡ SESSION PROTOCOL — READ THIS FIRST
**Do NOT read source files to orient yourself. Everything you need is in this file.**
Ask the user "what are we working on?" and use the module map below to read ONLY the 1–3 files directly relevant to the task.

## Stack
| Layer | Tech |
|-------|------|
| Runtime | Python 3.11 (pyproject), uv package manager |
| API | FastAPI (src/api/main.py) |
| Engine | Orchestrator → SignalEngine → Execution FSM |
| Data | ccxt (Binance/OKX), SQLite via StorageBackend |
| ML | XGBoost + HMM regime detector, ensemble predictor |
| Risk | Kelly sizing, cognitive engine, performance drift |
| Intelligence | OnChain providers (Arkham, DeFiLlama, Dune), aggregator |
| Tests | pytest, uv run pytest tests/ -x -q |
| Lint/Type | ruff + mypy strict |

## Module Map (read only what the task touches)
```
src/config.py                      — all Settings classes (BinanceSettings, RiskSettings, …)
src/api/main.py                    — FastAPI app, lifespan, endpoints
src/api/auth.py                    — API key verification
src/api/metrics.py                 — Prometheus metrics
src/data/fetcher.py                — MarketDataFetcher, OrderBookSnapshot
src/data/storage.py                — StorageBackend, BarRecord, TradeRecord, …
src/engine/orchestrator.py         — main trading loop
src/engine/signal_engine.py        — signal generation
src/execution/order_fsm.py         — order state machine
src/execution/live.py              — live executor
src/execution/paper.py             — paper executor
src/execution/order_manager.py     — order lifecycle
src/execution/live_fsm_integration.py — FSM ↔ live bridge
src/regime/detector.py             — HMM regime detection
src/models/trainer.py              — XGBoost model training
src/features/pipeline.py           — feature engineering
src/features/intelligence_features.py — intelligence feature extraction
src/risk/kelly.py                  — Kelly criterion sizing
src/risk/gates.py                  — risk gate checks
src/risk/cognitive_engine.py       — cognitive risk layer
src/risk/drift_integration.py      — performance drift hooks
src/risk/performance_drift.py      — drift detection
src/risk/portfolio_correlation.py  — correlation risk
src/risk/slippage.py               — slippage model
src/intelligence/client.py         — intelligence client
src/intelligence/ensemble_predictor.py — ensemble
src/intelligence/probabilistic.py  — probabilistic layer
src/intelligence/probabilistic_adapter.py — adapter
src/intelligence/metrics.py        — intelligence metrics
src/intelligence/causal_inference.py — causal layer
src/intelligence/providers/aggregator.py  — provider aggregation
src/intelligence/providers/binance_provider.py
src/intelligence/providers/okx_provider.py
src/intelligence/providers/coingecko_provider.py
src/intelligence/providers/blockchain_provider.py
src/intelligence/onchain/base.py   — OnChainProvider ABC, RateLimiter, CircuitBreaker
src/intelligence/onchain/arkham_provider.py  — Arkham netflow/whale
src/intelligence/onchain/defillama_provider.py — TVL/staking
src/intelligence/onchain/dune_provider.py     — Dune analytics
src/diagnostics/runtime_monitor.py
src/diagnostics/signal_debugger.py
src/diagnostics/trade_auditor.py
```

## Key Commands
```bash
uv sync                            # install/sync deps
uv run pytest tests/ -x -q        # run tests (always after changes)
uv run pytest tests/path/test.py  # single test file
uv run ruff check --fix src/       # lint + fix
uv run mypy src/                   # type check
uv run python -m src.api.main      # run API server
bash .ai/scripts/context-refresh   # regenerate this file's module map
```

## Engineering Rules
- **uv run** — never bare `python3` or `pip install`
- **pyproject.toml** — single source of truth for deps; never touch requirements.txt directly
- **No mocks/placeholders** — production-grade only
- **Conventional commits**: `feat|fix|refactor|test|chore|docs|perf(<scope>): <subject>`
- **Secrets**: env vars only; reference `.env.example` for keys, never read `.env`
- **After every code change**: ruff + mypy + pytest

## Tests Map
```
tests/test_orchestrator.py              → src/engine/orchestrator.py
tests/test_order_fsm.py                 → src/execution/order_fsm.py
tests/test_live_executor_fsm.py         → src/execution/live.py
tests/test_detector.py                  → src/regime/detector.py
tests/test_kelly.py                     → src/risk/kelly.py
tests/test_features.py                  → src/features/pipeline.py
tests/test_cognitive_engine.py          → src/risk/cognitive_engine.py
tests/intelligence/onchain/             → src/intelligence/onchain/
tests/test_intelligence_providers.py    → src/intelligence/providers/
```

## Forbidden (without explicit user approval)
- Reading `.env` (never needed — check `.env.example`)
- Deleting/renaming src/ files
- Committing to git remote
- Reading any file in `.project-intel/`, `data/`, `logs/`, `models/`, `.venv/`
- Reading ALL files in a package — read only the file the task requires

## Recent Activity (last 5 commits)
```
c6881b7 feat(onchain): OCI-003 — DeFiLlamaProvider (staking_unlock_risk, tvl_change, stablecoin_ratio)
e76bcb9 feat(onchain): OCI-002 — ArkhamProvider (netflow zscore, reserve ratio, imbalance, whale ratio)
b4134b5 feat(onchain): OCI-001 — OnChainProvider ABC, RateLimiter, CircuitBreaker
44b0974 chore(context): remove SRC MAP from resume — 76% token reduction
133ddf0 chore(context): maximize token savings — compressed session files
```
