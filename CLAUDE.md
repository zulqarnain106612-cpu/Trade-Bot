
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

`COMMAND_EXEC_SCHEMA` is version **1.1.0** (`common/command_schema.py`). Beyond
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

`result` now also carries `bytes_truncated`, `timed_out`, `duration_s`,
`classification`, `redactions_applied`, `command_sha256` and `started_at`.

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

## CI observability: capped logs, no live monitoring

Reading CI is how an agent burns a context window without noticing. The rules
are permanent and mechanical, not a matter of judgement in the moment:

- **Live monitoring is banned outright.** `gh run watch`, `--watch`, `tail -f`,
  `docker/kubectl logs -f`, `watch -n`, `inotifywait -m` and `while true`
  poll loops are refused however they are spelled, and so is the `Monitor`
  tool. A per-call line bound cannot cap a stream that has no end.
- **Bulk log and report retrieval is capped, not banned.** `gh run view --log`,
  `--log-failed`, `gh run list` and `gh api .../logs` are allowed only behind
  an explicit bound of 30 lines or fewer. Fetch the minimum number of lines
  that identifies the failure -- filter to the assertion, not to the run.
- **`gh pr checks` / `gh pr view` are untouched.** A PR's check summary is one
  line per check, and it is the right way to learn a PR's state.

Do not go looking for failures. `.github/workflows/ci-failure-notify.yml`
reports them: every completed run of a pull-request workflow leaves one
self-updating comment on the pull request naming each job that is **not
green** -- failure, cancellation, timeout, neutral, action_required, stale, or
an unexcused skip -- with its first failing step **and the exact failing
lines**, extracted server-side from the check annotations or, failing that,
from a filtered and capped read of the job log. A green run posts nothing, and
a standing red notice is rewritten as recovered once the fix lands.

That comment is the interface. Read the last such comment on the pull request;
it already contains what a log read would have told you. Only if it genuinely
does not, fetch at most thirty lines filtered to the failing assertion. Never
fetch a full run log or report, and never open a live watch.

Waiting for a merge is the same discipline: come back after a plausible
interval and read the comments. No polling loop, no `gh run watch`, no
background monitor.

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
- **The suite's install goes through `uv`** (`astral-sh/setup-uv`, SHA-pinned)
  with its cache enabled and keyed on both requirements files. Same pins, same
  interpreter, a fraction of the time, paid once per shard.
- **Every workflow that installs torch names the CPU index first.** PyPI's
  default wheel bundles the CUDA runtime: gigabytes for a CPU runner.
- **Six shards, and `total` equals the length of `shard`.** pytest-split is
  told `--splits $SHARD_TOTAL`; a mismatch runs part of the suite twice and
  part never, which is a coverage hole wearing a green check.
- **A superseded run on a branch is cancelled.** `main` is exempt.

Adding an install to a job that does not import the project fails that test.
That is deliberate: the cost is otherwise attributable to nothing.


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
