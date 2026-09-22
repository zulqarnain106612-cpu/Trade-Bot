
# Project directives

## Programmatic Tool Calling (PTC) pattern

For any task involving 3+ dependent/looped tool calls, or a large/metadata-heavy
tool result: do not call tools one at a time in conversation. Instead follow
`.claude/skills/programmatic-tool-calling/SKILL.md` -- write one script under
`scripts/`, importing only functions from `tools/registry.py`'s
`registry.namespace()` (orchestratable=True tools), and run it once via bash.
Return the answer from the script's printed digest, not from raw tool output
pasted into the conversation.

Never mark a destructive, rate-limited, or side-effecting tool
`orchestratable=True` in `tools/registry.py`. Those stay direct-call-only.

See `docs/PTC_PATTERN.md` for the full rationale and the API-vs-local mapping.

## Orchestrator (plan-big / execute-small)

For a task that naturally splits into independent research/lookup subtasks
followed by a synthesis step, use `orchestrator_cli.py "<task>"` instead of
doing all the reading yourself. It delegates subtasks to a cheap model
(`WORKER_MODEL`, default haiku) and only has the coordinator model
(`COORDINATOR_MODEL`, default sonnet) see short digests, not raw output.
See `docs/ORCHESTRATOR.md`.

## Knowledge Graph (Extract -> Resolve -> Assemble -> Query)

For questions that span multiple documents and require chaining facts
("who works with people who worked on project X"), use `kg_cli.py query`
instead of re-reading every source document. Ingest sources once with
`kg_cli.py ingest <file>` -- this persists structured facts in MongoDB so
future questions don't re-pay the extraction cost. Queries only load a
bounded subgraph (`KG_MAX_SUBGRAPH_TRIPLES`), never the whole graph.
See `docs/KNOWLEDGE_GRAPH.md`.

## Command Execution (output-capped, schema-enforced)

Every shell command Claude runs must be declared as a `COMMAND_EXEC_SCHEMA`
object (see `common/command_schema.py`) and executed via `common/shell_exec.run()`.
Raw stdout/stderr never enters context. Works in local terminal sessions and
cloud containers (`CLAUDE_CODE_REMOTE=true`).
Skill: `.claude/skills/command-execution/SKILL.md`

Hard rules:
- `max_lines` cap is mandatory on every command. Default: 50.
- Use `filter_mode` (grep/tail/regex/fields) to extract only needed data.
- `filter_mode=jq` requires `jq` on PATH -- use `fields`/`regex` on cloud containers.
- If `result["truncated"]` is True -> fix `filter_expr`, never raise `max_lines`.
- Never retry destructive commands (rm, DROP, DELETE).
- Never set `max_lines` > 100 without explicit justification.

## Command execution is enforced, not advised

`COMMAND_EXEC_SCHEMA` is version **1.2.0** (`common/command_schema.py`). Beyond
the output caps above, every declaration may and usually should carry:

- `classification`: `read_only` | `mutating` | `destructive`. **Mandatory in
  effect** -- `shell_exec.run()` refuses to execute when the declared class is
  weaker than what `classify()` detects in the command string. Misdeclaring is
  a hard error, not a warning.
- `confirm_destructive`: required alongside `classification="destructive"`.
  Destructive commands are never retried, whatever `retry_policy` says.
- `timeout_s`: per-attempt wall clock, 1-900s. Declare it; do not rely on the
  `run()` kwarg default.
- `cwd`: validated to exist before anything runs.
- `env`: `{inherit, allowlist, overrides}`. Use `allowlist` whenever the child
  does not need the parent's secrets, which is almost always.
- `output_policy.max_bytes`: byte cap, default 65536. `max_lines` alone is not
  a bound -- one minified or base64 line can be megabytes.
- `output_policy.redact`: secret masking, **on by default**. Turn it off only
  when you have established the output cannot contain a credential.
- `purpose`: one line on why. Optional, but it is the only field that makes the
  audit trail answer "why" rather than just "what" -- write it.
- `schema_version`: the version the declaration targets. Omit it and it means
  the current one. A version outside `SUPPORTED_SCHEMA_VERSIONS` is **refused
  before the declaration is validated**, because the wrong schema would report
  the symptom rather than the cause (SEC-0001).

`result` now also carries `purpose`, `bytes_truncated`, `timed_out`,
`duration_s`, `classification`, `redactions_applied`, `command_sha256` and
`started_at`.

Two properties of 1.2.0 that exist because 1.1.0 claimed them without having
them, and which must not be traded away:

- **The `jsonschema`-absent path is not a weaker path.** `_validate_fallback()`
  walks `COMMAND_EXEC_SCHEMA` itself rather than restating its rules, so a
  constraint added to the schema binds both paths and neither can drift
  (SEC-0002). A schema keyword the fallback cannot check is a loud error there,
  not a silent pass. Never reintroduce a hand-written subset of checks.
- **Every call leaves one audit record**, refusals included, on the
  `tradebot.command_audit` logger: `command_sha256`, `purpose`,
  classification declared and detected, outcome, exit code (SEC-0003). The
  command string is deliberately never in it -- a command line carries tokens
  and the record goes wherever the host sends logs.

A `PreToolUse` hook (`.claude/hooks/pre_tool_use.py`, policy in
`config/command_policy.json`) enforces the same rules on raw Bash calls, so
these constraints hold in a session that never read this file:

- Unbounded reads are refused. Use `sed -n '1,30p'`, `head -30`, `grep -m 30`,
  `-n 30`. Then fetch the next thirty in a separate call.
- A bound larger than 30 lines is refused. Widening the first fetch is the
  specific thing the directive forbids; page instead.
- On a bound violation exactly one line leaves the hook:
  `only <=30 lines are allowed,run command for minimum line which can make you
  understand the failure`. Nothing else is emitted, because a refusal that
  spends five sentences of context defeats the rule it is enforcing.
- Destructive commands are refused, with the `shell_exec` path named in the
  refusal.
- Commands that would print credentials into the transcript are refused.

The hook shares `classify()` with the runtime, so the two can never disagree.
It fails **open** on its own misconfiguration and can be relaxed for one
session with `TB_COMMAND_POLICY=warn|off` -- that override is the rollback
path, not a way around a refusal you disagree with.

## CI observability: the notice comment, and nothing else

Reading CI is how an agent burns a context window without noticing. The rule is
permanent, unconditional and mechanical -- not a matter of judgement in the
moment, and not a line budget:

- **No CI log, job record, step annotation, artifact, cache entry or check
  result may be fetched, in any condition.** `gh run view`/`list`/`download`,
  `gh workflow run`/`view`/`list`, `gh pr checks`, `gh cache`,
  `gh api .../actions/...`, `gh api .../check-runs`, `gh pr view --json
  statusCheckRollup`, `curl` to any of those, and `act` are all refused. So is
  dispatching or re-running a workflow in order to read what it prints: that is
  the same act with an extra step.
- **There is no bound that makes it allowed.** This is not an output-size rule,
  so `| head -5`, `--limit 10` and a `grep` filter do not help. The previous
  policy said "the minimum number of lines that explains the failure is always
  permitted", and that judgement -- made by the party that wants the lines --
  decayed into paging logs a few lines at a time, which is the cost the cap
  existed to prevent, paid in instalments.
- **Live monitoring stays banned too**, separately: `tail -f`,
  `docker/kubectl logs -f`, `watch -n`, `inotifywait -m`, `while true` poll
  loops, and the `Monitor` tool.
- **The comment channel stays open, and is checked first.** `gh pr view --json
  comments`, `gh pr/issue comment`, `gh api .../issues/.../comments`, plus
  ordinary `gh pr view/list/create/merge`. Closing this would leave no route at
  all to a failure's cause, and a guard with no route is a guard the next
  session switches off.

Enforced at both points, sharing one implementation
(`common.command_schema.is_ci_log_access`): the `PreToolUse` hook on raw Bash
calls, and `shell_exec.run()` so that routing a command through the sanctioned
runner is not a way around the hook. The hook falls back to the equivalent
patterns in `config/command_policy.json` when the project is not importable,
and `tests/test_ci_log_access.py` asserts the two lists agree.

### The notice comment is the whole interface

`.github/workflows/ci-failure-notify.yml` is the only channel, so it is
complete on its own. It waits until **every** watched workflow has finished for
a commit, then posts or edits **one** comment carrying:

- the status -- `all checks green`, or how many jobs are not green;
- for each one that is not green: the workflow, the job, the conclusion, the
  failing step, and **the exact failing lines**, extracted server-side from
  check annotations or, failing that, from a filtered read of the job log.

Nothing else. No advice, no footer, no link to a log nobody may open. Three
properties are load-bearing and each has a test:

- **One comment per commit**, not one per workflow and never one per check.
  The concurrency group is keyed on the head SHA, so the last workflow to
  finish posts and earlier partial pictures are cancelled.
- **Never posted early.** A notice claiming "all green" while a workflow is
  still in flight is worse than no notice, because it is the only thing anyone
  can read.
- **The runner's exit code is not an error message.** `Process completed with
  exit code 1` is filtered as noise; a prioritised signal list prefers the
  pytest summary line over a bare traceback frame. That string was the entire
  content of the notice on one failing shard, and it named no test, no file and
  no assertion.

Read the last such comment on the pull request. It contains what a log read
would have told you, and if it does not, fix the notice -- do not fetch the
log.

Waiting for a merge is the same discipline: come back after a plausible
interval and read the comments. No polling loop, no watch, no background
monitor.

Auto-merge does the rest. Every pull request is set to `--squash --auto`, so
GitHub merges it the moment its required checks are green, with no session
running and nobody watching.

## Pull requests: open one, let the checks run, merge when green

`main` is protected by a ruleset requiring the four gate checks **and** an
up-to-date branch. Nothing parks, promotes or serialises pull requests any
more. Open one, its checks run, and it merges when they are green.

There used to be a controller (`.github/workflows/pr-queue.yml`) that kept one
entry active and parked the rest as drafts. It is removed, along with the draft
guard every gating job carried for it. The reason is not that it was
complicated: **a parked entry accumulated check runs whose conclusion was
`skipped`, and GitHub counts a required check that reports `skipped` as
satisfied.** #292 merged into `main` with all four gates skipped and its test
suite never run. The mechanism built to guarantee "no neutral, no skipped, no
failed" was the mechanism producing it.

So:

- **A gate job may carry no condition but `always()`.** Not a draft guard, not
  anything else. `tests/test_assert_jobs_green.py` and `qe_gate.py` both check
  this as an exact alternative rather than a substring.
- **Nothing converts a pull request to a draft on your behalf.** A draft is a
  human saying the work is not ready.
- Open as many pull requests as you like. They are independent.

### The "Update branch" button presses itself (GOV-017)

The ruleset requires an up-to-date branch, so every merge leaves every other
open pull request one commit behind. That button was the last manual step in
the merge path and the reason a green pull request could sit for hours.

`.github/workflows/pr-auto-update.yml` presses it. On every push to `main` it
takes the **oldest open non-draft pull request whose `mergeable_state` is
`behind`**, updates that one, and stops. Not all of them: updating every branch
at once starts N full runs against a `main` that is about to move again, and
N-1 of them answer a stale question. The chain sustains itself -- the updated
pull request goes green, auto-merge merges it, that push fires the workflow
again, and the next one is updated.

It is **not** the queue coming back. Nothing is parked, drafted or closed, and
every pull request still runs its own checks whenever it likes.

**It requires `secrets.PR_AUTOUPDATE_TOKEN`** -- a PAT or GitHub App token with
`repo` scope. Not an option: a push made with `GITHUB_TOKEN` starts no workflow
run, so a branch updated with it would carry the check runs of its *previous*
head -- up to date, green-looking and permanently unmergeable. That is the trap
the removed queue's promotion step fell into. With the secret absent the job
fails loudly and touches nothing, which is the correct half of the operation to
perform.

The zero-token alternative is the **merge queue**: every gating workflow already
declares `merge_group`, so enabling it in branch protection makes GitHub build
each pull request on top of `main` and run the checks there, and no branch ever
needs updating. Either is fine; doing neither means clicking.


## CI wall clock is a property with a test (GOV-015)

A pull request used to wait ten to twelve minutes on a clean run, and almost
none of it was testing. Three of the five CI jobs installed the full runtime
dependency set -- a CPU torch wheel included -- to run `ruff check`, `coverage
report` and a handful of stdlib-only registry scripts. An install is invisible
in a green run, which is exactly why it survived.

The shape that fixed it is pinned by `tests/test_ci_workflow_cost.py`:

- **Only `python-tests` installs the project.** `python-lint`,
  `python-coverage-floors` and `architecture` run stdlib-only scripts; each
  installs the single tool it invokes and nothing else. The lint job reads its
  ruff pin out of `requirements-dev.txt` rather than repeating it.
- **Every required check that installs the requirements goes through `uv`**
  (`astral-sh/setup-uv`, SHA-pinned) with its cache enabled — `python-tests`,
  the review job's retrieval stack, and CodeQL's Python extractor. Same pins,
  same interpreter, a fraction of the time. Bare `pip install -r requirements`
  in a workflow that gates the merge fails the test.
- **Every workflow that installs torch names the CPU index first.** PyPI's
  default wheel bundles the CUDA runtime: gigabytes for a CPU runner.
- **Six shards, and `total` equals the length of `shard`.** pytest-split is
  told `--splits $SHARD_TOTAL`; a mismatch runs part of the suite twice and
  part never, which is a coverage hole wearing a green check.
- **A superseded run on a branch is cancelled.** `main` is exempt.

Adding an install to a job that does not import the project fails that test.
That is deliberate: the cost is otherwise attributable to nothing.

## New tests are written for speed (GOV-016)

Every test and every check added from now on is written so it runs as fast as
it can while still deciding what it is there to decide. This is not a licence
to assert less -- the coverage floor and the quality contract are untouched.
It is a constraint on *how* the assertion is reached.

The suite is 349 files and runs on every push, six shards wide. A test that
takes an extra tenth of a second costs that tenth on every pull request
forever, and nobody ever attributes it to the commit that added it. So:

- **Never sleep to wait for something.** Drive the clock (`monkeypatch` the
  time source, `freeze` it, advance it), or await the event, condition or
  future the code actually signals. `asyncio.sleep(0)` as a scheduler yield is
  free and fine; anything else is a stall.
- **Never spawn a process to run Python you can import.** `subprocess.run([sys
  .executable, "scripts/x.py"])` pays interpreter startup and re-imports the
  world; `import x; x.main([...])` does not, and gives a real traceback.
- **No network, no real database, no real filesystem beyond `tmp_path`.** Fake
  the client at its boundary. A test that can fail because something else is
  down is slow *and* flaky.
- **Scope fixtures as wide as correctness allows.** Anything that parses a
  file, builds a registry or compiles a schema is `scope="module"` or
  `scope="session"`. Function scope for a read-only value re-does the work once
  per case.
- **`@pytest.mark.parametrize`, not a loop with setup inside it.** Parametrised
  cases shard and parallelise; a loop is one case that runs N times in one
  worker and reports one failure for N problems.
- **Build the smallest input that can fail.** Three rows, not three thousand;
  four bits of a key, not 256, unless the size *is* the property.
- **Read a file once per module, not once per assertion.** Most of these tests
  are checks over the same handful of YAML and JSON files.

`tests/test_suite_speed_budget.py` holds the mechanical half as a **ratchet**:
the number of real sleeps and process spawns in the suite has a frozen budget
that may only be lowered, and the test fails if the budget has slack left in
it, so a saving is banked rather than spent on the next test that wants a
sleep. The rest of the list is not machine-checkable and is enforced in review.

Lowering a budget is always in order. Raising one has to be argued in the diff,
like any other gate.


## Mathematical foundations registry

`config/math_registry.json` is the single source of truth for every
mathematical object this project relies on or explicitly refuses, with a
verdict on each (`load_bearing`, `performance_critical`, `attack_surface`,
`provenance_only`, `folklore`). Load it through `src/mathcore/registry.py`,
which validates it strictly on read.

Rules:

- **Never claim an implementation that does not exist.** An entry marked
  `implemented` must name an owning module present on disk; the loader checks
  the filesystem and refuses otherwise.
- **Never mark a `folklore` entry `implemented`.** Numerology cannot become a
  live signal by editing one field.
- **One owner per entry.** An object that is genuinely one concept with more
  than one implementation -- `finite-fields`, GF(p) and GF(2^n) -- names the
  package as `owner` and each implementing module as a `component`. Components
  are held to the same existence check as owners; the kind buys a second
  module, not a weaker standard. `registry.implemented_by()` answers "what am
  I on the hook for if I change this file?"; `owned_by()` does not see
  components.
- `depends_on` must resolve and the graph must stay acyclic. Both were violated
  by the registry's first draft and caught by the loader; that is why the
  checks exist.
- `docs/MATH_FOUNDATIONS.md` is **generated**. Edit the registry, then run
  `python3 scripts/generate_math_docs.py`. CI runs it with `--check`.

Design laws, module contracts and wiring points: `docs/MATH_ARCHITECTURE.md`.
Build order and exit gates: `docs/MATH_ROADMAP.md`.

## Quality engineering applies to every change

Every change to `src/`, `tests/`, `config/*.json` or `.github/workflows/`
carries the quality contract with it. It is not a phase, a review step, or
something to add at the end: the registry entry and the test that decides a
change land in the same commit as the behaviour, because a test written after
the code tends to assert what the code does rather than what it should.

Before writing code, three questions: **which requirement does this serve,
which test decides it, and what goes wrong in production if it is wrong?**
A change that cannot answer them is not ready to be written.

Before pushing:

```bash
python3 .claude/skills/quality-engineering/scripts/qe_gate.py
```

Seconds, reads files only. It checks the registry against its schema, that
every claimed test exists, that the generated traceability document is in
sync, that every JSON file validates, and that every pull-request workflow
ends in a gate needing all its jobs. The full suite runs in GitHub Actions --
a local pass proves nothing about the gate that merges the change.

The reasoning, the change classes and the worked examples:
`.claude/skills/quality-engineering/SKILL.md`. The gates, commands and rules
as data: `.claude/skills/quality-engineering/qe.config.json`. A `PostToolUse`
hook (`.claude/hooks/quality_gate.py`) applies the mechanical half after every
edit, so a broken registry or an unpinned workflow surfaces immediately rather
than in CI twenty minutes later; it fails open and can be silenced for one
session with `TB_QUALITY_HOOK=off`.

Never widen a gate to make a change pass. If a rule is wrong, argue it in the
diff and change it deliberately -- a threshold moved in the same commit as the
change it was blocking, with no argument, is indistinguishable a year later
from a deadline that was close.

## Wiring: dependencies run downward (GOV-018)

`config/architecture_layers.json` declares what may import what. Seven layers,
bottom to top: **foundation** (config, logging_setup, mathcore, quality) ->
**crypto** (ecc, security) -> **io** (data, diagnostics) -> **analytics**
(causal, engines, features, fusion, intelligence, models, regime, tuning) ->
**decision** (execution, risk, strategies) -> **orchestration** (engine, intel,
upgrade, workers) -> **edge** (api). A package may import from its own layer or
any layer below it, never above.

`check_layering` in `scripts/check_static_invariants.py` enforces it, and runs
in the suite on every push like every other invariant there.

Why a second check when `check_import_cycles` exists: that one refuses a
*module*-level cycle, which is the easy case -- it fails at import time, so it
reports itself. A **package**-level cycle never fails. It hides behind
submodule and deferred imports and surfaces instead as two packages that cannot
be changed, tested or reasoned about apart.

Six upward edges exist today and are declared in `accepted_upward_edges`, each
with the argument for it. Two of them are real cycles (`api <-> engine`,
`engine <-> strategies`) and two are domain code importing the HTTP edge
(`engine -> api`, `intelligence -> api`). The list is a **ratchet**: fixing one
means deleting its entry, and the check fails if a declared inversion no longer
exists, so the fix is banked rather than leaving a slot for the next one.

- **Adding a top-level package under `src/` fails the check** until it is
  placed in a layer. That is deliberate -- where a package sits is an
  architectural decision, not a default.
- **Never add to `accepted_upward_edges` to make a violation go away.** Move
  the shared shape down instead. If the edge is genuinely right, the layer
  order is wrong, and that is the thing to change.

## Seam contracts: assert a hand-off from both sides (SIG-004)

`src/engines/` is the widest hand-off in the system -- eighteen producers, one
consumer -- and `EngineOrchestrator.run` attributes the results **by position**:

```python
zip(self._engines, [f"E-{i:02d}" for i in range(1, 19)], strict=True)
...
eid = f"E-{i + 1:02d}"
```

Nothing in the code says `self._engines[3]` is E-04. Reorder that list and
every output is filed under the wrong engine, every SLA lands on the wrong
engine, every log line names the wrong thing -- with no exception raised and
no per-engine test failing, because each engine is still correct on its own.
Swapping two entries fails exactly one test in the suite:
`tests/test_engine_seam_contract.py`. Before it existed, it failed none.

The pattern, for any seam worth defending:

- **Write the contract once, assert it twice** -- at the producer and again
  where the consumer receives it (`_assert_contract` is called from both). If
  the two ever drift, the gap is where the assertions differ.
- **Make an implicit positional agreement explicit.** Wherever one list is
  consumed against a parallel list of names or ids, pin the pairing.
- **Degradation is part of the contract.** Every engine given empty data must
  abstain into a *valid* output; raising would be swallowed by the gather and
  become a silently missing engine.
- **Account for everything, once.** A cycle must return all eighteen as either
  an output or a named failure -- no drops, no duplicates. That is the loss no
  per-engine test can see.
- **Smoke-test the composition root.** Constructing `EngineOrchestrator` and
  running one real cycle is the cheapest proof the pipeline still assembles.

## Quality and security requirements registry

`config/quality_registry.json` is the single source of truth for every
requirement, trading invariant, regression and security regression this project
holds itself to, and for **which test decides each one**. Load it through
`src/quality/registry.py`, which validates it strictly on read.

Rules:

- **Never claim a test that does not exist.** An entry at status `verified` or
  `partial` must name at least one test file, and the loader stats every one of
  them. A renamed or deleted test turns the claim red.
- **Never leave work without an owner.** An entry at status `planned` must name
  **no** test and **must** name the PR phase that will add one.
- **A `critical` entry can never be an `accepted_gap`.** To waive it you must
  first argue, in the diff, that it is not critical.
- The id prefix and the declared `kind` are one fact: `INV-` invariant, `REG-`
  regression, `SEC-` security regression, anything else a requirement.
- `depends_on` must resolve and the graph must stay acyclic.
- `docs/quality/REQUIREMENTS_TRACEABILITY.md` is **generated**. Edit the
  registry, then run `python3 scripts/generate_quality_docs.py`. CI runs it
  with `--check`.

Every production defect gets a `REG-####` entry and a permanent test; every
security finding gets a `SEC-####` entry and a permanent test. Neither test is
ever deleted because the problem is fixed.

Policy and taxonomy: `docs/quality/QUALITY_POLICY.md`,
`docs/quality/TEST_STRATEGY.md`. Gates and workflows:
`docs/quality/CI_GATE_ARCHITECTURE.md`. Phase sequence and branch mapping:
`docs/quality/IMPLEMENTATION_PLAN.md`. Threat model, security policy, incident
runbooks, key management and the GitHub configuration checklist: `docs/security/`.

## Required checks: no neutral, no skipped, no failed

A pull request merges only when every check is green. Neutral, skipped,
cancelled and failed are all "not green".

GitHub cannot express that directly: branch protection requires checks **by
name**, and a check that never reports satisfies the requirement by being
absent. A job skipped by an `if:`, or never reached because an earlier job
failed, produces no check at all.

So every workflow that runs on a pull request ends in a `gate` job that
`needs:` every other job in it, runs `if: always()`, and calls
`scripts/assert_jobs_green.py`. The script exits non-zero unless every result
is `success`; malformed input exits 2 rather than 0, because a gate that
cannot evaluate has verified nothing.

Rules:

- **Add a job, add it to that workflow's gate `needs:`.**
  `tests/test_assert_jobs_green.py` asserts the two sets are equal, so
  forgetting fails the suite rather than silently narrowing coverage.
- **Never drop `if: always()`** from a gate. Without it the gate is skipped
  the moment a dependency fails, and a skipped required check blocks nothing.
- **A legitimate skip goes in `ALLOW_SKIPPED`** with a comment saying why. It
  excuses skipping only -- an allowed job that fails still fails the gate.
  There is exactly one entry today (`retrieve-context`, on fork pull
  requests).
- **Require the gates in branch protection, not the individual jobs.**
  Requiring a job directly reintroduces the hole.

Setup steps and the required-check names: `docs/REQUIRED_CHECKS.md`.

## Cloud review + retrieval (Component 5)

Every pull request is automatically reviewed by `.github/workflows/claude-review.yml`,
grounded in this project's MongoDB Atlas RAG + knowledge graph via
`review/retrieval.py` (vector/full-text/hybrid/graph patterns). This is
advisory only -- it never approves or merges. See `docs/CLOUD_REVIEW.md` for
setup and the one-time secrets/GitHub App requirements.
