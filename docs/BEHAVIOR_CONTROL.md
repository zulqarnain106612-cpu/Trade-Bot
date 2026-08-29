# Trade-Bot Behavior Control Reference

Every setting/parameter that governs bot behavior, who can change it, when it
takes effect, and what kind of intelligence (if any) drives it.

## 1. Who can change what

| Owner | Mechanism | Takes effect | Examples |
|---|---|---|---|
| **User (operator)** | `.env` file, restart required | On next process start | Exchange keys, `TRADING_MODE`, Kelly ceiling, HMM/XGBoost hyperparams, feature windows — everything in `src/config.py` `Settings`/sub-settings (immutable, `lru_cache`d for process lifetime) |
| **User (operator)** | REST API, live | **Immediately, no restart** | `POST /execution-mode` (auto/restricted/manual), `POST /risk-controls` (stop-loss/take-profit/max-hold toggles+values), approvals via `POST /approvals/{id}` |
| **Claude / a developer** | Editing `src/config.py`, `src/risk/*.py`, `src/regime/detector.py` source, then redeploy | On next restart | Any hard-coded constant, validator logic, model architecture |
| **Trade-Bot itself (ML/statistical)** | In-process computation, no config write | Immediately, per-signal or per-trade | Kelly fraction sizing, regime-based position scalar, entropy-gated confidence scaling, meta-label gate, drift detection halt recommendation |

**There is no "self-rewriting config" path today.** The bot's self-adjusting behavior is entirely computed at inference time from live data (see §3) — it does not persist new values back into `RuntimeConfig` or `.env`. Drift detection *flags* problems (`GET /performance-drift`, `GET /debug/drift`); a human must act (e.g. flip execution mode to `manual`, or restart with new hyperparameters).

## 2. Config surface — `src/config.py` (env-var driven, restart required)

All are Pydantic `BaseSettings`, prefixed by env var group, loaded once via `get_settings()`.

- **`RiskSettings`** (`RISK_*`): drawdown halt %, consecutive-loss halt, max position %, `kelly_multiplier` (0.5 = half-Kelly), `kelly_ceiling`, OOS Sharpe/drawdown live-gate thresholds, min trades for live gate, restricted-mode notional limit + approval timeout, Almgren-Chriss slippage coefficients, stop-loss/take-profit/max-hold **defaults** (seed values only — live values move to `RuntimeConfig`, see §4).
- **`HMMSettings`** (`HMM_*`): regime count, covariance type, EM iterations, entropy-gate threshold/floor — controls the *unsupervised* regime detector.
- **`XGBoostSettings`** (`XGB_*`): tree count, depth, learning rate, subsampling, regularization — controls the *supervised* direction/meta-label classifiers.
- **`FeatureSettings`** (`FEATURE_*`): frac-diff order, rolling windows (VWAP/OFI/ATR/Sharpe/volume), triple-barrier labeling parameters, CPCV cross-validation splits.
- **`StorageSettings`**, **`APISettings`**, **`IntelligenceSettings`** (`INTELLIGENCE_*`): on-chain data provider keys/cache TTLs (Glassnode, CryptoQuant, Arkham, Dune, Coinglass) — feed the intelligence layer described in §3.
- **`Settings`** root: `TRADING_MODE` (paper/live — live requires explicit env var, cannot be flipped in code), `EXECUTION_MODE` default, symbol universe, capital, paper-trading minimum days before live unlock.

Change path: edit `.env` → restart process. `invalidate_settings_cache()` exists only for test isolation, never called in production (deliberately — see VUL-028 comment in `config.py`, a torn-state race was found and fixed).

## 3. Trade-bot's own intelligence (what actually "thinks")

None of this is a neural net making free-form decisions — it's a pipeline of statistical/ML estimators, each bounded by the config above, feeding a rule-based gate cascade (`CognitiveEngine` in `src/risk/cognitive_engine.py`).

| Layer | Type of intelligence | What it controls live | Bounded by |
|---|---|---|---|
| **Regime detection** (`src/regime/detector.py`) | Gaussian **HMM** (Hamilton 1989), unsupervised, 3 latent states (ranging/trending/volatile) | Position-size scalar via posterior entropy (`RegimePrediction.position_scalar`) — high regime uncertainty shrinks size automatically | `HMMSettings.entropy_threshold/entropy_scalar_floor` |
| **Direction model** | **XGBoost** classifier | Long/short/no-trade signal probability | `XGBoostSettings` |
| **Meta-label gate** | Second **XGBoost** classifier (AFML meta-labeling) | Filters primary signal — vetoes low-conviction trades | same `XGBoostSettings`, separate instance |
| **Calibration** (`src/intelligence/calibration.py`, new) | Probability calibration (e.g. isotonic/Platt-style) | Corrects raw model probabilities before they hit Kelly sizing | n/a — see file for method |
| **Position sizing** | **Kelly criterion** (Kelly 1956), fractional/half-Kelly with hard ceiling | Trade notional | `RiskSettings.kelly_multiplier/kelly_ceiling` — mathematical ceiling, not a target |
| **Portfolio correlation** (`src/risk/portfolio_correlation.py`) | Statistical (correlation matrix) | Diversification/exposure limits across concurrent positions | risk settings |
| **Performance drift** (`src/risk/performance_drift.py`) | Statistical hypothesis testing (Sharpe/accuracy/win-rate/drawdown drop vs. training baseline) | **Detection only** — raises `DriftDetected`, exposed via API; does not auto-halt or auto-retrain | `_DRIFT_SHARPE_DROP_PP` and siblings (module constants, not env-configurable today) |
| **Cognitive gate cascade** (`src/risk/cognitive_engine.py`) | Rule-based ensemble of validators (`QuantValidator`, `ProbabilityValidator` w/ Monte-Carlo CVaR, `RiskValidator`, `BlockchainValidator`, `RegimeValidator`) | Final go/no-go + composite confidence per signal | each validator's own thresholds, mostly sourced from `RiskSettings`/`HMMSettings` |
| **On-chain intelligence** (`src/intelligence/*`) | External data aggregation (Glassnode, CryptoQuant, Arkham, Dune, Coinglass, DeFiLlama) + causal inference module (`causal_inference.py`) | Feeds `BlockchainValidator` and features, not a standalone decision-maker | `IntelligenceSettings` |

**None of these layers write back to config.** They compute a decision per call; the next call re-reads current `Settings`/`RuntimeConfig`. So "the bot controlling its own behavior" today = **read-only adaptive inference**, not **self-modifying configuration**. There's no online-learning/auto-retrain loop wired into the live path.

## 4. Runtime-mutable state — `RuntimeConfig` (`src/config.py:602`, no restart)

Process-wide, `asyncio.Lock`-protected, seeded from `RiskSettings` defaults at startup:

- `execution_mode` — `GET/POST /execution-mode`. AUTOMATIC / RESTRICTED / MANUAL.
- `stop_loss_enabled`, `stop_loss_pct`, `take_profit_enabled`, `take_profit_pct`, `max_holding_period_s` — `GET/POST /risk-controls`. Bounded by the same `ge/le` ranges as their `RiskSettings.*_default` counterparts (enforced by the API layer's Pydantic validation before the setter is called).

These are the **only** two operator-toggleable, no-restart controls in the system, and both are User-only (operator-secret gated per the code comments — not exposed to unauthenticated callers, and not toggleable by Claude/the model itself).

## 5. Gaps vs. what was requested

The user's request implies three toggle axes per parameter: Claude-editable-at-runtime, User-editable-at-runtime, Bot-self-editable-at-runtime. **Today only 2 of ~40 parameter groups (`execution_mode`, `risk-controls`) have a live-mutable path, and both are User-only.** Everything else (model hyperparameters, Kelly ceiling, feature windows, HMM config) requires an `.env` edit + restart. There is no code path today for Claude or the bot itself to mutate `RuntimeConfig` or `.env` at runtime — building that would mean adding write-enabled endpoints/hooks, which is a deliberate architectural choice per the VUL-028 note (avoiding torn-state races on hot config). Flag if you want that extended; it's a real scope decision, not a doc gap.
