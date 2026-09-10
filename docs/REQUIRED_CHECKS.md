# Required checks: no neutral, no skipped, no failed

Policy: **a pull request merges only when every check is green.** Neutral,
skipped, cancelled and failed are all "not green", and none of them may be
waved through.

GitHub does not offer that rule directly, and the gap is not obvious. Branch
protection requires checks *by name*, and a check that never reports satisfies
the requirement by being absent. A job skipped by an `if:`, or never reached
because an earlier job failed, produces no check at all — so a rule that says
"require `pytest`" is silent exactly when `pytest` did not run. That is the
hole this setup closes.

## How it works

Every workflow that runs on a pull request ends in a `gate` job:

```yaml
  gate:
    name: CI gate (all jobs green)
    runs-on: ubuntu-latest
    if: always()
    needs: [python, architecture, frontend]
    steps:
      - uses: actions/checkout@3d3c42e5... # v7.0.1
      - name: Assert every job in this workflow succeeded
        env:
          NEEDS: ${{ toJSON(needs) }}
        run: python3 scripts/assert_jobs_green.py
```

Three parts carry the weight:

- **`needs:` lists every other job in the workflow.** The gate cannot go green
  without each of them reporting. `tests/test_assert_jobs_green.py` asserts
  this set equals the workflow's job list, so adding a job and forgetting to
  gate it fails the test suite rather than silently narrowing coverage.
- **`if: always()`** makes the gate run even when a dependency failed. Without
  it the gate is itself skipped the moment anything upstream fails — and a
  skipped required check blocks nothing, which is the failure mode this whole
  arrangement exists to prevent.
- **`scripts/assert_jobs_green.py`** exits non-zero unless every result is
  `success`. `skipped` and `cancelled` are failures, not passes. Malformed
  input exits 2 rather than 0: a gate that cannot evaluate has verified
  nothing, and must never report a pass.

One implementation, five callers. A shell snippet copied into five workflows
drifts; this does not.

## Turning it on

The workflow half is in the repository. The enforcement half is a repository
setting and **has to be done by someone with admin rights** — it cannot be
committed.

Settings → Branches → branch protection rule for `main` → *Require status
checks to pass before merging*, then add by name:

| Check to require | Covers |
|---|---|
| `CI gate (all jobs green)` | `python`, `architecture`, `frontend` |
| `Security gate (all jobs green)` | `bandit`, `pip-audit`, `npm-audit`, `secrets` |
| `CodeQL gate (all jobs green)` | `analyze` (both matrix legs) |
| `Workflow lint gate (all jobs green)` | `actionlint` |
| `Cloud review gate (all jobs green)` | `retrieve-context`, `claude-review` |

Also enable *Require branches to be up to date before merging*, otherwise a
green result from an older base can be merged into a base that has since
changed.

Require the **gates**, not the individual jobs. Requiring `python` directly
reintroduces the hole: if that job is skipped, its check never appears, and
the requirement is vacuously satisfied.

### One judgement call for you

`docs/CLOUD_REVIEW.md` and `CLAUDE.md` both describe the cloud review as
**advisory only — it never approves or merges**. Making `Cloud review gate`
a required check contradicts that: it turns an advisory bot into a merge
blocker, and it will block on infrastructure problems (a missing
`MONGODB_URI`, an Atlas outage) that say nothing about the change under
review.

The gate job exists on that workflow either way, so its state is always
visible. Whether to mark it *required* is your call. Recommendation: require
the four correctness gates, leave the cloud review gate visible but not
required, and revisit if it proves reliable.

## Legitimate skips

Some skips are correct and must not be forced green. `ALLOW_SKIPPED` names
them explicitly, with the reason in a comment beside it:

```yaml
        env:
          NEEDS: ${{ toJSON(needs) }}
          # retrieve-context: skips on pull requests from forks, where the
          # repository secrets it needs are not available to the workflow
          ALLOW_SKIPPED: retrieve-context
```

Rules for using it:

- One job per entry, each with a comment saying why the skip is correct.
- It excuses **skipping only**. An allowed job that *fails* still fails the
  gate.
- A stale entry — naming a job that no longer exists — is reported in the run
  log and asserted against in the test suite, because an exemption that
  outlived its reason is a hole nobody is watching.

There is currently exactly one entry in the whole repository.

## What this does not cover

Two honest limits.

**Step-level skips are not failures.** Inside a job, `if:` conditions such as
`if: matrix.language == 'python'` correctly skip steps that do not apply to
that matrix leg. Those show as "skipped" in the run log and are not defects —
forcing them green would mean installing Python on the JavaScript leg for no
reason. The gate deliberately works at job granularity.

That said, a condition that can *never* match is a real bug, and it hides as
an innocent skip. `codeql.yml` had one: the matrix produced
`javascript-typescript` while two steps tested for `javascript`, so Node setup
was skipped on both legs. `tests/test_assert_jobs_green.py` does not catch
that class; check by hand that every step condition names a value its matrix
actually produces.

**The CodeQL "neutral" check is not ours to make green.** Code scanning posts
its own check separately from the Actions jobs, and reports *neutral* when the
analysis ran and found nothing new in the code the pull request changes. That
is a normal, non-blocking outcome, not a failure — and no workflow
configuration turns it green, because there is nothing wrong to fix. The
`CodeQL gate` check reports on the analysis **jobs**, which do go green, and
that is the one to require.

If the CodeQL check reports neutral on a pull request that *does* change
scanned code, that is worth investigating — the likely causes are a
non-canonical SARIF `category`, or an analysis on the base branch that the
head cannot be compared against.

## Related

- `scripts/assert_jobs_green.py` — the gate implementation
- `tests/test_assert_jobs_green.py` — script behaviour and workflow wiring
- `.github/workflows/workflow-lint.yml` — actionlint over every workflow
- `docs/CLOUD_REVIEW.md` — why the cloud review is advisory
