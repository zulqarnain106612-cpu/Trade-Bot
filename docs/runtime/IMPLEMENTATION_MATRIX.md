# Universal Runtime Platform -- Phase 0 implementation matrix

Recorded at baseline `19a86d2ed3bce974f7e824b68887637f80a6bb21` (`main`), task
`TB-RUN-20261007-0001`. Every row cites the code it was decided from; "existing"
means it was read in that commit, not assumed.

## Baseline

| Fact | Value |
|---|---|
| Repository root | the clone's top level (single worktree, `main`) |
| Clone | shallow; 211 remote branches; no tags |
| Active Git operation | none (no merge / rebase / cherry-pick state) |
| Python | 3.11 in CI (`.python-version`), 3.13 locally |
| Local checks | test, lint, type, frontend and architecture runners are refused by `config/command_policy.json`; CI decides (`scripts/local_checks.py` re-runs only checks CI reported failing) |
| Coverage | branch coverage, `fail_under = 99` overall, 90 % per file (`scripts/check_coverage_floors.py`) |
| Test cost | process-spawn and sleep budgets are a ratchet (`tests/test_suite_speed_budget.py`) |

## Existing safety mechanisms that must survive unchanged

* Observation boundary: `.claude/hooks/observation_gate.py`, `observation_failure.py`.
* Command policy: destructive-command marker and secret-echo refusal in
  `.claude/hooks/pre_tool_use.py` + `common/command_schema.py`.
* GOV-036: no scheduled or unattended Claude (`.claude/settings.json` deny list).
* Trading: risk gates (`src/risk/gates.py`), strategy kill switch
  (`src/risk/strategy_kill_switch.py`), execution FSM and idempotency
  (`src/execution/`), approval queue, execution mode, promotion gauntlet
  (`src/tuning/promotion_gauntlet.py`), post-promotion watchdog
  (`src/tuning/watchdog.py`), model shadow evaluation
  (`src/models/model_registry.py`).
* Storage migration discipline (Gap-012) and SQLite/Timescale migration parity
  (`scripts/check_static_invariants.py`).
* Event bus law (INV-032): `publish` never blocks, never raises, never awaits.

## Matrix

| Decision | Item | Basis |
|---|---|---|
| KEEP | observation hooks, command policy, GOV-036 deny list | repository-enforced constraints |
| KEEP | risk gates, kill switch, execution FSM, approval queue, execution mode | trading safety; the runtime platform gets no path to them |
| KEEP | `StrategyRegistry`, `models.ModelRegistry`, `tuning.ParameterRegistry`, `upgrade.ModelRegistry` | live consumers (`src/engine/orchestrator.py`, `src/api/main.py`, bootstrap) |
| EXTEND | `.claude/hooks/pre_tool_use.py` | guarded Git operations need a task authorization while a task is active |
| EXTEND | `.claude/settings.json` | Stop, PreCompact, SessionStart recovery and PostToolUse(Bash) hooks |
| EXTEND | `src/eventbus` | event envelope and correlation context (foundation layer, so every producer can use it) |
| EXTEND | `src/data/storage.py`, `src/data/timescale_storage.py` | migration v9: runtime desired state, both backends |
| EXTEND | `src/api/main.py` | runtime read and mutation routes through the change manager; no second API |
| EXTEND | `frontend/src` | one runtime panel in the existing panel system |
| EXTEND | `config/architecture_layers.json` | `agent_control` -> foundation, `runtime` -> orchestration |
| CONNECT | strategy / model / tuning / upgrade registries, engine orchestrator, orchestrator asyncio tasks, event bus | adapters into one runtime registry; no rewrite |
| CONNECT | promotion gauntlet, watchdog, shadow evaluation | gates of the adaptive lifecycle |
| REFACTOR | none | no existing module had to change shape |
| ADD | `src/agent_control`, `scripts/agent_control.py`, lifecycle hooks | durable task state, completion gate, recovery |
| ADD | `src/runtime` | contracts, registry, supervisors, desired state, dependencies, change manager, adaptive lifecycle |
| REJECT | a second control API or persistence mechanism | precedence: existing mechanism + smallest change |
| REJECT | live code patching (`exec`, monkeypatching, function replacement) | no reload path executes code |
| REJECT | pause/resume controllers the subsystem does not support (trading loop, engines) | capabilities report what exists; nothing is faked |
| REJECT | a stored copy of dependency edges | edges are declared by component specs at startup; a stored copy would drift |
| REJECT | running the test suite, linters or `qe_gate.py` locally; scheduling CI re-checks | `config/command_policy.json`, GOV-036 |

## Dependency order

```
P0 -> P1 (task state) -> P2 (git safety, branch audit) -> P3 (completion, Stop)
   -> P4 (PreCompact, SessionStart)
P5 (contracts) -> P6 (registry, adapters) -> P7 (supervisors)
   -> P8 (desired state, reconcile) -> P9 (dependencies) -> P10 (change manager)
   -> P11 (envelope, trace) -> P12 (adaptive lifecycle)
   -> P13 (API) -> P14 (UI) -> P15 (failure injection) -> P16 (docs)
   -> P17 (integration) -> P18 (final audit, delivery)
```

## Repository validation commands

| Purpose | Command | Where it runs |
|---|---|---|
| Static invariants (layers, cycles, migration parity) | `python3 scripts/check_static_invariants.py` | local + CI |
| Generated docs in sync | `python3 scripts/generate_quality_docs.py --check`, `python3 scripts/generate_math_docs.py --check` | local + CI |
| Agent-control docs match the CLI | `python3 scripts/agent_control.py docs verify` | local + CI (test) |
| Unit / integration / coverage / lint / frontend | the PR workflow `.github/workflows/ci.yml` | CI only |
