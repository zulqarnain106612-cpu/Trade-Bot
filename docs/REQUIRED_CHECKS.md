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

## Repository scope

Two different things are described as "repo-scoped", and only one of them
needs a tool.

**Files are repo-scoped once they are on the default branch.** The workflows,
`config/command_policy.json`, `config/math_registry.schema.json`, the coverage
gate in `pyproject.toml`, the `.claude/` hooks -- every branch cut from `main`
carries them, and every session in the repository reads them. They do not need
a setting to apply everywhere; they need to be merged.

**Blocking is not a file.** Refusing a merge when a check is red is a GitHub
setting, and nothing committed to the repository can enforce it. That makes it
the one rule here invisible to review: nobody can tell from a diff whether it
changed, drifted, or was switched off.

`.github/rulesets/main-protection.json` closes that gap. The policy lives in
the repository, reviewable as a diff, and is applied from there:

```bash
python3 scripts/apply_repo_ruleset.py --dry-run   # show what would be sent
python3 scripts/apply_repo_ruleset.py             # create or update
python3 scripts/apply_repo_ruleset.py --check     # verify, change nothing
```

A ruleset applies to **every** push and pull request targeting the branches in
its `conditions`, for as long as it is active. It is repository state, not
per-pull-request state.

`GITHUB_TOKEN` needs the `administration: write` permission. **The default
Actions token does not have it** -- use a fine-grained PAT or a GitHub App
installation token. A 403 or 404 from this script almost always means the
token is missing that permission, not that the repository is missing.

Run `--check` periodically, or in a scheduled workflow once a token is
available: it is what catches protection being turned off later. A rule that
nobody re-verifies is a rule that quietly stops existing.

### What the ruleset enforces

| Rule | Effect |
|---|---|
| `required_status_checks` | The four gates below must be green |
| `strict_required_status_checks_policy` | Branch must be up to date with the base first |
| `pull_request` | Changes reach `main` through a pull request, with review threads resolved |
| `non_fast_forward` | No force-pushes to `main` |
| `deletion` | `main` cannot be deleted |

| Required check | Covers |
|---|---|
| `CI gate (all jobs green)` | `python`, `architecture`, `frontend` |
| `Security gate (all jobs green)` | `bandit`, `pip-audit`, `npm-audit`, `secrets`, `supply-chain`, `dependency-review`, `container` |
| `CodeQL gate (all jobs green)` | `analyze` (both matrix legs) |
| `Workflow lint gate (all jobs green)` | `actionlint` |

`Release gate (all jobs green)` is deliberately **not** a required check:
release.yml runs only on a version tag or a manual dispatch, never on a pull
request, so requiring it would block every pull request on a check that can
never report. Its gate exists for the same reason as the others — so a failed
build or a failed provenance verification cannot be mistaken for a successful
release.

`dependency-review` skips on push and schedule (it diffs a pull request's base
against its head and has nothing to compare otherwise), which is why it is
named in that workflow's `ALLOW_SKIPPED`. The allowance excuses skipping only:
if it runs and fails, the gate fails.

Require the **gates**, not the individual jobs. Requiring `python` directly
reintroduces the hole: if that job is skipped its check never appears, and the
requirement is vacuously satisfied. `tests/test_apply_repo_ruleset.py` asserts
every required context names a gate that actually exists, because GitHub
accepts a required check no workflow ever posts -- and then pull requests wait
forever, or merge with nothing verified once the rule is relaxed.

### One judgement call for you

`Cloud review gate (all jobs green)` is deliberately **not** required.
`CLAUDE.md` and `docs/CLOUD_REVIEW.md` both describe the cloud review as
advisory -- it never approves or merges. Requiring it would turn an advisory
bot into a merge blocker and would block on infrastructure problems (a missing
`MONGODB_URI`, an Atlas outage) that say nothing about the change under
review.

Its gate job still runs, so its state is always visible. Add the context to
the ruleset if you want it blocking; a test asserts the current choice, so
changing it means changing that test and these docs in the same commit.

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
