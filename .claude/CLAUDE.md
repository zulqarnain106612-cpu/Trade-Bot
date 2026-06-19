# CLAUDE.md — Trade-Bot

## Identity
Production algorithmic trading bot — real money. Python 3.11+. Binance primary, OKX secondary.

## Absolute Code Rules
- Every function: fully implemented, typed, structlog, specific exceptions only
- No stubs / pass / NotImplementedError / TODO / FIXME / placeholder / demo / minimal / partial
- No print() — structlog only
- No bare except — name the exception type
- No mutable default arguments
- No global state outside src/config.py
- No .ipynb anywhere
- Only README.md allowed as .md

## File Reading Protocol (TOKEN GATE)
**Before reading any file, check this table. Read ONLY what the task requires.**

| Task | Read these files | Skip everything else |
|---|---|---|
| Fix bug in X | only the file containing X | all others |
| Add feature to module Y | Y + its direct imports | transitive deps |
| Write new file | CLAUDE.md only | all src/ |
| Debug signal | signal_engine.py + pipeline.py | execution/, api/, frontend/ |
| Debug execution | paper.py or live.py + gates.py | features/, models/, regime/ |
| Debug risk | kelly.py + gates.py | all others |
| Retrain / model | trainer.py + pipeline.py | execution/, api/, frontend/ |
| API endpoint | api/main.py + orchestrator.py | features/, models/, regime/ |
| Frontend | frontend/src/ only | all src/ |
| Tests | test file + the module it tests | all others |

**Never load the full project. One file = one task scope.**

## Architecture (reference — do not re-read src/ to verify)

### Signal Flow
```
fetcher → storage → pipeline → detector → trainer → signal_engine → orchestrator → executor
```

### Module Contracts (signatures only)
| Module | Key exports |
|---|---|
| src/config.py | Settings, Timeframe, TradingMode, TIMEFRAME_SECONDS |
| src/data/storage.py | StorageBackend, RegimeSnapshotRecord, BarRecord, TradeRecord |
| src/data/fetcher.py | MarketDataFetcher.bootstrap_history(), fetch_ticker_price() |
| src/features/pipeline.py | build_feature_matrix(bars) → FeatureMatrix |
| src/regime/detector.py | RegimeDetector.fit(features), .predict(features) → RegimeResult |
| src/models/trainer.py | ModelTrainer.train_direction(fm), .train_meta_label(fm, model) |
| src/risk/kelly.py | compute_kelly_size(), compute_win_loss_stats() |
| src/risk/gates.py | RiskGates.check_all() → GateResult |
| src/execution/paper.py | PaperExecutor.submit_signal(), .get_daily_pnl(), .get_consecutive_losses() |
| src/execution/live.py | LiveExecutor (same interface as PaperExecutor) |
| src/engine/signal_engine.py | SignalEngine.tick() → SignalResult, .swap_models() |
| src/engine/orchestrator.py | Orchestrator.startup(), .run(), .stop(), .shutdown() |
| src/api/main.py | FastAPI app, WebSocket /ws, lifespan handler |

## Signal Architecture
| Component | Choice |
|---|---|
| Regime | GaussianHMM 3-state: ranging / trending / volatile |
| Features | frac-diff d=0.4, VWAP deviation, OFI, realized-vol ratio, ATR momentum, rolling Sharpe, volume z-score |
| Primary model | XGBoost classifier — direction (long/short) |
| Meta-label gate | XGBoost — gates whether to bet at all |
| Labels | Triple-barrier: profit-taking · stop-loss · time-exit (López de Prado Ch.3) |
| Validation | CPCV only — never standard k-fold (López de Prado Ch.7) |
| Sizing | Half-Kelly: multiplier=0.5, ceiling=0.25 (Kelly 1956) |

## Risk Gates (never weaken)
| Gate | Limit |
|---|---|
| Daily drawdown halt | 2% |
| Consecutive loss halt | 3 trades |
| Regime gate | no new positions when state = volatile |
| Max position size | 5% of capital |
| Default mode | paper — live requires TRADING_MODE=live in .env |
| Live gate (per TF) | OOS Sharpe > 1.5, max DD < 15%, 500+ trades |

## Timeframes
| TF | Bars | Mode |
|---|---|---|
| scalping | 1m | paper |
| intraday | 15m | primary real-money |
| swing | 4h | paper |

## Execution Modes
- AUTOMATIC — no approvals
- RESTRICTED — autonomous below notional limit, approval above, auto-skip on timeout
- MANUAL — every trade waits for approval

## Stack
ccxt (Binance+OKX) · xgboost · hmmlearn · scikit-learn · pandas · numpy · scipy · statsmodels · aiosqlite · fastapi · uvicorn · websockets · pydantic-settings · structlog · rich · joblib

## References
- López de Prado (2018) AFML: Ch.3 triple-barrier, Ch.4 meta-labeling, Ch.5 frac-diff, Ch.7 CPCV
- Chan (2013) Algorithmic Trading: Winning Strategies
- Kelly (1956) A New Interpretation of Information Rate
- Hamilton (1989) HMM regime switching, Econometrica 57(2)

## Per-Prompt Contract
- Context loaded from this file — never re-paste system prompt
- State the single file being worked on before writing any code
- Read ONLY files permitted by TOKEN GATE table above
- Each response: one file, complete, production-ready, state next file

## Context Loading Directive (appended)
- On session start, read `PROJECT_SUMMARY.md` at project root for full-project structural context — bullet-only, AST-derived, real file contracts.
- On any debugging / fix-my-code / review request, read `DIAGNOSTICS.md` at project root first — it mirrors the VS Code Problems panel exactly (ruff + pyright + eslint).
- Do NOT open individual source files to "check for issues" — DIAGNOSTICS.md already contains every current issue with exact file:line:col.
- Only open a source file once DIAGNOSTICS.md or PROJECT_SUMMARY.md points you to a specific line that needs editing.
- If DIAGNOSTICS.md is missing or stale (older than the last edit), say so and ask the user to run: `python3 scripts/export_diagnostics.py`
- If PROJECT_SUMMARY.md is missing, say so and ask the user to run: `python3 scripts/gen_project_summary.py . --force`
