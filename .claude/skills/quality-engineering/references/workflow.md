# Change classes, and worked examples

`SKILL.md` covers the loop and the reasoning. This is the detail: what each
kind of change owes, and three worked examples of the changes that actually
come up.

## What each change class owes

Read from `qe.config.json`, so the scripts and this document cannot disagree.

| Class | Triggered by | Owes |
|---|---|---|
| `production_behaviour` | any edit under `src/` | a registry entry, the test that decides it, traceability regenerated, a stated blast radius and rollback |
| `test_only` | edits under `tests/` | if the test decides a requirement, it belongs in that requirement's `verification` list |
| `workflow` | `.github/workflows/*.yml` | the gate job needs every job, actions SHA-pinned, `permissions:` least-privilege |
| `config_schema` | `config/*.json` | validates against its schema |
| `defect_fix` | a bug found in production | a `REG-####` entry and a permanent test |
| `security_finding` | anything security-relevant | a `SEC-####` entry and a permanent test |
| `docs_only` | `docs/`, comments | nothing further — but generated docs are regenerated, never edited |
| `refactor_no_behaviour_change` | moving code | the tests that prove the claim; "no behaviour change" is exactly what a test can check |

The last one gets skipped most often and is worth a sentence: the refactor
that quietly changed one behaviour is the reason the claim is tested rather
than asserted in the commit message.

## Worked example — a new feature

*"Add a per-symbol daily loss limit."*

**Plan.** New requirement: nothing in the registry covers a per-symbol limit.
The failure mode is concrete — without it, one symbol can consume the whole
daily loss budget while every existing limit reports healthy, which is a
portfolio-level control that misses a concentration.

```bash
python3 .claude/skills/quality-engineering/scripts/qe_new_requirement.py \
  --prefix RISK- --title "Per-symbol daily loss is bounded" \
  --statement "For each symbol, realised plus unrealised loss on a trading day never exceeds the configured per-symbol ceiling." \
  --criticality critical --subsystem risk --source QE-42 \
  --failure-mode "One symbol consumes the whole daily budget while portfolio-level limits still report healthy." \
  --status planned --planned-in PR-013
```

**Test before implementation.** The boundary is the interesting part: exactly
at the ceiling is permitted, one tick past is refused, and a position that
crosses it through an unrealised move is refused as well — that last case is
the one an implementation written from the description tends to miss.

**Implementation**, then flip the entry to `verified` with the test named.
Regenerate the traceability document. Run `qe_gate.py`. Push; CI decides.

## Worked example — a production bug

*"Orders were double-submitted after a venue timeout."*

The fix is the smaller half. The entry and the test are what stop it
returning:

```bash
python3 .claude/skills/quality-engineering/scripts/qe_new_requirement.py \
  --prefix REG- --title "A venue timeout never releases the idempotency key" \
  --statement "After a timeout with no venue response, the idempotency key stays claimed and a retry is refused rather than submitted." \
  --criticality critical --subsystem execution --source QE-51 \
  --failure-mode "A retry after an ambiguous failure places a second order against a first one that may well have executed." \
  --status verified --test tests/recovery/test_crash_replay.py --test-type regression
```

Write the test so it fails against the old behaviour. A regression test that
passes before the fix is testing something else.

**The test is permanent.** It is not deleted when the bug is old, when the
module is rewritten, or when it looks redundant next to a newer test. It is
the only thing standing between here and the same bug in a year.

## Worked example — a workflow edit

*"Add a job that runs the mutation-score check."*

Three things, and the third is the one that gets forgotten:

1. SHA-pin every action, with the version in a trailing comment so a reader
   can tell what it is.
2. `permissions:` at the top, starting from `contents: read`.
3. **Add the job to that workflow's `gate` job `needs:`.** A job outside
   `needs` can fail while the required check stays green, which is the exact
   hole the gate pattern exists to close. `qe_gate.py` checks this, and so
   does `tests/test_assert_jobs_green.py`.

If the job legitimately skips on some events, add it to `ALLOW_SKIPPED` with a
comment saying why. That allowance excuses skipping only — a job that runs and
fails still fails the gate.

## Reviewing somebody else's diff

Four questions, in this order:

1. **What decides this?** Find the test. If there isn't one, that is the
   finding — everything else is secondary.
2. **Would the test fail without the change?** A test that passes against both
   versions is documentation, not verification.
3. **What is the failure mode?** If the diff cannot answer it, the change has
   not been thought through to the end.
4. **Did anything get quieter?** A widened threshold, a new `noqa`, a removed
   assertion, a gate dependency dropped. Each may be fine; each needs its
   argument in the diff rather than in somebody's memory.

## Where the enforcement lives

Three layers, deliberately:

- **The schema** (`config/quality_registry.schema.json`) holds every rule JSON
  Schema can express, so a tool reading the registry without importing the
  loader still sees them.
- **The loader** (`src/quality/registry.py`) holds what the schema cannot:
  whether a claimed test exists on disk, whether `depends_on` resolves,
  whether the graph is acyclic.
- **The hook** (`.claude/hooks/quality_gate.py`) runs after an edit to a
  registry, schema or workflow file and reports what broke immediately, rather
  than letting it surface in CI twenty minutes later.

Knowing which layer owns which rule matters when something needs changing: a
rule in the wrong layer either cannot be expressed or is invisible to half the
tools that should see it.
