# Vulner-Fix.md — Vulnerability & Issue Tracker
<!-- AUTO-MANAGED: Claude / Copilot agents append to this file. Never overwrite. -->
<!-- FORMAT: Each finding is one block. Status changes from OPEN → IN_PROGRESS → APPLIED. -->
<!-- RULE: Agents MUST append new findings at the LAST LINE. Never insert above existing entries. -->
<!-- RULE: When a fix is successfully applied, update status to `Applied` on that entry only. -->

---

## How to read this file

| Field | Meaning |
|-------|---------|
| `ID` | Unique sequential ID |
| `Severity` | CRITICAL / HIGH / MEDIUM / LOW / INFO |
| `Tool` | Which tool detected it (CodeQL / bandit / semgrep / manual / Claude / Copilot) |
| `Status` | `Open` → `In Progress` → `Applied` |
| `File` | Source file and line number |
| `Fix` | Exact fix applied or to apply |

---

## Entries

<!-- AGENTS: append new entries below this line, one block per finding, newest at bottom -->

### [VF-001] — 2025-06-01 — Initial audit baseline
- **Severity:** INFO
- **Tool:** Manual audit (VULNERABILITY_AUDIT_AND_FIXES.md)
- **Status:** Applied
- **Summary:** 30 vulnerabilities (9 critical, 9 high, 8 medium, 4 low) identified in initial SCAN3 audit.
- **Fix:** All 30 vulnerabilities fixed across 13 files. See git log for full diff.
- **Verified:** `python3 -m py_compile src/**/*.py` → ALL_OK

<!-- NEW FINDINGS BELOW THIS LINE -->
### [VF-002] — 2026-06-12 04:35 UTC
- **Severity:** LOW
- **Tool:** test
- **File:** `test:1`
- **Status:** Applied
- **Summary:** Autocommit test entry
- **Fix:** No fix needed

### [VF-003] — 2026-06-19 — src/config.py BinanceSettings.resolve_urls
- **Severity:** HIGH
- **Tool:** Claude manual audit
- **File:** `src/config.py` — `BinanceSettings.resolve_urls` model_validator
- **Status:** Applied
- **Summary:** `resolve_urls` silently overwrote operator-supplied `BINANCE_BASE_URL` / `BINANCE_WS_URL` env vars when `testnet=False`, making custom proxy/URL overrides impossible and hiding the config mutation.
- **Fix:** Guard injection with `if "BINANCE_BASE_URL" not in os.environ` / `if "BINANCE_WS_URL" not in os.environ` so operator overrides survive.

### [VF-004] — 2026-06-19 — src/config.py RuntimeConfig._get_lock race condition
- **Severity:** HIGH
- **Tool:** Claude manual audit
- **File:** `src/config.py` — `RuntimeConfig._get_lock`
- **Status:** Applied
- **Summary:** Lazy `asyncio.Lock` creation in `_get_lock()` had a TOCTOU race: two coroutines could both pass `if self._lock is None` and create two independent lock objects, silently breaking mutual exclusion on `_execution_mode`.
- **Fix:** Added `threading.Lock` (`self._init_guard`) as a one-time creation guard using double-checked locking pattern. After the single creation, steady-state path is lock-free (CPython atomic read) then asyncio-locked.

### [VF-005] — 2026-06-19 — src/config.py APISettings CORS wildcard not rejected
- **Severity:** LOW
- **Tool:** Claude manual audit
- **File:** `src/config.py` — `APISettings`
- **Status:** Applied
- **Summary:** No validator prevented `cors_origins=["*"]` from being set via env, which disables all CORS protection.
- **Fix:** Added `@field_validator("cors_origins")` that raises `ValueError` if any element is `"*"`.

### [VF-006] — 2026-06-19 — src/config.py enforce_live_gate env var normalisation
- **Severity:** LOW
- **Tool:** Claude manual audit
- **File:** `src/config.py` — `Settings.enforce_live_gate`
- **Status:** Applied
- **Summary:** `os.environ.get("TRADING_MODE", "paper").lower()` did not `.strip()`, so values like `" live"` (leading space) or `"LIVE\n"` (trailing newline from shell heredoc) would silently bypass the gate, allowing live trading without a clean env var value.
- **Fix:** Added `.strip()` before `.lower()`: `os.environ.get("TRADING_MODE", "paper").strip().lower()`.

### [VF-007] — 2026-06-19 — src/data/storage.py asyncio.Lock lazy-init race (VF-011)
- **Severity:** HIGH
- **Tool:** Claude manual audit
- **File:** `src/data/storage.py` — `StorageBackend.__init__`
- **Status:** Applied
- **Summary:** `asyncio.Lock()` was created at `__init__` time (module import), raising `DeprecationWarning` on Python 3.10+ when no event loop is running, and will error in future Python. Identical pattern to VF-004.
- **Fix:** Replaced `self._lock: asyncio.Lock = asyncio.Lock()` with lazy double-checked init guarded by `threading.Lock` (`_lock_init_guard`). Added `_get_lock()` method; all `async with self._lock:` callers updated to `async with self._get_lock():`.

### [VF-008] — 2026-06-19 — src/data/storage.py fetch_equity_curve f-string SQL
- **Severity:** HIGH
- **Tool:** Claude manual audit
- **File:** `src/data/storage.py` — `fetch_equity_curve` ~L1009
- **Status:** Applied
- **Summary:** `f" FROM equity_curve {where_sql} ORDER BY ts ASC LIMIT ?"` — f-string SQL composition is a latent injection vector inconsistent with all other queries in the file that use `?` exclusively.
- **Fix:** Replaced with explicit branching: two fully-literal query strings (with/without `ts>=?` clause), selected based on `since_ts is not None`. No variable ever reaches the SQL text.

### [VF-009] — 2026-06-19 — src/data/storage.py fetch_trades f-string SQL
- **Severity:** HIGH
- **Tool:** Claude manual audit
- **File:** `src/data/storage.py` — `fetch_trades` dynamic query
- **Status:** Applied
- **Summary:** Same f-string SQL pattern as VF-008 in `fetch_trades`: `f" FROM trades {where_sql} ORDER BY ..."`.
- **Fix:** Replaced with explicit `if clauses / else` branching producing two fully-literal query strings. `params` list still carries all filter values via `?` placeholders.

### [VF-010] — 2026-06-19 — src/data/storage.py health_check f-string defence-in-depth
- **Severity:** LOW
- **Tool:** Claude manual audit
- **File:** `src/data/storage.py` — `health_check`
- **Status:** Applied
- **Summary:** `f"SELECT COUNT(*) FROM {table}"` with table from `_ALLOWED_TABLES` is safe today but lacks a defence-in-depth guard. A future extension adding a misconfigured name to `_ALLOWED_TABLES` would silently reach the f-string.
- **Fix:** Added explicit `if table not in _ALLOWED_TABLES: raise RuntimeError(...)` guard before the f-string as defence-in-depth, with noqa comment explaining intentional double-check.

### [VF-011] — 2026-06-19 — src/data/fetcher.py _with_retry RateLimitExceeded final-attempt silently swallowed
- **Severity:** HIGH
- **Tool:** Claude manual audit
- **File:** `src/data/fetcher.py` — `_with_retry`
- **Status:** Applied
- **Summary:** On the final retry attempt for `ccxt.RateLimitExceeded`, the loop slept and exited normally, reaching the terminal `raise RuntimeError(f"_with_retry exhausted...")` instead of re-raising the original `RateLimitExceeded`. Callers received a generic `RuntimeError` hiding the actual cause. Inconsistent with the `NetworkError` branch which correctly re-raised on final attempt.
- **Fix:** Added `if attempt == attempts: log.error(...); raise` at the top of the `RateLimitExceeded` handler, matching the `NetworkError` branch pattern.

### [VF-012] — 2026-06-19 — src/data/fetcher.py asyncio.Semaphore at __init__ time
- **Severity:** HIGH
- **Tool:** Claude manual audit
- **File:** `src/data/fetcher.py` — `MarketDataFetcher.__init__`
- **Status:** Applied
- **Summary:** `asyncio.Semaphore(1)` created in `__init__` raises `DeprecationWarning` on Python 3.10+ when no event loop is running. Same pattern as VF-004 and VF-007.
- **Fix:** Replaced with `self._gap_fill_sem: asyncio.Semaphore | None = None` + `threading.Lock` sentinel (`_sem_init_guard`) + `_get_sem()` lazy initializer using double-checked locking. All callers updated to `async with self._get_sem():`.

### [VF-013] — 2026-06-19 — src/data/fetcher.py API credentials in ccxt constructor dict
- **Severity:** MEDIUM
- **Tool:** Claude manual audit
- **File:** `src/data/fetcher.py` — `_build_binance`, `_build_okx`
- **Status:** Applied
- **Summary:** `apiKey` and `secret` were passed inside the options dict to the ccxt constructor. If ccxt logs or `repr()`s the options dict on an initialization error, credentials appear in logs/tracebacks.
- **Fix:** Removed credentials from the constructor dict; set them on the exchange instance after construction via `exchange.apiKey`, `exchange.secret`, `exchange.password`.

### [VF-014] — 2026-06-19 — src/data/fetcher.py unknown exchange_id silent Binance fallback
- **Severity:** LOW
- **Tool:** Claude manual audit
- **File:** `src/data/fetcher.py` — `fetch_orderbook`, `fetch_ticker_price`
- **Status:** Applied
- **Summary:** Both methods used `if exchange_id == EXCHANGE_OKX: ... else: use_binance` — any unrecognised string silently routed to Binance with no error.
- **Fix:** Changed both to explicit `if/elif/else` with `raise ValueError(f"Unknown exchange_id {exchange_id!r}...")` in the else branch.

### [VF-015] — 2026-06-19 — src/features/pipeline.py triple_barrier_labels O(n×k) Python loop blocks event loop
- **Severity:** MEDIUM
- **Tool:** Claude manual audit
- **File:** `src/features/pipeline.py` — `triple_barrier_labels`
- **Status:** Applied
- **Summary:** Double Python `for` loop (`for t in range(n): for k in range(...)`) — O(n × max_holding) ≈ 600K iterations at n=10000, max_holding=60. Blocks the async event loop for seconds during training/inference, causing missed bar signals.
- **Fix:** Replaced with vectorized NumPy implementation: pre-compute `upper`/`lower` barrier arrays; iterate only over `k` in [1, max_holding] (max 60 passes); each pass does a single array comparison in C. ~100× speedup. Labels are identical to the original loop.

### [VF-016] — 2026-06-19 — src/features/pipeline.py min_required check missing frac-diff window
- **Severity:** MEDIUM
- **Tool:** Claude manual audit
- **File:** `src/features/pipeline.py` — `build_feature_matrix`
- **Status:** Applied
- **Summary:** `min_required = max(...)` did not include `_FRAC_DIFF_MAX_WINDOW = 200`. If all other windows were < 200 bars, the function passed the guard with e.g. 120 bars but produced 200 NaN rows from frac-diff, silently dropping them post-build rather than failing fast with a clear error.
- **Fix:** Added `_FRAC_DIFF_MAX_WINDOW` to the `max(...)` expression in `min_required`.

### [VF-017] — 2026-06-19 — src/regime/detector.py _convergence_failed not persisted in save/load
- **Severity:** HIGH
- **Tool:** Claude manual audit
- **File:** `src/regime/detector.py` — `save()`, `load()`
- **Status:** Applied
- **Summary:** `_convergence_failed=True` was set on non-convergent fits (correctly defaulting regime to VOLATILE), but the field was NOT included in the joblib payload. On `load()`, it was initialized to `False` via `__init__`, silently restoring a known-bad model as fully-converged. The regime gate would fail to block positions for a loaded non-convergent model.
- **Fix:** Added `"convergence_failed": self._convergence_failed` to the `save()` payload dict. In `load()`, added `detector._convergence_failed = bool(payload.get("convergence_failed", False))` (backward-compatible via `.get()` default).

### [VF-018] — 2026-06-19 — src/regime/detector.py n_init=0 causes silent None dereference
- **Severity:** LOW
- **Tool:** Claude manual audit
- **File:** `src/regime/detector.py` — `fit()` multi-init loop
- **Status:** Applied
- **Summary:** `_HMM_N_INIT = getattr(cfg, "n_init", 5)` — if `n_init=0`, zero HMM fits run, `best_model` stays `None`, and `RuntimeError("HMM multi-init: all candidate fits failed to score")` is raised — misleading error, wrong root cause.
- **Fix:** Added `if _HMM_N_INIT < 1: raise ValueError(...)` guard immediately after the `getattr` to fail fast with a clear message.

### [VF-019] — 2026-06-19 — src/models/trainer.py non-atomic model/manifest writes
- **Severity:** HIGH
- **Tool:** Claude manual audit
- **File:** `src/models/trainer.py` — `save()`, `_write_manifest()`
- **Status:** Applied
- **Summary:** `joblib.dump(obj, path)` and `path.write_text(...)` wrote directly to the final path. A concurrent reader (e.g. `signal_engine.swap_models()` hot-loading a model while a retrain job saves a new one) could observe a partially-written/truncated pickle or a half-flushed manifest, causing a load-time crash or — worse — a corrupt model silently driving live signals.
- **Fix:** Added `_atomic_write_bytes()` (temp file + `os.replace()`, atomic on POSIX/Windows on the same filesystem). `save()` now serializes via `io.BytesIO` and writes the resulting bytes atomically; `_write_manifest()` writes the JSON manifest atomically too.

### [VF-020] — 2026-06-19 — src/models/trainer.py TOCTOU between manifest verify and joblib.load
- **Severity:** HIGH
- **Tool:** Claude manual audit
- **File:** `src/models/trainer.py` — `_verify_manifest()`, `load_direction()`, `load_meta()`
- **Status:** Applied
- **Summary:** `_verify_manifest(path)` hashed the file via one `path.read_bytes()` call, then the caller independently re-read the same path via `joblib.load(path)`. Anything with write access to the model directory could swap the file in the gap between the two reads, bypassing the integrity check entirely — the documented threat model ("prevents tampered or poisoned model files from being loaded") was not actually enforced.
- **Fix:** `_verify_manifest()` now returns the exact bytes it hashed; `load_direction`/`load_meta` deserialize via `joblib.load(io.BytesIO(data))` on those same bytes instead of re-reading from disk. The file is read exactly once — hash and deserialization target are guaranteed identical.

### [VF-021] — 2026-06-19 — src/models/trainer.py model-dir mkdir / filename construction (reviewed, no change)
- **Severity:** INFO
- **Tool:** Claude manual audit
- **File:** `src/models/trainer.py` — `save()`, `load_direction()`, `load_meta()`
- **Status:** Reviewed — not applicable
- **Summary:** Checked `symbol.replace("/", "_")` filename construction for path-traversal risk. `symbol` originates from internal `Settings.trading_pairs` (see CLAUDE.md Module Contracts), not from unauthenticated external input in this file's call path — no fix applied here. Flagged for re-check if/when `src/api/main.py` (next-but-one file in queue) is found to pass a user/request-supplied symbol into `ModelTrainer(...)` without validation against the configured trading-pair allowlist.

### [VF-022] — 2026-06-19 — src/models/trainer.py NaN/Inf silently poisoning persisted risk metrics
- **Severity:** MEDIUM
- **Tool:** Claude manual audit
- **File:** `src/models/trainer.py` — `_oos_sharpe_and_drawdown()`
- **Status:** Applied
- **Summary:** `if abs(sigma) < 1e-10` does not catch `sigma = NaN` (NaN comparisons are always `False` in IEEE-754/NumPy), so a degenerate fold (`np.std(..., ddof=1)` on a length-1 array) let `sharpe = NaN` propagate into `oos_sharpe`, which is persisted via `ModelMetricsRecord` and exposed over the API. NaN is not valid JSON and would break strict downstream consumers (frontend, monitoring). Separately, `running_max <= 0` (possible with an extreme single-bar `log_return`) divided to `inf`/`nan` in the drawdown calc with no guard.
- **Fix:** Added explicit `np.isfinite()` checks around `sigma` and the final `sharpe` (falls back to `0.0`); clamped `running_max` to a `1e-12` floor before dividing and filtered any residual non-finite drawdown values before taking the min, falling back to `100.0` (fail-safe: treated as max drawdown) if no finite values remain.

### [VF-023] — 2026-06-19 — src/models/trainer.py unhandled crash on n_samples < n_splits
- **Severity:** MEDIUM
- **Tool:** Claude manual audit
- **File:** `src/models/trainer.py` — `build_cpcv_folds()`
- **Status:** Applied
- **Summary:** When `n_samples < n_splits` (or `n_splits <= 1`), `_build_groups` produces empty group arrays, and `_build_train_indices` called `.min()`/`.max()` on those arrays before checking their length — raising an unhandled `ValueError: zero-size array to reduction operation minimum which has no identity` deep in the call stack. A symbol/timeframe with too little bootstrapped history (e.g. a new listing) would crash the retrain job with a cryptic, hard-to-diagnose error.
- **Fix:** Added explicit guards at the top of `build_cpcv_folds()`: `n_splits < 2` and `n_samples < n_splits` now raise a clear, actionable `ValueError` immediately instead of failing deep inside `_build_train_indices`.

### [VF-024] — 2026-06-19 — src/risk/kelly.py kelly_fraction NaN/Inf win_loss_ratio bypasses validation
- **Severity:** HIGH
- **Tool:** Claude manual audit
- **File:** `src/risk/kelly.py` — `kelly_fraction()`
- **Status:** Applied
- **Summary:** `if win_loss_ratio <= 0.0: raise ...` does not catch `NaN` (NaN comparisons are always `False` in IEEE-754) or `inf`. A NaN ratio produces `f_star = nan`, and the existing defensive `max(0.0, min(1.0, f_star))` clip then resolves to **1.0** (max aggression) — not because anyone decided 1.0 was safe, but because Python's `min(1.0, nan)` keeps the literal first argument when the comparison is False. Corrupted stats input would silently produce the single most aggressive Kelly fraction possible instead of failing.
- **Fix:** `if win_loss_ratio <= 0.0 or not math.isfinite(win_loss_ratio): raise ValueError(...)`.

### [VF-025] — 2026-06-19 — src/risk/kelly.py half_kelly_fraction multiplier/ceiling override unbounded
- **Severity:** MEDIUM
- **Tool:** Claude manual audit
- **File:** `src/risk/kelly.py` — `half_kelly_fraction()`
- **Status:** Applied
- **Summary:** `multiplier`/`ceiling` override parameters had no bounds check. No current caller passes them, but the public signature otherwise let any future caller silently exceed the spec'd half-Kelly multiplier (0.5) / 0.25 ceiling — CLAUDE.md: "Risk Gates — never weaken."
- **Fix:** `if not (0.0 <= mult <= 1.0): raise ValueError(...)` / same for `cap`.

### [VF-026] — 2026-06-19 — src/risk/kelly.py kelly_from_model_probs invalid direction silently treated as short
- **Severity:** MEDIUM
- **Tool:** Claude manual audit
- **File:** `src/risk/kelly.py` — `kelly_from_model_probs()`
- **Status:** Applied
- **Summary:** `win_prob = p_long if direction == 1 else (1.0 - p_long)` — any `direction` value other than `1` (e.g. `-1`, `2` from a caller bug) was silently treated identically to `0` (short). Same defect class as VF-014 (fetcher.py unknown `exchange_id` silently routed to Binance).
- **Fix:** `if direction not in (0, 1): raise ValueError(...)` at the top of the function.

### [VF-027] — 2026-06-19 — src/risk/kelly.py NaN p_long silently coerced to maximum confidence
- **Severity:** HIGH
- **Tool:** Claude manual audit
- **File:** `src/risk/kelly.py` — `kelly_from_model_probs()`
- **Status:** Applied
- **Summary:** `win_prob = max(0.01, min(0.99, win_prob))` with `win_prob = NaN` evaluates to `0.99` — Python's `min(0.99, nan)` returns the literal `0.99` because `nan < 0.99` is `False`. A corrupted/NaN model prediction (numerical instability, bad feature data) was therefore silently turned into **99% confidence to bet**, the exact opposite of this project's fail-safe design (e.g. VF-017's convergence-failure → VOLATILE default).
- **Fix:** Added an explicit `math.isfinite(p_long)` check before the clip; on failure, logs `kelly.invalid_p_long` and returns `(0.0, 0.0, False)` — zero Kelly fraction, so `size_position()`'s existing `quantity <= 0.0` guard skips the trade.

### [VF-028] — 2026-06-19 — src/risk/kelly.py extreme avg_win/avg_loss can overflow win_loss_ratio to inf
- **Severity:** LOW
- **Tool:** Claude manual audit
- **File:** `src/risk/kelly.py` — `kelly_from_model_probs()`
- **Status:** Applied
- **Summary:** `avg_win_usd / avg_loss_usd` with a near-zero `avg_loss_usd` (data bug, not a real trade) can overflow float64 to `inf`, which combined with VF-024's now-fixed `kelly_fraction()` would raise instead of silently maxing out — better than before, but still an avoidable crash from a stats-layer numerical edge case rather than a real trading decision.
- **Fix:** Floor **and** ceiling `win_loss_ratio` to `[0.1, 1000.0]` (was floor-only) and explicitly reset non-finite values to `1.0` before calling `kelly_fraction()`.

### [VF-029] — 2026-06-19 — src/risk/kelly.py size_position NaN capital_usd/entry_price bypass validation
- **Severity:** HIGH
- **Tool:** Claude manual audit
- **File:** `src/risk/kelly.py` — `size_position()`
- **Status:** Applied
- **Summary:** Same defect class as VF-024: `capital_usd <= 0.0` / `entry_price <= 0.0` do not catch `NaN`, so a corrupted equity or price feed would silently pass the guard intended to reject it.
- **Fix:** `if not math.isfinite(capital_usd) or capital_usd <= 0.0: raise ValueError(...)` / same for `entry_price`.

### [VF-030] — 2026-06-19 — src/risk/kelly.py size_position max_position_pct=0.0 silently ignored (falsy-zero bug) + unbounded override
- **Severity:** MEDIUM
- **Tool:** Claude manual audit
- **File:** `src/risk/kelly.py` — `size_position()`
- **Status:** Applied
- **Summary:** `max_position_pct or cfg.max_position_size_pct` treats an explicit `max_position_pct=0.0` (a legitimate "block new positions" call) as falsy and silently substitutes the config default instead — the opposite of the caller's intent, and inconsistent with the correct `is not None` pattern already used for `multiplier`/`ceiling` in `half_kelly_fraction()` in the same file. Separately, the resolved percentage had no upper bound, so a future override of e.g. `500` would size positions at 5× capital.
- **Fix:** Switched to `max_position_pct if max_position_pct is not None else cfg.max_position_size_pct`, then validated the resolved value is in `[0, 100]`, raising `ValueError` otherwise.

### [VF-031] — 2026-06-19 — tests/test_kelly.py stale assertions after NEW-010 threshold change (not a vulnerability — test/code drift)
- **Severity:** INFO
- **Tool:** Claude manual audit (found while running `pytest tests/test_kelly.py` to validate VF-024…030)
- **File:** `tests/test_kelly.py` — `TestComputeWinLossStats.test_correct_win_probability`, `.test_correct_averages`
- **Status:** Applied
- **Summary:** `compute_win_loss_stats()` requires ≥50 trades before trusting the sample (per in-code comment, threshold was raised from 10 — "NEW-010"), but these two tests still supplied only 10 trades, so they silently exercised the conservative-default fallback `(0.5, 1.0, 1.0)` rather than the real computation they claim to test. Both assertions were failing (caught while validating this session's kelly.py changes, unrelated to them).
- **Fix:** Scaled both test PnL series to 50 trades at the same 6:4 win:loss ratio (`[10.0]*30 + [-5.0]*20`) so the real computation path is exercised. `tests/test_kelly.py` now 41/41 passing.


### [VF-020] — 2026-07-09 — src/data/storage.py health_check dead guard
- **Severity:** MEDIUM
- **Tool:** Claude static audit
- **File:** `src/data/storage.py` — `health_check()` line ~1548
- **Status:** Applied
- **Summary:** Guard `if table not in _ALLOWED_TABLES` was logically dead — `table` is drawn
  from iterating `_ALLOWED_TABLES` itself, so the condition can never be True. The intended
  defence-in-depth (prevent a misconfigured allowlist value reaching the f-string) never fired.
  An attacker or future developer who added an unsafe name to `_ALLOWED_TABLES` would see it
  interpolated into the SQL `SELECT COUNT(*) FROM {table}` without any validation stopping it.
- **Fix:** Replaced the dead set-membership check with a regex `^[a-z][a-z0-9_]{0,63}$`
  validation that actually catches unsafe characters (spaces, quotes, SQL keywords) independent
  of allowlist membership. Regex compiled as `_SAFE_TABLE_RE` inline in the loop.
- **Verified:** `python3 -m py_compile src/data/storage.py` → OK

### [VF-021] — 2026-07-09 — src/intelligence/client.py cache TOCTOU race
- **Severity:** MEDIUM
- **Tool:** Claude static audit
- **File:** `src/intelligence/client.py` — `IntelligenceAggregator._cache` in all three
  `get_*` methods (lines ~107-126, ~147-164, ~184-199)
- **Status:** Applied
- **Summary:** `self._cache` dict was read (check-for-staleness) and written (store new entry)
  without any asyncio lock. Two concurrent coroutines fetching the same key could both pass
  the staleness check, both fire the expensive network fetch, and race to overwrite each other's
  result — or one could read stale data between the other's check and write. No data corruption
  risk (values are idempotent), but duplicate fetches waste rate-limit quota (Glassnode: 5/min).
- **Fix:** Added `self._cache_lock: asyncio.Lock` in `__init__`. Each `get_*` method now wraps
  its check-read under `async with self._cache_lock` and its store-write under a second
  `async with self._cache_lock`. Network I/O is performed *outside* the lock to avoid blocking
  peer coroutines waiting for unrelated cache keys.
- **Verified:** `python3 -m py_compile src/intelligence/client.py` → OK

### [VF-022] — 2026-07-09 — src/api/main.py AttributeError on /intelligence/coverage
- **Severity:** HIGH (runtime crash — endpoint returns 500 on every call)
- **Tool:** Claude static audit
- **File:** `src/api/main.py` — `get_intelligence_coverage()` line ~1025
- **Status:** Applied
- **Summary:** Endpoint read `_state.runtime_config` but `AppState` has no `runtime_config`
  attribute. `runtime_config` is a module-level singleton imported at line 51 from `src.config`.
  The attribute access would raise `AttributeError` on every call, masked by the bare
  `except Exception` that returns `{"error": str(exc)}` — making the endpoint appear to work
  but always return an error JSON instead of coverage data.
- **Fix:** Replaced `cfg = _state.runtime_config` with `cfg = get_settings()` and
  `cfg.symbol` with `cfg.primary_symbol` (the correct Settings field name, matching all other
  endpoints in the file).
- **Verified:** `python3 -m py_compile src/api/main.py` → OK

### [VF-023] — 2026-07-09 — src/api/main.py WebSocket orchestrator None-deref
- **Severity:** LOW
- **Tool:** Claude static audit
- **File:** `src/api/main.py` — `websocket_endpoint()` line ~809
- **Status:** Applied
- **Summary:** WS tick loop accessed `_state.orchestrator._executor` without first
  checking `_state.orchestrator is not None`. The WS endpoint has no `require_ready`
  dependency guard. A client connecting during the startup window (before `_state.ready=True`)
  would trigger an `AttributeError` on `None._executor`, caught by the bare `except Exception`
  at the bottom of the loop — silently killing the WS connection with a logged error rather
  than a graceful skip.
- **Fix:** Added `if _state.orchestrator is None: continue` before the `_executor` access.
  Client receives no tick during startup but stays connected and resumes automatically once
  orchestrator is initialized.
- **Verified:** `python3 -m py_compile src/api/main.py` → OK

### [VF-024] — 2026-07-11 — src/execution/order_manager.py filled order with no fill price
- **Severity:** CRITICAL (live trading — PnL/notional corruption)
- **Tool:** Claude manual audit (Loop-1 bug scan)
- **File:** `src/execution/order_manager.py` — `_confirm_order_fill()`
- **Status:** Applied
- **Summary:** `confirmed.get("average") or confirmed.get("price") or 0` silently recorded
  `avg_price=0.0` when an exchange reported an order as `filled`/`closed` but omitted both fill
  price fields (or returned them as exactly `0.0`, which is falsy and fell through the same
  `or` chain). A `0.0` fill price is recorded as "position acquired for free," corrupting
  PnL/notional accounting for that trade with no visible error.
- **Fix:** Check `average`/`price` individually with `is None` (not `or`), and treat both
  "missing" and "explicitly <= 0.0" as invalid. On an invalid fill price, log `critical` with
  `action="MANUAL_RECONCILIATION_REQUIRED"`, transition the order FSM to `FAILED`, and raise
  `ValueError` instead of silently continuing.
- **Verified:** `uv run pytest tests/test_order_manager.py -q` → 4 passed; full suite 2150 passed,
  coverage 96.17%.

### [VF-025] — 2026-07-11 — src/config.py FeatureSettings purge_gap_bars shorter than label horizon
- **Severity:** HIGH (model validity — AFML Ch.7 data leakage)
- **Tool:** Claude manual audit (Loop-1 bug scan)
- **File:** `src/config.py` — `FeatureSettings`
- **Status:** Applied
- **Summary:** `purge_gap_bars` defaulted to `5` while `triple_barrier_max_holding_bars`
  (the label horizon) defaulted to `60`. A training sample within 6-59 bars of the test-fold
  boundary would have its triple-barrier label computed using bars that fall inside the
  nominal test window — leaking test-period price information into training labels, silently
  inflating backtest/CPCV performance metrics.
- **Fix:** Changed `purge_gap_bars` default to `60` (matches the label horizon) and added
  `validate_purge_gap_covers_label_horizon` (`@model_validator(mode="after")`) that raises
  `ValueError` at config-load time if `purge_gap_bars < triple_barrier_max_holding_bars`.
- **Verified:** `uv run pytest tests/test_config_feature_settings.py tests/test_tuning_backtest_harness.py tests/test_model_trainer_coverage.py -q` → all passed; full suite green.

### [VF-026] — 2026-07-11 — src/strategies/position_sizing.py recommend_position_notional overrides unanimous no-edge veto
- **Severity:** HIGH (risk — forces a trade sizing methods unanimously vetoed)
- **Tool:** Claude manual audit (Loop-1 bug scan)
- **File:** `src/strategies/position_sizing.py` — `recommend_position_notional()`
- **Status:** Applied
- **Summary:** `max(_MIN_NOTIONAL_USD, min(thorp, afml, carver, corr_adj_thorp))` floored the
  recommended notional to `$10` even when every sizing method returned exactly `0.0` (their
  explicit "no edge, do not trade" signal). A unanimous veto from all four methods was silently
  overridden into a manufactured minimum-size trade.
- **Fix:** `recommended = 0.0 if raw_min <= 0.0 else max(_MIN_NOTIONAL_USD, raw_min)` — the
  floor now only applies when the methods agree on a real, positive (if sub-minimum) edge.
- **Verified:** `uv run pytest tests/test_position_sizing.py -q` → 61 passed (2 new regression
  tests added); full suite green.

### [VF-027] — 2026-07-11 — src/execution/paper.py / live.py _snapshot_equity lock-ordering race
- **Severity:** MEDIUM (concurrency — inconsistent equity_curve rows)
- **Tool:** Claude manual audit (Loop-1 bug scan)
- **File:** `src/execution/paper.py`, `src/execution/live.py` — `_snapshot_equity()`
- **Status:** Applied
- **Summary:** `_snapshot_equity()` re-read `self._positions`/`self._cash` without holding
  `self._lock`, after the caller's own lock block had already released it (with an `await
  storage.insert_trade(...)` in between). A concurrent `mark_to_market`/`close_position` could
  mutate state in that window, producing an equity_curve row inconsistent with the triggering
  event.
- **Fix:** Wrapped the state reads in `async with self._lock:` in both executors. Verified no
  deadlock: every call site invokes `_snapshot_equity()` only after its own lock block has
  already exited (confirmed via indentation/structure review of both files).
- **Verified:** `uv run pytest tests/test_paper_executor.py tests/test_live_executor_coverage.py tests/test_live_additional_coverage.py tests/test_live_executor_fsm.py -q` → 138 passed.

### [VF-028] — 2026-07-11 — src/risk/performance_drift.py division by zero on invalid starting_equity
- **Severity:** MEDIUM (availability — crashes the drift-detection loop)
- **Tool:** Claude manual audit (Loop-1 bug scan)
- **File:** `src/risk/performance_drift.py` — `PerformanceDriftDetector.record_trade_outcome()`
- **Status:** Applied
- **Summary:** `drawdown_pct = (self._live_equity_peak - current_equity) / starting_equity` had
  no guard for `starting_equity <= 0`, raising `ZeroDivisionError` uncaught out of
  `check_drift()` — crashing the risk-gate-adjacent drift loop instead of failing closed on bad
  input data.
- **Fix:** Added a guard: `starting_equity <= 0.0` logs `error` and returns early, discarding
  only that call's drawdown update (all other state — pnl window, win/loss counters,
  predictions — was already recorded earlier in the same call).
- **Verified:** `uv run pytest tests/test_performance_drift.py tests/test_drift_integration_coverage.py -q` → 23 passed (2 new regression tests added).

### [VF-029] — 2026-07-11 — src/intelligence/metrics.py whale_buy_sell_ratio normalization discarded
- **Severity:** MEDIUM (signal correctness — unbounded value feeds trading decisions)
- **Tool:** Claude manual audit (Loop-1 bug scan)
- **File:** `src/intelligence/metrics.py` — `IntelligenceMetricsCalculator.compute_metrics()`
- **Status:** Applied
- **Summary:** `np.clip(np.log(whale_ratio + 0.01) / np.log(3), -1, 1)` was computed and its
  result discarded (not assigned); the function returned the raw, unbounded `whale_ratio`
  instead (e.g. `ratio=50` propagated as `50` rather than clamping to `1.0`).
- **Fix:** Assigned the clipped/normalized value to `whale_ratio` and used it as
  `whale_buy_sell_ratio`. Updated the field docstring and the neutral-fallback default
  (`1.0` raw → `0.0` normalized).
- **Verified:** `uv run pytest tests/test_intelligence_metrics.py -q` → 24 passed (1 new
  regression test added).

### [VF-030] — 2026-07-11 — src/intelligence/probabilistic.py asymmetric abs() in extremity score
- **Severity:** MEDIUM (risk — overconfident credible intervals on crisis signals)
- **Tool:** Claude manual audit (Loop-1 bug scan)
- **File:** `src/intelligence/probabilistic.py` — `BayesianExchangeStressModel._compute_credible_interval()`
- **Status:** Applied
- **Summary:** `extremity = abs(netflow)/3.0 + funding/0.15 + (basis/150.0) + abs(reserve-0.35)/0.35`
  omitted `abs()` on `funding` and `basis`. A large **negative** funding rate or basis spread
  (a real crisis signal, e.g. a short squeeze) reduced `extremity` instead of increasing it,
  which — via `n_eff = base_n_eff / (1 + extremity)` — increased `n_eff` and produced an
  artificially narrower, overconfident interval for exactly the kind of unprecedented input
  this function's docstring says must widen it.
- **Fix:** Wrapped `funding` and `basis` in `abs()`, matching `netflow`/`reserve`.
- **Verified:** `uv run pytest tests/test_probabilistic_engine.py -q` → 25 passed (2 new
  regression tests confirming extreme negative funding/basis widen, not narrow, the interval).

### [VF-031] — 2026-07-11 — src/models/trainer.py timeframe path-traversal into model filename
- **Severity:** MEDIUM (defense-in-depth — no confirmed live HTTP-reachable path)
- **Tool:** Claude manual audit (Loop-1 bug scan)
- **File:** `src/models/trainer.py` — `ModelTrainer.__init__`, `save()`, `load_direction()`, `load_meta()`
- **Status:** Applied
- **Summary:** `timeframe` was interpolated directly into a model filename via
  `_DIRECTION_FILENAME.format(timeframe=timeframe)` with no sanitization (unlike `symbol`,
  which gets `.replace("/", "_")`). A `timeframe` value containing `../` could traverse outside
  `model_dir`.
- **Fix:** Added `_validate_timeframe()` — rejects empty strings and anything outside
  `[A-Za-z0-9_-]+`, called from `__init__`, `load_direction`, and `load_meta`. Deliberately
  does NOT restrict to the 3-value `Timeframe` enum, since `timeframe` is used as a loosely
  typed free-form string elsewhere in the codebase (e.g. `"1h"` in existing test fixtures).
- **Verified:** `uv run pytest tests/test_model_trainer_coverage.py -q` → 80 passed (4 new
  regression tests, including one confirming non-enum strings like `"1h"` remain valid).

### [VF-032] — 2026-07-11 — src/api/main.py /model-metrics missing timeframe validation + raw exception leak
- **Severity:** LOW (info disclosure / input validation gap; auth already required)
- **Tool:** Claude manual audit (Loop-1 bug scan)
- **File:** `src/api/main.py` — `model_metrics()`, `get_order_status()`
- **Status:** Applied
- **Summary:** (a) `/model-metrics?timeframe=` passed an unvalidated string straight to
  `storage.latest_model_metrics`/`live_gate_passes`, unlike `/regime/{timeframe}` which
  validates against the `Timeframe` enum. (b) `/orders/{order_id}/status` returned
  `{"error": str(exc)}` on any lookup failure, echoing raw exception text (potential internal
  state leak) to an authenticated-but-untrusted caller.
- **Fix:** (a) Added the same `Timeframe` enum validation as `/regime/{timeframe}`, returning
  400 on an invalid value. (b) Logs the real exception server-side at `warning` and returns a
  generic `{"error": "Failed to look up order status."}` to the client.
- **Verified:** `uv run pytest tests/test_api_main_coverage.py -q` → 71 passed (3 new regression
  tests).

### [VF-033] — 2026-07-11 — Silent exception swallowing (multiple, LOW severity, batched)
- **Severity:** LOW
- **Tool:** Claude manual audit (Loop-1 bug scan)
- **Status:** Applied
- **Summary:** Several `except Exception: pass`/debug-only-log patterns hid real bugs
  indefinitely with no operator-visible signal:
  - `src/engine/orchestrator.py` — Prometheus metrics push failure was a bare `pass`.
  - `src/engine/signal_engine.py` — OFI/intel-aggregator fetch failures logged at `debug`
    (invisible by default), silently degrading order-flow signal quality and failing the
    exchange-stress/whale gates open.
  - `src/diagnostics/trade_auditor.py` — `except statistics.StatisticsError: pass` in the
    anomaly-scan checks that exist specifically to detect a degenerate/constant model feed.
  - `src/diagnostics/runtime_monitor.py` — `_rss_mb()` swallowed all exceptions and returned
    `0.0`, indistinguishable from "healthy" to the memory-leak probe consuming it.
- **Fix:** Each now logs at `warning` (orchestrator, signal_engine) or `debug` with a
  descriptive event name (trade_auditor, runtime_monitor) instead of silently discarding the
  exception. No behavior change to the trade path — logging only.
- **Verified:** `uv run pytest tests/ -q` → 2150 passed, 1 skipped, coverage 96.17%.

### [VF-034] — 2026-07-11 — frontend/src/App.jsx overlapping poll requests + missing client-side threshold bounds
- **Severity:** LOW
- **Tool:** Claude manual audit (Loop-1 bug scan)
- **File:** `frontend/src/App.jsx`
- **Status:** Applied
- **Summary:** (a) `fetchData`/`fetchRiskControls` `setInterval` polls had no in-flight guard —
  a slow response could let a second poll fire before the first resolved, letting an
  out-of-order response overwrite newer state. (b) `saveThreshold()` sent `parseFloat()` output
  directly to `/risk-controls` with no client-side range check (the `<input min/max>` HTML
  attributes are advisory only); the backend's Pydantic bounds are the real enforcement, but a
  bad value round-tripped as an unexplained 422 with no inline feedback.
- **Fix:** (a) Added a per-effect `inFlight` boolean guard around both poll functions. (b) Added
  `THRESHOLD_BOUNDS` mirroring `SetRiskControlsRequest`'s Pydantic `ge`/`le` values in
  `src/api/main.py`, with an inline `alert()` on violation before the fetch is attempted.
- **Verified:** Manual review (no frontend test harness in this repo); backend test suite
  unaffected, full suite green.
