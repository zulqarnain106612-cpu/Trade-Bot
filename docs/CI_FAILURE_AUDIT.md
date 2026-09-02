# Audit: the 84 failed CI runs on `main`

`main` accumulated 84 `ci.yml` push runs that ended in `failure` (plus 77
`cancelled` and 38 `success`, 199 total). Those runs are merged history: their
logs are immutable and their pull requests are closed, so none of them can be
re-run or turned green. This document records what each one actually failed on
and whether that failure left a defect that is **still present** in `main`.

## Method

The verdicts are not read off commit titles. For every one of the 84 runs the
audit pulled, from the Actions API, the failing **job**, the failing **step**,
and the run's **check-run failure annotations** — the same text GitHub renders
on the run page. (Raw log archives are served from Azure blob storage, which the
audit environment cannot reach; annotations carry the actual compiler/linter
message, so no verdict rests on a guess.) The recovered messages name concrete
defects — `PIE794` on `src/config.py`, `F811` on `src/tuning/backtest_harness.py`,
`RUF100`/`UP017` on `src/api/main.py`, `B017` on a test — and each was then
checked against the current tree.

Every failure mode in the 84 is one of the following gates, and every one of
those gates was re-run on `2deefab` (current `main`):

| Gate | Result on `2deefab` |
|---|---|
| `ruff check .` | All checks passed |
| `ruff format --check .` | 581 files already formatted |
| `pytest -q` | 6154 passed, 92 skipped (the 92 are the TimescaleDB suite — see below) |
| `python scripts/check_coverage_floors.py` | All 248 measured files clear their floor |
| `scripts/arch_gate.sh` (`ARCH_PYTHON=python`) | RESULT: PASS (98 checks, 0 HIGH/CRITICAL) |
| `npm ci && npm run build` (frontend) | Build succeeded |

## Classification

- **A — already fixed forward** (31 runs). A real defect existed; a later commit
  repaired it, and the gate that caught it is clean today.
- **B — environmental / CI-infra only** (53 runs). No product defect ever
  existed: the job either never started, or died in provisioning, or ran a CI
  step that has since been deleted along with the file it checked.
- **C — still live in current `main`**: **0 runs.**

### Notable category-A defects and their fixing commits

| Defect | Runs | Fixed by |
|---|---|---|
| `src/tuning/backtest_harness.py` `F811` — `ensemble_blend_samples_from_trades` and `run_ensemble_blend_backtest` each defined twice, the second silently shadowing the first | 848–870 (12 runs) | `a5bdd9c` *fix(tuning): drop the superseded ensemble-blend backtest implementation* |
| `src/config.py:1073` `PIE794` — `Settings.strategy` declared twice, so the field an operator sets was not the field the selector read | 844 | `7353619` *fix(config): remove the duplicate Settings.strategy declaration* |
| `src/api/main.py` `RUF100` / `UP017` | 20 | superseded by `b766d0c`; `ruff check .` clean |
| `tests/test_coverage_boost2.py:203` `B017` blind-exception assert | 107 | file no longer carries the rule violation; `ruff check .` clean |

### Dominant category-B causes

| Cause | Runs |
|---|---|
| GitHub billing / spending-limit — the job was never started, so no code ever executed | 213, 215, 419, 486–490, 494–496, 499, 502–510, 521, 530, 534, 537, 538, 540, 541 (26 runs) |
| `Lint - ruff check` exit 127 — ruff absent from the runner | 11–19 (9 runs) |
| Dependency / runner provisioning failure (`Install dependencies`, `Set up Python`, workflow bootstrap) | 1–6, 23, 34, 591 (9 runs) |
| `Requirements drift check` — gated a `requirements.lock` that no longer exists | 600, 683, 693, 727 (4 runs) |
| `Secret scanning — fail on unaudited baseline entries` exit 127 — `detect-secrets` absent; step and baseline since deleted | 35, 37, 38, 39 (4 runs) |
| `Run tests with coverage` exit 127 — pytest absent from the runner | 7 (1 run) |

Several of these runs failed in more than one job at once; the table above
attributes each run to the cause that made it red, and the full table below
records every failing job and step.

## Conclusion

**Category C is empty.** None of the 84 failures left a defect that survives in
`main`. Two of them (`F811` in the tuning backtest harness, `PIE794` in
`Settings`) were genuine correctness bugs with real blast radius — a shadowed
backtest implementation and a config field the strategy selector could not see —
and both were repaired by the commits cited above. Everything else was either
repaired by a later commit on the same gate, or was never a product defect at
all.

## Two environmental issues found while auditing — both fixed in this branch

Neither is traceable to any of the 84, but both were making CI report a
confidence it had not earned.

1. **MongoDB Atlas was unreachable from CI** — an SSL handshake failure against
   `ac-mg2dtc0-shard-00-*.2aaxv95.mongodb.net`, which read as a network or
   allowlist problem but was not one. `rag_mongo/db.py` and `kg/db.py` built
   their `MongoClient` with no `tlsCAFile`, so pymongo verified Atlas's chain
   against whatever CA store the runner image happened to ship. Both now pin
   `certifi.where()` (and `certifi` is an explicit requirement), which is
   ignored for a non-TLS URI and so leaves local `mongodb://` runs alone.
   Advisory reviews had been running without their RAG / knowledge-graph
   grounding for as long as this was broken.

2. **The TimescaleDB integration suite ran nowhere.** All 92 of `pytest`'s skips
   were `tests/test_timescale_storage.py`, which needs a live TimescaleDB;
   `ci.yml` had lost the service container an older workflow provided, so the
   storage backend was green by absence rather than green. `ci.yml`'s Python job
   now runs `timescale/timescaledb:2.17.2-pg17` as a service on host port 5433
   with a `pg_isready` health gate, and sets `STORAGE_TIMESCALE_DSN` to match
   the DSN the suite already defaults to.

## Full table

| Run | SHA | Commit | Class | Evidence & verdict |
|---:|---|---|:---:|---|
| 974 | `98abb320` | fix(config): remove the duplicate Settings.strategy declaration (#164) | **A** | Per-file floor breach; check_coverage_floors.py clears all 248 files on 2deefab. |
| 970 | `0c207d8b` | ci: per-file coverage floors, and unblock the advisory review (#162) | **A** | Per-file floor breach; check_coverage_floors.py clears all 248 files on 2deefab. |
| 938 | `1ae9abb3` | Delete setup_all_v3.sh | **A** | Test failure; `pytest -q` is 6154 passed / 92 skipped on 2deefab. |
| 935 | `3de1beda` | test: cover advisory-scalar aggregation in evaluate_all_gates + Drawd… | **A** | Test failure; `pytest -q` is 6154 passed / 92 skipped on 2deefab. |
| 921 | `dbf66732` | test: bring src/engines/e01_statistical.py to 100% | **A** | Test failure; `pytest -q` is 6154 passed / 92 skipped on 2deefab. |
| 917 | `72aaef3d` | test: bring src/upgrade/registry.py, src/data/feeds.py, src/features/… | **A** | Test failure; `pytest -q` is 6154 passed / 92 skipped on 2deefab. |
| 916 | `6b54a3bb` | test: bring the six new packages (common, kg, orchestrator, rag_mongo, | **A** | Test failure; `pytest -q` is 6154 passed / 92 skipped on 2deefab. |
| 915 | `053f243d` | style: apply ruff format to unblock the CI format-check gate | **A** | Test failure; `pytest -q` is 6154 passed / 92 skipped on 2deefab. |
| 914 | `f11ad0cb` | fix(deps): restore full requirements.txt; add repo-wide 99% coverage … | **A** | ruff-format drift; `ruff format --check .` reports 581/581 formatted on 2deefab. |
| 912 | `931cb62d` | fix(ci): allow claude-review to actually post its findings to the PR | **A** | ruff-format drift; `ruff format --check .` reports 581/581 formatted on 2deefab. |
| 910 | `7606500d` | fix(ci): raise claude-review max-turns headroom to 40 | **A** | ruff-format drift; `ruff format --check .` reports 581/581 formatted on 2deefab. |
| 908 | `75b3a70d` | fix(ci): grant claude-review the read-only tools it needs | **A** | ruff-format drift; `ruff format --check .` reports 581/581 formatted on 2deefab. |
| 906 | `1c4d5ea2` | fix(ci): invoke review/build_context.py as a module | **A** | ruff-format drift; `ruff format --check .` reports 581/581 formatted on 2deefab. |
| 904 | `ec95dc7b` | Add RAG + PTC + orchestrator + knowledge-graph + cloud-review (Claude… | **A** | ruff-format drift; `ruff format --check .` reports 581/581 formatted on 2deefab. |
| 870 | `82548e96` | Delete CLAUDE_ACTION_PATTERNS.md | **A** | Lint finding; `ruff check .` is clean on 2deefab. |
| 869 | `feb442f0` | Build complete frontend monitoring dashboard with resizable panels (#… | **A** | Lint finding; `ruff check .` is clean on 2deefab. |
| 866 | `ad9f6667` | Delete .claude/skills/crypto-architect/scripts/validate_arch.py | **A** | Lint finding; `ruff check .` is clean on 2deefab. |
| 864 | `4df96c7f` | Delete .claude/skills/crypto-architect/SKILL.md | **A** | Lint finding; `ruff check .` is clean on 2deefab. |
| 859 | `f2887765` | Delete ROADMAP_V2_PLAN.md | **A** | Lint finding; `ruff check .` is clean on 2deefab. |
| 855 | `6dcf7e80` | Delete DECISION_LOG.md | **A** | Lint finding; `ruff check .` is clean on 2deefab. |
| 853 | `74fb8f13` | Delete armor directory | **A** | Lint finding; `ruff check .` is clean on 2deefab. |
| 852 | `b0532ae3` | Delete .sentinel directory | **A** | Lint finding; `ruff check .` is clean on 2deefab. |
| 850 | `8be5ba07` | Update settings.json | **A** | Lint finding; `ruff check .` is clean on 2deefab. |
| 849 | `818fbe1e` | Delete .claude/hooks/pre-tool.sh | **A** | Lint finding; `ruff check .` is clean on 2deefab. |
| 848 | `5b331072` | Delete .circleci directory | **A** | Lint finding; `ruff check .` is clean on 2deefab. |
| 844 | `86f0be46` | feat(execution): idempotency keys on every order path, closing LAW3 C… | **A** | Lint finding; `ruff check .` is clean on 2deefab. |
| 815 | `e2bbaec8` | feat(risk,strategies,diagnostics): vol-target sizer, regime selector,… | **A** | Test failure; `pytest -q` is 6154 passed / 92 skipped on 2deefab. |
| 727 | `291a4b60` | ci: remove every unattended commit, push and merge from CI (#84) | **B** | Removed CI step (requirements.lock drift); the lockfile and the step no longer exist. |
| 693 | `8eaa4b53` | Refactor VSCode settings for formatters and tools | **B** | Removed CI step (requirements.lock drift); the lockfile and the step no longer exist. |
| 683 | `1c8da1fb` | Delete Vulner-Fix.md | **B** | Removed CI step (requirements.lock drift); the lockfile and the step no longer exist. |
| 600 | `0b1d253e` | ci: give the TimescaleDB service an ephemeral host port (#82) | **B** | Removed CI step (requirements.lock drift); the lockfile and the step no longer exist. |
| 591 | `aa41cd56` | ci: run all workflows on the self-hosted runner (#81) | **B** | Dependency/runner provisioning failure; no product code executed. |
| 541 | `cb6f3c0b` | chore(deps): bump sharp from 0.33.5 to 0.35.3 in /frontend (#72) | **B** | GitHub billing/spending limit — jobs never started; no code was ever executed. |
| 540 | `c4f36569` | chore(deps): bump eslint-config-prettier in /frontend (#75) | **B** | GitHub billing/spending limit — jobs never started; no code was ever executed. |
| 538 | `d59b4d1a` | chore(deps): bump electron from 43.2.0 to 43.3.0 in /frontend (#74) | **B** | GitHub billing/spending limit — jobs never started; no code was ever executed. |
| 537 | `7ffe566b` | chore(deps): update stable-baselines3 requirement (#71) | **B** | GitHub billing/spending limit — jobs never started; no code was ever executed. |
| 534 | `6d157c5e` | chore(deps): update pytest requirement from >=8 to >=9.1.1 (#69) | **B** | GitHub billing/spending limit — jobs never started; no code was ever executed. |
| 530 | `07d8b06e` | fix(risk,strategies,engine,tuning): seven controls that silently did … | **B** | GitHub billing/spending limit — jobs never started; no code was ever executed. |
| 521 | `2254fb49` | docs(env): add combined .env.example and .env.example.intelligence | **B** | GitHub billing/spending limit — jobs never started; no code was ever executed. |
| 510 | `1a2c3e90` | chore(deps): bump ruff from 0.15.21 to 0.16.1 in the dev-dependencies… | **B** | GitHub billing/spending limit — jobs never started; no code was ever executed. |
| 509 | `d3c4d02c` | chore(deps): update websockets requirement from <14.0,>=12.0 to >=17.… | **B** | GitHub billing/spending limit — jobs never started; no code was ever executed. |
| 506 | `8de33ed3` | chore(deps): update joblib requirement from <2.0,>=1.4 to >=1.5.3,<2.… | **B** | GitHub billing/spending limit — jobs never started; no code was ever executed. |
| 505 | `d86e4ae6` | chore(deps): update uvicorn requirement from <1.0,>=0.51.0 to >=0.52.… | **B** | GitHub billing/spending limit — jobs never started; no code was ever executed. |
| 504 | `df9ff2b1` | chore(deps): update pytest-cov requirement from >=5 to >=7.1.0 (#53) | **B** | GitHub billing/spending limit — jobs never started; no code was ever executed. |
| 503 | `5d4f6514` | chore(deps): bump the vite-react group across 1 directory with 2 upda… | **B** | GitHub billing/spending limit — jobs never started; no code was ever executed. |
| 502 | `09d25159` | feat: give the strategy portfolio a runtime — poll it, feed it, and p… | **B** | GitHub billing/spending limit — jobs never started; no code was ever executed. |
| 499 | `2caba8f4` | chore(deps): bump tailwindcss from 3.4.19 to 4.3.3 in /frontend (#55) | **B** | GitHub billing/spending limit — jobs never started; no code was ever executed. |
| 496 | `a08fd537` | chore(deps): bump electron from 43.1.1 to 43.2.0 in /frontend (#56) | **B** | GitHub billing/spending limit — jobs never started; no code was ever executed. |
| 495 | `cc701e61` | chore(deps): bump prettier from 3.9.5 to 3.9.6 in /frontend (#57) | **B** | GitHub billing/spending limit — jobs never started; no code was ever executed. |
| 494 | `0cc36f26` | chore(deps): bump eslint from 8.57.1 to 10.8.0 in /frontend (#58) | **B** | GitHub billing/spending limit — jobs never started; no code was ever executed. |
| 490 | `49f7e990` | chore(actions): bump actions/checkout from 4 to 7 (#59) | **B** | GitHub billing/spending limit — jobs never started; no code was ever executed. |
| 489 | `52c07d9c` | chore(actions): bump github/codeql-action from 3 to 4 (#60) | **B** | GitHub billing/spending limit — jobs never started; no code was ever executed. |
| 488 | `88718348` | chore(actions): bump softprops/action-gh-release from 2 to 3 (#61) | **B** | GitHub billing/spending limit — jobs never started; no code was ever executed. |
| 487 | `fc3d9403` | chore(actions): bump dependabot/fetch-metadata from 2 to 3 (#62) | **B** | GitHub billing/spending limit — jobs never started; no code was ever executed. |
| 486 | `de774074` | fix(deps): reconcile requirements.txt with requirements.in, and catch… | **B** | GitHub billing/spending limit — jobs never started; no code was ever executed. |
| 419 | `51838353` | feat(intel): crypto intelligence v6 — horizons, ECC, mempool, DuckDB … | **B** | GitHub billing/spending limit — jobs never started; no code was ever executed. |
| 281 | `d4c94907` | feat: wire five tested-but-unreachable modules into the running syste… | **A** | Test failure; `pytest -q` is 6154 passed / 92 skipped on 2deefab. |
| 215 | `8a5ca1a9` | feat(api): enforce the RBAC role table on every mutating endpoint (#37 | **B** | GitHub billing/spending limit — jobs never started; no code was ever executed. |
| 213 | `b9fd3dac` | feat(risk,engine): give the v7 macro exposure overlay a producer and … | **B** | GitHub billing/spending limit — jobs never started; no code was ever executed. |
| 107 | `b9d959e6` | chore(deps): update hmmlearn requirement from <1.0,>=0.3 to >=0.3.3,<… | **A** | Lint finding; `ruff check .` is clean on 2deefab. |
| 42 | `527b4443` | fix(ci): install requirements-dev.txt unconditionally in backend job | **A** | Test failure; `pytest -q` is 6154 passed / 92 skipped on 2deefab. |
| 39 | `9bec71da` | chore: enable VS Code terminal integration, add primer marker | **B** | detect-secrets not installed (exit 127); step and baseline removed from CI. |
| 38 | `ac02e43f` | fix(ci): fix cross-job variable scoping bug in mutation-testing workfl | **B** | detect-secrets not installed (exit 127); step and baseline removed from CI. |
| 37 | `f5cd9392` | ci(mutation): add .github/mutation-trigger.json to allow push-trigger… | **B** | detect-secrets not installed (exit 127); step and baseline removed from CI. |
| 35 | `ce004d65` | fix(deps): regenerate requirements.lock — was unusable on Python 3.11 | **B** | detect-secrets not installed (exit 127); step and baseline removed from CI. |
| 34 | `4e87d2c0` | feat(ci): add sharded mutation-testing workflow (manual dispatch) | **B** | Dependency/runner provisioning failure; no product code executed. |
| 23 | `94e0872d` | feat(self-tuning): add Bayesian proposer for parameter search | **B** | Dependency/runner provisioning failure; no product code executed. |
| 20 | `4de6f8c5` | feat: enhance WebSocket client management, improve rate limiting, and… | **A** | Lint finding; `ruff check .` is clean on 2deefab. |
| 19 | `cb2eb4cc` | feat(orchestrator): implement dedicated training executor for CPU-bou… | **B** | ruff not installed on the runner (exit 127) — tooling, not code. |
| 18 | `dacaec01` | feat(ci): implement hashed dependency installation and secret scannin… | **B** | ruff not installed on the runner (exit 127) — tooling, not code. |
| 17 | `77be694b` | feat(middleware): Enhance CORS validation to reject unsafe origins an… | **B** | ruff not installed on the runner (exit 127) — tooling, not code. |
| 16 | `723e2913` | refactor: update typing annotations for query parameters in trades an… | **B** | ruff not installed on the runner (exit 127) — tooling, not code. |
| 15 | `0bef1bb8` | security: add detect-secrets baseline for pre-commit secret scanning | **B** | ruff not installed on the runner (exit 127) — tooling, not code. |
| 14 | `31d61a51` | chore: remove push helper script | **B** | ruff not installed on the runner (exit 127) — tooling, not code. |
| 13 | `bfebd524` | chore: remove temporary commit helper script | **B** | ruff not installed on the runner (exit 127) — tooling, not code. |
| 12 | `17228c88` | feat(api): implement API key validation and CORS middleware; enhance … | **B** | ruff not installed on the runner (exit 127) — tooling, not code. |
| 11 | `3d36105b` | Merge branch 'main' of https://github.com/zulqarnain106612-cpu/Trade-B | **B** | ruff not installed on the runner (exit 127) — tooling, not code. |
| 7 | `13ba62c3` | fix(ci): remove non-ASCII chars from all workflow files [skip-fix] | **B** | pytest not installed on the runner (exit 127) — tooling, not code. |
| 6 | `69360aec` | fix(ci): fix security.yml heredoc YAML error + format src/api/main.py… | **B** | Job never reached a step (runner/billing/infra). |
| 5 | `69360aec` | fix(ci): fix security.yml heredoc YAML error + format src/api/main.py… | **B** | Job never reached a step (runner/billing/infra). |
| 4 | `69360aec` | fix(ci): fix security.yml heredoc YAML error + format src/api/main.py… | **B** | Job never reached a step (runner/billing/infra). |
| 3 | `39aa6ab5` | perf(ci): fix security audit slow install ΓÇö use pip dry-run resolve… | **B** | Job never reached a step (runner/billing/infra). |
| 2 | `92542536` | ci: add GitHub Actions CI/CD workflows (ci, release, security) | **B** | Job never reached a step (runner/billing/infra). |
| 1 | `92542536` | ci: add GitHub Actions CI/CD workflows (ci, release, security) | **B** | Job never reached a step (runner/billing/infra). |
