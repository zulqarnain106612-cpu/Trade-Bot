# Trade Bot — Amazon Q Workspace Rules

## Project
Python 3.11+ async algorithmic trading bot. Binance (primary) + OKX (secondary).

## Architecture
- `src/config.py` — single source of truth (pydantic-settings). All settings via `get_settings()`.
- `src/engine/orchestrator.py` — async event loop, bootstraps & coordinates all subsystems.
- `src/api/main.py` — FastAPI app, lifespan manages orchestrator lifecycle.
- `src/execution/paper.py` / `live.py` — paper and live executors, implement `AbstractExecutor`.
- `src/risk/gates.py` — pure gate functions, evaluated sequentially, short-circuit on first fail.
- `src/risk/kelly.py` — half-Kelly sizing (multiplier=0.5, ceiling=0.25).
- `src/features/pipeline.py` — 7-feature pipeline + triple-barrier labels.
- `src/regime/detector.py` — GaussianHMM 3-state fit/predict/persist.
- `src/models/trainer.py` — XGBoost direction + meta-label + CPCV validation.
- `src/strategies/filters.py` — 8 pure signal filters (Carver, Chan, Peters, Schwager…).
- `src/strategies/position_sizing.py` — Carver, AFML, Thorp sizing methods.
- `src/diagnostics/` — runtime monitor, signal debugger, trade auditor.
- `frontend/` — React + Vite + Tailwind dashboard.

## Code Conventions
- All async I/O uses `asyncio.Lock` (never `threading.Lock` in async code).
- `runtime_config` (mutable) is separate from `get_settings()` (immutable, lru_cached).
- Risk gates are pure functions — no side effects, fully testable.
- Never hard-code risk thresholds — always read from `RiskSettings`.
- Live trading gate requires `TRADING_MODE=live` in `.env` — cannot be set programmatically.
- Always use `structlog` for logging — never `print()` or stdlib `logging` directly.
- `StorageBackend` is the only persistence layer — never write to DB outside it.

## Security
- All API endpoints require `X-API-Key` header.
- `/execution-mode` requires additional `OPERATOR_SECRET` (second factor).
- CORS wildcard `*` is forbidden — specify explicit origins.
- Never expose API without TLS termination when bound outside loopback.

## Testing
- `pytest tests/ -v` — 60% coverage minimum enforced.
- `invalidate_settings_cache()` between test cases.
- Tests live in `tests/` — mirror `src/` structure.
