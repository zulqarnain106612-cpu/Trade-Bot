# Project Directives

## 1. Scope

These directives govern all engineering work in this repository, including:

* source code, tests, configuration, dependencies, builds/runtime
* CI/CD, architecture, security, mathematics, registries
* documentation, scripts, tooling, repository structure
* production/operational behavior
* trading behavior: signals, risk, execution, positions, orders, balances,
  market data, retries, concurrency, exchange/API interactions

They apply to every task unless a higher-priority instruction explicitly changes scope.

Claude may autonomously inspect, analyze, modify, test, validate, refactor, and audit anything required to correctly implement the user's objective.

Do **not** make unrelated changes: no speculative features, unrelated refactors, dependency upgrades, configuration/API changes, documentation rewrites, architectural changes, or unrelated trading/risk behavior changes.

A broader change is permitted when the existing implementation cannot satisfy the requirement correctly without it.

---

# 2. Precedence and Decision Authority

Apply this precedence when information or instructions conflict:

1. System/platform/safety constraints
2. Explicit user requirements and desired outcome
3. These project directives
4. Repository-enforced constraints:
   architecture, quality/security/math rules, tests, CI gates, schemas,
   generated-source rules, dependency constraints, contracts
5. Existing implementation and conventions
6. Documentation, comments, history, blame
7. General engineering knowledge
8. Model assumptions

The user's **desired outcome** is authoritative. Their description of the repository's current state is not until verified.

### WHAT vs HOW

The user controls **WHAT**: objectives, product requirements, explicit constraints, irreversible business decisions.

Claude controls **HOW**: implementation, algorithms, architecture, dependencies, refactoring, error handling, testing, validation, and technical trade-offs.

Do not ask the user to choose between ordinary technical alternatives. Resolve them through repository evidence and engineering analysis.

When sources conflict:

**IDENTIFY CONFLICT → APPLY PRECEDENCE → PRESERVE COMPATIBLE CONSTRAINTS → IMPLEMENT**

Ask one precise question only when a materially correctness-critical issue remains unresolved after investigation.

---

# 3. Evidence-First Engineering

> **Never decide from the request alone. Establish repository truth first, determine the correct solution, implement it, and verify it.**

Required model:

**CURRENT STATE → DESIRED STATE → GAP → ROOT CAUSE → SOLUTION → VALIDATION**

Default workflow:

**DISCOVER → TRACE → CLASSIFY → COMPARE → DECIDE → IMPLEMENT → TEST → AUDIT → REPORT**

## Repository discovery

Before implementation decisions, inspect the actual repository without modifying it.

Inspect as relevant:

* branch, HEAD, status, staged/unstaged/untracked changes
* relevant commits/history/blame
* source layout, modules/packages, entry/composition roots
* configuration, manifests, lockfiles, scripts
* tests, generated files, CI/CD, docs, hooks, registries
* architecture, quality, security, mathematical rules
* language/runtime/framework/package manager
* dependency versions, build/test/lint/type-check systems
* databases/storage, external APIs, execution environment

Never invent repository facts, dependencies, versions, APIs, frameworks, or architecture.

Preserve existing user work. Never reset, revert, stash, overwrite, delete, or discard it unless explicitly required.

## Existing functionality

Before creating anything, search for equivalent or related:

* implementations, functions, classes, services, interfaces, types
* endpoints/configuration/schema usage
* callers/consumers
* tests, mocks, fixtures
* documentation and registry entries
* quality/security requirements

Reuse existing correct functionality where appropriate. Do not duplicate behavior without evidence.

## Behavioral tracing

For affected functionality, establish:

**ENTRY → CALLER → FUNCTION/SERVICE → DEPENDENCIES → STATE/DATA → EXTERNAL SYSTEM → OUTPUT → ERROR HANDLING → TESTS**

Do not infer behavior from filenames, naming, or assumptions.

---

# 4. Evidence Classification

Classify important conclusions as:

* **FACT** — directly verified
* **INFERENCE** — derived from verified facts
* **UNKNOWN** — not established
* **ASSUMPTION** — unverified belief

Before implementation:

> **ASSUMPTIONS = 0**

Resolve assumptions through repository evidence, tests, configuration, history, or technical investigation.

Treat statements about the current implementation in the task description as hypotheses until verified.

---

# 5. Problem Resolution and Technical Decisions

Before implementation:

1. Establish current behavior/state.
2. Establish the actual requirement.
3. Establish the gap.
4. Identify the root cause or missing capability.
5. Identify constraints and affected boundaries.
6. Generate viable solutions proportional to complexity/risk.
7. Compare them.
8. Select the strongest evidence-supported solution.
9. Implement and validate.

Possible solutions include reusing/extending existing mechanisms, changing abstractions or boundaries, altering algorithms/data models/execution flow, using configuration, or introducing a dependency/API when justified.

Compare candidates on:

* correctness and requirement coverage
* architecture/conventions
* regression/security risk
* failure and state handling
* concurrency/performance
* complexity/maintainability/testability
* compatibility/dependency/operational impact
* migration/rollback safety
* observability/extensibility

Prefer:

**existing correct mechanism + smallest necessary change**

over:

**new mechanism + duplication**

Minimal change is not a goal when it would produce an incorrect or architecturally unsound solution.

## Decision ownership

Claude must make ordinary engineering decisions autonomously.

Do not ask:

* "Which approach do you prefer?"
* "Should I use A or B?"
* "Should I modify X or Y?"
* "Should I proceed?"

unless the decision is genuinely outside technical determination.

## User questions

Ask only when repository evidence cannot resolve a materially correctness-critical issue, such as:

* contradictory explicit requirements
* genuinely unspecified business behavior
* irreversible business/product choice
* unavailable required access/credentials
* externally incompatible requirements
* required authorization
* legal/safety/compliance/contractual decisions
* materially ambiguous desired outcome

Before asking, inspect source, config, tests, docs, history, patterns, and technical alternatives.

If uncertainty does not affect correctness, choose the safest appropriate solution and continue.

Never repeat a question already answered unless new evidence creates a genuine conflict.

## Decision record

For non-trivial decisions maintain internally:

**Problem → Current State → Candidates → Comparison → Selected Solution → Basis → Rejected Alternatives**

Mention the decision briefly in the final report only when useful.

## Invalidated plans

If new evidence invalidates the selected approach:

**STOP → RE-INSPECT → RE-ESTABLISH STATE → RECOMPARE → RESELECT → CONTINUE**

Never defend an implementation merely because work has already started.

---

# 6. Hard Stops and Correctness

Investigate instead of guessing when:

* code contradicts the task
* requested functionality already appears to exist
* requirements conflict
* interpretations materially differ
* architecture/API behavior is uncertain
* tests contradict the proposed behavior
* user changes may be overwritten
* destructive/security/trading/live-execution behavior may change
* required evidence is unavailable

A hard stop means **investigate first**, not automatically **ask the user**.

Correctness requires:

**CURRENT STATE VERIFIED

* REQUIREMENT VERIFIED
* GAP VERIFIED
* ROOT CAUSE VERIFIED
* SOLUTION SELECTED
* IMPLEMENTATION ALIGNED
* TESTS EXECUTED
* FINAL DIFF AUDITED**

Passing tests alone is insufficient if the wrong behavior was implemented.

---

# 7. Programmatic Tool Calling

First always use desktop commander mcp server and its tools, if failed then follow below instructions.

## Tool surface

`.claude/settings.local.json` allows only these eight desktop-commander tools and denies every other tool, built-in or MCP:

`read_file`, `list_directory`, `start_search`, `get_more_search_results`, `edit_block`, `write_file`, `start_process`, `read_process_output`

They are the floor — read, list, search, page-search, edit, write, run, read-output. Removing any one stalls ordinary work.

`permissions.allow` and `permissions.deny` are arrays of **strings** in permission-rule syntax: a tool name, optionally with an argument pattern such as `Bash(git *)`. Object entries are invalid and are ignored, and no key expresses a per-call line bound — rules match tool names, never argument values. The read bound lives in `config/command_policy.json`; desktop-commander's own reads are governed by its `fileReadLineLimit`.

Because `Bash` is denied, all shell execution runs through `start_process`, and `.claude/hooks/pre_tool_use.py` — which matches `Bash` only — never fires. The line bound, the destructive-command refusal and the secret-echo refusal are unenforced in that configuration; enforcement rests on `common/shell_exec.run()` being used deliberately.

`.claude/skills/programmatic-tool-calling/SKILL.md`

Use one script under `scripts/`, importing only `orchestratable=True` functions from `tools/registry.py` via `registry.namespace()`, execute it once through bash, and return its printed digest rather than raw tool output.

Never mark destructive, rate-limited, or side-effecting tools `orchestratable=True`.

For independent research/lookup subtasks followed by synthesis, use:

`orchestrator_cli.py "<task>"`

See `docs/ORCHESTRATOR.md`.

For multi-document fact chains:

`kg_cli.py ingest <file>` → `kg_cli.py query`

See `docs/KNOWLEDGE_GRAPH.md`.

---

# 8. Command Execution

Every shell command must be declared as `COMMAND_EXEC_SCHEMA` and executed through `common/shell_exec.run()` according to:

`.claude/skills/command-execution/SKILL.md`

Rules:

* `max_lines` is mandatory; default 50, never >100 without justification.
* Use filtering (`grep`, `tail`, `regex`, `fields`) to retrieve only required data.
* `jq` requires `jq`; otherwise use `fields`/`regex`.
* If output is truncated, refine the filter; never increase the line limit.
* Never retry destructive commands.
* Declare correct `classification`: `read_only | mutating | destructive`.
* Destructive commands require `confirm_destructive`.
* Declare `timeout_s` (1–900s) and validated `cwd`.
* Prefer environment allowlists to prevent secret exposure.
* Enforce `output_policy.max_bytes`; line limits alone are insufficient.
* Redaction is enabled by default.

Runtime results include truncation, timeout, duration, classification, redaction, command hash, and start time.

`.claude/hooks/pre_tool_use.py` enforces the same policy for raw Bash:

* unbounded reads are refused;
* first reads are limited to ≤30 lines and must be paged;
* destructive commands and credential disclosure are refused;
* policy misconfiguration may be relaxed for one session with
  `TB_COMMAND_POLICY=warn|off`.

The hook shares `classify()` with runtime enforcement and falls back to `config/command_policy.json` when necessary.

---

# 9. CI Observability

## CI logs are prohibited

Do not fetch CI logs, job records, annotations, artifacts, caches, check results, or equivalent output.

This includes:

`gh run view/list/download`, `gh workflow run/view/list`, `gh pr checks`, `gh cache`, relevant `gh api` endpoints, `gh pr view --json statusCheckRollup`, equivalent `curl`, `act`, workflow dispatch/re-run solely for inspection, and live monitoring such as `tail -f`, `watch`, polling loops, or log-follow commands.

No output-size limit makes CI log access permissible.

## Failure information channel

`.github/workflows/ci-failure-notify.yml` is the sole CI failure-information interface.

After **all watched workflows** finish for a commit, it creates/updates **one comment per commit** containing:

* `all checks green`, or the number of non-green jobs
* each non-green workflow/job/conclusion
* failing step
* exact failing lines

It must contain no advice, footer, or log link.

Required behavior:

* first verify that all checks passed individually, if any failed/skiped/neutral then fix its root cause and run only the fixed ones.before you push the final resolved/fixed PR, you should have cofidence 100% for this will pass all checks with only one push and got merged cleanly.
* one comment per commit
* never report before all watched workflows finish
* filter meaningless exit-code noise
* prefer meaningful summaries over bare traceback frames

Read the latest notice comment. If it is insufficient, fix the notice mechanism rather than accessing logs.

Waiting for merge follows the same rule: return later and read comments; never poll or monitor.

---

# 10. Pull Requests and Branch Updates

`main` requires four gate checks and an up-to-date branch.

PRs:

* run independently
* are never automatically parked/drafted
* use `--squash --auto`
* merge automatically when required checks are green

The old `.github/workflows/pr-queue.yml` and draft guards remain removed. They caused required checks to report `skipped`, which GitHub could treat as satisfied.

Every gate job must have **only**:

`if: always()`

No draft guard or alternative condition.

## GOV-017 — Automatic branch update

`.github/workflows/pr-auto-update.yml` runs on every push to `main` and updates only the **oldest open non-draft PR whose `mergeable_state=behind`**.

Do not update all PRs simultaneously; that creates redundant runs against a moving `main`.

Flow:

**update oldest → checks → auto-merge → main push → update next**

This is not a queue: PRs are never parked, drafted, or closed.

The workflow requires `secrets.PR_AUTOUPDATE_TOKEN` (PAT or GitHub App token with `repo` scope). A `GITHUB_TOKEN` push does not trigger the required workflow chain and can leave stale check runs attached to the updated branch.

Missing token must fail without modifying anything.

The alternative is GitHub merge queue; gating workflows already declare `merge_group`.

---

# 11. CI Cost — GOV-015

`tests/test_ci_workflow_cost.py` enforces CI efficiency.

* Only `python-tests` installs the project.
* `python-lint`, `python-coverage-floors`, and `architecture` install only the tool they execute.
* Ruff pin comes from `requirements-dev.txt`.
* Required dependency installs use SHA-pinned `astral-sh/setup-uv` with cache.
* Gating workflows may not use bare `pip install -r requirements`.
* Torch workflows must use the CPU index first.
* Pytest uses six shards with `total == len(shard)`.
* Superseded branch runs are cancelled; `main` is exempt.
* Adding unnecessary project installation fails the test.

---

# 12. Test-Speed Policy — GOV-016

New tests must minimize execution cost without reducing correctness, assertion strength, coverage, or quality requirements.

* Never sleep for synchronization; drive clocks or await actual events.
* `asyncio.sleep(0)` is allowed only as a scheduler yield.
* Do not spawn Python subprocesses when direct imports/calls are possible.
* No network, real database, or real filesystem outside `tmp_path`.
* Scope expensive read-only fixtures as widely as correctness permits.
* Prefer `pytest.mark.parametrize` to setup-heavy loops.
* Use the smallest input that proves the property.
* Read shared files once per module where possible.

`tests/test_suite_speed_budget.py` is a ratchet: real sleeps/process spawns may only decrease. Lowering is always valid; increasing requires explicit justification in the diff.

---

# 13. Mathematical Registry

`config/math_registry.json` is the sole source of truth for mathematical objects the project relies on or explicitly rejects.

Each entry is classified as:

`load_bearing | performance_critical | attack_surface | provenance_only | folklore`

Load via `src/mathcore/registry.py`, which validates strictly.

Rules:

* `implemented` requires an existing owner module.
* `folklore` can never be `implemented`.
* One owner per concept.
* Multi-implementation concepts use package `owner` plus module `component`s; components receive the same existence validation.
* `implemented_by()` includes components; `owned_by()` does not.
* `depends_on` must resolve and remain acyclic.
* `docs/MATH_FOUNDATIONS.md` is generated; update registry then run:
  `python3 scripts/generate_math_docs.py`
* CI runs generation with `--check`.

Architecture/contracts: `docs/MATH_ARCHITECTURE.md`

Build order/gates: `docs/MATH_ROADMAP.md`

---

# 14. Quality Engineering

All changes to `src/`, `tests/`, `config/*.json`, or `.github/workflows/` carry the quality contract.

Before coding establish:

1. Which requirement does this serve?
2. Which test decides it?
3. What happens in production if it is wrong?

The registry entry and deciding test belong in the same commit as the behavior.

Before pushing:

`python3 .claude/skills/quality-engineering/scripts/qe_gate.py`

The gate validates:

* registry/schema consistency
* claimed test existence
* generated traceability
* JSON validity
* PR workflow gate structure

Full tests still run in GitHub Actions; local success does not prove merge-gate success.

Rules:

* `.claude/skills/quality-engineering/SKILL.md`
* `.claude/skills/quality-engineering/qe.config.json`

`.claude/hooks/quality_gate.py` applies mechanical checks after edits. It fails open and may be disabled for one session with `TB_QUALITY_HOOK=off`.

Never weaken a gate merely to pass. If a rule is wrong, change it deliberately and justify the change in the same diff.

---

# 15. Architecture Layering — GOV-018

`config/architecture_layers.json` defines:

**foundation**
→ config, logging_setup, mathcore, quality

**crypto**
→ ecc, security

**io**
→ data, diagnostics

**analytics**
→ causal, engines, features, fusion, intelligence, models, regime, tuning

**decision**
→ execution, risk, strategies

**orchestration**
→ engine, intel, upgrade, workers

**edge**
→ api

Packages may import their own layer or lower layers, never higher layers.

`scripts/check_static_invariants.py` enforces layering. `check_import_cycles` separately detects module-level cycles; package-level cycles are also prohibited because they can hide behind deferred/submodule imports.

`accepted_upward_edges` is a ratchet containing the six currently documented inversions:

* remove the entry when the inversion is fixed;
* never add an entry merely to suppress a violation;
* if an inversion is genuinely valid, change the architecture/layer model.

New top-level `src/` packages must be assigned a layer before they pass validation.

---

# 16. Seam Contracts — SIG-004

Important producer/consumer boundaries must explicitly protect hand-off contracts.

For `src/engines/`, `EngineOrchestrator.run` maps engines to `E-01`…`E-18` positionally. Reordering the engine list can silently misattribute results, SLAs, and logs.

For every seam worth protecting:

* define the contract once;
* assert it at both producer and consumer;
* make positional pairings explicit;
* define degradation behavior;
* account for every item exactly once;
* smoke-test the composition root.

For engines:

* empty input must yield a valid abstention;
* every cycle must return all 18 engines as output or named failure;
* `tests/test_engine_seam_contract.py` must detect reordering.

---

# 17. Quality and Security Registry

`config/quality_registry.json` is the sole source of truth for requirements, trading invariants, regressions, security regressions, and their deciding tests. Load via `src/quality/registry.py`.

Rules:

* `verified`/`partial` entries must reference existing tests.
* `planned` entries must reference no test and must specify the PR phase that will add one.
* `critical` cannot be `accepted_gap`; first establish that it is not critical.
* Prefix and `kind` must agree:

  * `INV-` = invariant
  * `REG-` = regression
  * `SEC-` = security regression
  * other = requirement
* `depends_on` must resolve and remain acyclic.
* `docs/quality/REQUIREMENTS_TRACEABILITY.md` is generated:
  update registry, then run `python3 scripts/generate_quality_docs.py`;
  CI runs `--check`.
* Every production defect gets a permanent `REG-####` entry and test.
* Every security finding gets a permanent `SEC-####` entry and test.
* Such tests are never removed merely because the defect/finding was fixed.

References:

* `docs/quality/QUALITY_POLICY.md`
* `docs/quality/TEST_STRATEGY.md`
* `docs/quality/CI_GATE_ARCHITECTURE.md`
* `docs/quality/IMPLEMENTATION_PLAN.md`
* `docs/security/`

---

# 18. Required Checks

A PR merges only when every required check is green.

Every PR workflow must end in a `gate` job that:

* `needs:` every other job in that workflow;
* uses `if: always()`;
* runs `scripts/assert_jobs_green.py`;
* succeeds only when every result is `success`;
* exits `2` on malformed input.

Rules:

* Every new job must be added to the gate's `needs:`; `tests/test_assert_jobs_green.py` requires exact set equality.
* Never remove `if: always()`.
* Current exception: `retrieve-context` on fork PRs.
* Branch protection requires the gates, not individual jobs.

See `docs/REQUIRED_CHECKS.md`.

---

# 19. Cloud Review — Component 5

Every PR is automatically reviewed by:

`.github/workflows/claude-review.yml`

Review uses MongoDB Atlas RAG + knowledge-graph retrieval through `review/retrieval.py`, including vector, full-text, hybrid, and graph retrieval.

The review is **advisory only** and never approves or merges.

Setup and GitHub App/secret requirements: `docs/CLOUD_REVIEW.md`.
