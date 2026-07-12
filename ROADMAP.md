# Roadmap

Status snapshot as of this writing (branch `claude/local-project-next-step-iy0kp3`):

- Tests: 170/170 passing
- mypy: clean (25 source files)
- Ruff: clean except 19 cosmetic ambiguous-unicode findings (en-dashes, ×,
  in docstrings/comments — deliberately left, see Phase 4)
- **Test coverage: 22% total, gate requires 60% — CI coverage check has been
  failing on `main` for months, independent of any lint issue.**
- Frontend: lint/build clean.

This file tracks what's fixed, what's known-broken, and what's next, in
priority order. Each phase is independently shippable.

---

## Phase 0 — Done (this session)

- [x] Fixed 5 stale test assertions that didn't match deliberate prior
      production fixes (meta-label timing, Kelly stats window, risk gate
      ordering).
- [x] Removed ~20 tracked CI-debug scratch files from repo root; untracked
      `src/tradebot.egg-info/` (build artifact); moved `extract_resolved.py`
      into `scripts/`.
- [x] Fixed 100 ruff findings (dead imports, deprecated aliases, missing
      `zip(strict=True)`, a mid-file import).
- [x] Fixed `.github/workflows/release.yml` — malformed YAML caused it to
      ignore its `branches: ["main"]` filter and run (and fail) on every
      push to every branch.
- [x] **Safety fix**: scalping (1m) and swing (4h) timeframes had no code
      enforcing the documented paper-only invariant; with
      `TRADING_MODE=live` they could place real orders through the shared
      executor. `Orchestrator` now gives every non-primary timeframe its
      own dedicated `PaperExecutor`.
- [x] **Safety fix**: `LiveExecutor._place_and_record` could silently drop
      an already-filled exchange order from internal bookkeeping (no
      position record, no stop-loss, no trade row) if a post-fill cash
      check failed, while restoring cash as if the trade never happened.
      Now every filled order is always recorded; a negative cash
      reconciliation is logged critical instead of discarding the trade.
- [x] Added tests for previously-untested security modules
      (`api/auth.py`, `api/middleware.py`) and the executor-routing fix
      (`test_orchestrator.py`).

---

## Phase 1 — Remaining safety-critical items (do first, before any live capital)

These were identified but deliberately **not** auto-fixed because they
require a product/risk-policy decision, not just a code change.

1. **Cross-timeframe position-size race.** Each timeframe loop
   independently evaluates `check_position_size` against total capital
   before calling `submit_signal`. Two timeframes closing bars near-
   simultaneously for the same symbol can each independently pass the
   5%-of-capital gate and both submit, producing up to 2x the intended
   aggregate exposure on one symbol. `_trade_semaphore` in `LiveExecutor`
   only serializes the cash debit, not the gate evaluation itself.
   - Decision needed: should the 5% cap be per-trade (current behavior)
     or aggregate-per-symbol across all timeframes? Once decided, fix is
     either (a) move the position-size gate to re-check against
     *currently open* aggregate notional inside the executor's locked
     section, or (b) accept per-trade semantics as intentional and
     document it explicitly in the README risk-gates table.
   - File: `src/engine/signal_engine.py` (gate evaluation), `src/engine/orchestrator.py::_tick`.

2. **MANUAL-mode approval has no re-validation at fill time.** Kelly
   sizing and risk gates are evaluated once at signal time; approval can
   arrive arbitrarily later (`timeout_s=None`), and the order fires with
   the original (stale) quantity/notional without re-checking gates
   against current capital/price/drawdown.
   - Decision needed: is human review the intended safety check (current
     assumption), or should gates be re-evaluated automatically at
     approval time? If the latter: re-run `evaluate_all_gates` and
     re-size via `compute_position_size` immediately before
     `_submit_signal_auto` in `_submit_signal_with_approval`.
   - File: `src/execution/live.py::_submit_signal_with_approval`.

3. **CI "Auto-Fix and Re-trigger" job is broken** (fails on every run,
   independent of code): its checkout step needs a `GH_TOKEN` repo secret
   that isn't configured. This requires a repo-admin action (Settings →
   Secrets and variables → Actions), not a code change.
   - File: `.github/workflows/ci.yml` (job: `auto-fix`).

---

## Phase 2 — Make the CI coverage gate honest

The gate (`--cov-fail-under=60` in `pyproject.toml`) has been failing
since before this session on every push to `main`. Two honest paths,
pick one explicitly (don't leave it silently red):

**Option A (recommended): ratchet the gate up as coverage is earned.**
Set `--cov-fail-under` to match current real coverage (22%) now, so CI
goes green, then raise it in each PR that adds real tests until it
reaches 60%+. This makes the gate meaningful again instead of
permanently-ignored red noise.

**Option B: write the tests first, then re-enable at 60%.** Higher
integrity, slower — CI stays red until Phase 3 below is substantially
done.

Do not lower the number without also picking a plan to raise it back —
a silently-lowered coverage gate is worse than an honestly-failing one.

---

## Phase 3 — Close the test-coverage gap (the real work)

Ordered by risk × effort. Estimates assume `unittest.mock`/`pytest-asyncio`,
no new test infra.

| Module | Stmts | Coverage | Effort | Why it matters |
|---|---|---|---|---|
| `src/risk/gates.py` | 137 | 96% | — | already solid |
| `src/risk/kelly.py` | 101 | 96% | — | already solid |
| `src/features/pipeline.py` | 203 | 94% | — | already solid |
| `src/config.py` | 187 | 93% | — | already solid |
| `src/execution/live.py` | 351 | 0% | High | real-money order placement — highest risk, needs ccxt mocking |
| `src/execution/paper.py` | 264 | 0% | Medium | shares logic with live.py; mock storage only |
| `src/engine/orchestrator.py` | 219 | 0% | Medium-High | now has safety-critical routing logic (this session's fix) |
| `src/engine/signal_engine.py` | 115 | 0% | Medium | gate evaluation + Kelly sizing entry point |
| `src/data/storage.py` | 311 | 0% | Medium | SQLite I/O, can use in-memory DB, no network mocking needed |
| `src/data/fetcher.py` | 230 | 0% | Medium-High | ccxt network calls, needs mocking |
| `src/models/trainer.py` | 272 | 0% | High | XGBoost training, needs synthetic data fixtures |
| `src/regime/detector.py` | 192 | 0% | Medium-High | GaussianHMM fit, needs synthetic regime data |
| `src/api/main.py` | 243 | 0% | Medium | FastAPI TestClient, straightforward once executors are mockable |

Suggested order: `storage.py` (easiest, no exchange mocking) →
`signal_engine.py` → `paper.py` → `main.py` → `live.py` → `fetcher.py` →
`orchestrator.py` → `detector.py` → `trainer.py`.

Each module should land as its own PR/commit — do not attempt this in one
giant change.

---

## Phase 4 — Low-priority cleanup (optional, cosmetic)

- 19 remaining ruff findings are ambiguous-unicode characters (en-dash
  `–`, multiplication sign `×`) inside docstrings/comments — readable and
  intentional, left as-is. Fix only if the team wants strict ASCII.
- `UP042`: 4 classes (`TradingMode`, `ExecutionMode`, `Timeframe`,
  `GateStatus`) use `class X(str, Enum)` instead of `enum.StrEnum`. Ruff
  flags this, but migrating changes `__str__`/repr behavior in ways that
  could affect logging output or API JSON serialization — verify all
  call sites before switching, don't do it as a blind autofix.

---

## How to pick up where this left off

1. Read this file top to bottom before starting new work — it reflects
   the actual state of the repo, not assumptions.
2. Phase 1 items block any real `TRADING_MODE=live` usage — resolve
   those first regardless of what else is being worked on.
3. Update the checkboxes/tables here as items land, in the same PR that
   does the work, so this file never drifts from reality.
