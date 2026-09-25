---
name: quality-engineering
description: >
  The quality and security contract every change to this repository is held
  to: a registry entry and the test that decides it land in the same commit as
  the behaviour, generated docs are regenerated rather than edited, and gates
  are argued with rather than widened. Use this for ANY change to src/, tests/,
  config/*.json or .github/workflows/ -- a feature, a bug fix, a refactor, a
  dependency bump, a workflow edit -- and whenever the words requirement,
  invariant, regression, security finding, coverage, traceability, registry,
  gate, schema, readiness or "production ready" come up. Also use it when
  reviewing a diff, when deciding whether something is done, and when a check
  fails and the tempting fix is to loosen it.
---

# Quality engineering

This repository already decided how it verifies itself. Ninety-one registry
entries, each naming the test that decides it; gates that fail closed; a
traceability document generated from the registry rather than written beside
it. The machinery exists. What this skill carries is the part that is easy to
lose: **the habit of adding to it rather than around it.**

The failure mode is not malice or laziness. It is that a change arrives with a
deadline, the registry feels like paperwork, and the test that would have
decided the change gets written afterwards — at which point it is a test the
code already passes, which is a different and much weaker thing.

## The three questions

Before writing code, answer these. They take a minute and they change what
gets built:

1. **Which requirement does this serve?** A registry id. If none fits, this
   change is introducing a requirement — add it (`scripts/qe_new_requirement.py`)
   rather than proceeding without one. A behaviour nobody agreed to is a
   behaviour nothing decides.

2. **Which test decides it?** Name the file and what it would catch that
   nothing else does. If the honest answer is "nothing", the test is coverage
   rather than verification, and the change is still unverified.

3. **What goes wrong in production if this is wrong?** This decides how much
   care the change deserves. A logging tweak and an order-sizing change are
   not the same risk and should not get the same review.

For anything beyond a one-liner, write the answers as a change plan and check
it: `qe_gate.py --plan plan.json` (schema: `schemas/change_plan.schema.json`).
The value is not the file — it is that the questions have to be answerable
before the code exists rather than after.

## The loop

```
plan → registry entry → test → implementation → gates → push → CI decides
```

Three things about the order.

**The registry entry comes before the test** because the entry is where the
statement gets written as a proposition. "The kill switch is durable" is a
slogan; "activating the kill switch survives a process restart" is something a
test can decide, and writing the entry is what forces the difference.

**The test comes before the implementation** where you can manage it. Not
dogma — but a test written against code that exists tends to assert what the
code does, and a test written against a statement asserts what it should do.

**CI decides, not your machine.** The full suite runs in GitHub Actions. Local
runs prove nothing about the gate that merges the change, and they are slow
enough that people skip them. The exception is this skill's own gates: they
read files, take seconds, and catch the malformed registry before the push
rather than three round trips later.

```bash
python3 .claude/skills/quality-engineering/scripts/qe_gate.py
```

Run it before every push. It checks the registry against its schema, that
every claimed test exists, that the traceability document is in sync, that
every JSON file parses and the schema'd ones validate, and that every workflow
that runs on a pull request ends in a gate needing all its jobs.

## Status is a claim about evidence

The four statuses are not a progress bar. They are claims, and each has a
shape the schema enforces:

| Status | Means | Carries |
|---|---|---|
| `verified` | a test decides it, today | ≥1 test, **no** `planned_in` |
| `partial` | some of it is decided | ≥1 test **and** the phase that finishes it |
| `planned` | scheduled, no evidence yet | **no** test, **and** a `planned_in` |
| `accepted_gap` | deliberately not done | a waiver with an expiry; never on a `critical` entry |

The one that matters most is `verified` with an empty verification list —
that is the single failure the registry exists to prevent, and it is now
rejected by `config/quality_registry.schema.json` rather than only by the
loader, so any tool reading the registry sees it.

To waive a critical requirement you must first argue, in the diff, that it is
not critical. That argument is the control, not the waiver.

## Defects and findings are permanent

Every production defect gets a `REG-####` entry and a test. Every security
finding gets a `SEC-####` entry and a test. **Neither test is ever deleted
because the problem is fixed** — the test is what stops it coming back, and
deleting it converts a permanent guarantee into a historical anecdote.

```bash
python3 .claude/skills/quality-engineering/scripts/qe_new_requirement.py \
  --prefix REG- --title "..." --statement "..." --criticality high \
  --subsystem execution --source QE-51 --failure-mode "..." \
  --status verified --test tests/execution/test_the_fix.py --test-type regression
```

The scaffolder builds the entry from the rules — kind from the id prefix,
`planned_in` or `verification` from the status — and validates before writing
with **the same loader the gate runs**, not just the JSON schema. The schema
does not know the test-type taxonomy, so schema-only validation once printed
`[ok  ] added SEC-0001` for an entry `load_registry` then refused (REG-0011).

`--prefix` allocates against **every branch**, not this work tree. The work
tree sees only what has merged to main, so two branches open at once used to
be handed the same id — three times in one session, caught by hand every time
(REG-0010). `--local-only` restores the old behaviour for a tree with no refs
to read. `git fetch` first if you want branches pushed since your last fetch
to count; the scan reads the object store and never the network.

`qe_gate.py --only registry-id-collision` is the same check as a gate: it
reports an id this branch adds that another live branch already holds. It is
advisory, because a stale branch nobody will finish can hold an id hostage and
that is a conversation rather than a block. The blocking half is
`registry-loader`, which refuses a duplicate id once both entries are in one
file — correct, but only after the merge that created the problem.

## When a gate fails

The instinct is to make it pass. Sometimes that is right and sometimes it is
how a gate becomes decorative, so separate the two cases:

- **The code is wrong.** Fix the code. Most of the time.
- **The gate is wrong.** Say so in the diff, change the rule deliberately, and
  explain what it was protecting against and why that no longer applies.
  Changing a threshold in the same commit as the change it was blocking, with
  no argument, is the pattern to avoid — a year later nobody can tell whether
  the rule was wrong or the deadline was close.

Never widen a bound to get green. Never add `# noqa` without a reason beside
it. Never edit `docs/quality/REQUIREMENTS_TRACEABILITY.md` by hand — it is
generated; edit the registry and run `scripts/generate_quality_docs.py`.

## Writing tests that are worth having

The repository's existing suites are the reference. What they have in common:

- **The docstring says what the test would catch**, not what it calls. A
  reader six months from now needs to know whether deleting it loses anything.
- **The negative cases carry the weight.** A validator nobody has shown a
  rejection to is a validator that passes because it finds nothing. For every
  "this is accepted", write the "this is refused".
- **Failure-mode first.** Ask what breaks in production, then write the test
  that sees it. Tests derived from the code's structure test the structure.
- **An unevaluable check fails.** If a test cannot determine the answer —
  missing fixture, absent data — that is not a pass.

## And written to run fast (GOV-016)

The suite runs on every push, six shards wide, across 349 files. A tenth of a
second added here is a tenth of a second on every pull request from now on, and
nobody ever traces it back to the commit that added it. Speed is a property of
a new test, not an optimisation pass afterwards — and it never comes out of
what the test asserts.

- **Never sleep to wait.** Drive the clock or await the event the code signals.
  `asyncio.sleep(0)` is a scheduler yield and is free; everything else stalls.
- **Never spawn a process to run Python you can import.** `import x; x.main([…])`
  skips interpreter startup and gives a real traceback.
- **No network, no real database, no filesystem beyond `tmp_path`.** Fake at
  the boundary. A test that fails when something else is down is slow *and*
  flaky.
- **Scope fixtures as wide as correctness allows.** Anything that parses a
  file, builds a registry or compiles a schema is `module` or `session` scope.
- **`parametrize`, not a loop with setup inside it.** Parametrised cases shard
  and parallelise, and report one failure per problem.
- **Smallest input that can still fail.** Three rows, not three thousand —
  unless the size *is* the property under test.

`tests/test_suite_speed_budget.py` ratchets the mechanical half: real sleeps
and process spawns carry a frozen budget that may only be lowered, and a budget
with slack left in it fails until it is lowered, so a saving is banked instead
of being spent on the next test that wants a sleep. Lowering one is always in
order; raising one is argued in the diff like any other gate.

## Reference

- `references/workflow.md` — the change classes, what each requires, and
  worked examples of a feature, a bug fix and a workflow edit.
- `qe.config.json` — the gates, commands and rules as data. The scripts and
  the hook read this, so it cannot drift from what is documented here.
- `schemas/change_plan.schema.json` — the plan structure.
- `config/quality_registry.schema.json` — the registry contract, including the
  conditional rules described above.
- Repository docs: `docs/quality/QUALITY_POLICY.md`,
  `docs/quality/TEST_STRATEGY.md`, `docs/quality/CI_GATE_ARCHITECTURE.md`,
  `docs/REQUIRED_CHECKS.md`.
