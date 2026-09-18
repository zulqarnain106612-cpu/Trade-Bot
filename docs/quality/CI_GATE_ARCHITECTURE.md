# CI gate architecture

## 1. The five gates

| Gate | Where it runs | What must pass |
|---|---|---|
| **A — developer** | Local, before push | lint, format, unit, type check |
| **B — pull request** | GitHub Actions | security, coverage, unit, integration, architecture, regression |
| **C — merge** | Branch protection | all required checks green, approved review, branch up to date |
| **D — release** | GitHub Actions | full regression, model verification, trading invariants, security, E2E, artifact verification |
| **E — live** | Operator + CI | paper qualification, risk qualification, operational readiness, manual approval, canary |

Gates A and B are advisory to each other, not redundant: A exists so a
developer finds a lint failure in seconds instead of in a CI round trip, and B
exists because A is not enforceable.

## 2. "Green" means `success`

Neutral, skipped, cancelled and failed are all **not green**.

GitHub cannot express that directly. Branch protection requires checks **by
name**, and a check that never reports satisfies the requirement by being
absent. A job skipped by an `if:`, or never reached because an earlier job
failed, produces no check at all.

So every workflow that runs on a pull request ends in a `gate` job that:

- `needs:` **every** other job in that workflow,
- runs `if: always()`,
- calls `scripts/assert_jobs_green.py`,
- and passes `NEEDS: ${{ toJSON(needs) }}`.

`assert_jobs_green.py` exits non-zero unless every result is `success`.
Malformed input exits **2** rather than 0, because a gate that cannot evaluate
has verified nothing.

The rules, restated because each one has already failed once somewhere:

- **Add a job, add it to that workflow's gate `needs:`.**
  `tests/test_assert_jobs_green.py` asserts the two sets are equal, so
  forgetting fails the suite rather than silently narrowing coverage.
- **Never drop `if: always()`** from a gate. Without it the gate is skipped the
  moment a dependency fails, and a skipped required check blocks nothing.
- **A legitimate skip goes in `ALLOW_SKIPPED`** with a comment saying why. It
  excuses skipping only — an allowed job that fails still fails the gate.
- **Require the gates in branch protection, not the individual jobs.**
  Requiring a job directly reintroduces the hole.

Setup steps and the required-check names are in `docs/REQUIRED_CHECKS.md`.

## 3. Target workflow architecture

Seven workflows, reached incrementally. Creating twenty empty workflows on day
one produces twenty checks that verify nothing.

```
.github/workflows/

pr-quality.yml
security.yml
integration.yml
regression.yml
nightly-quality.yml
release.yml
production.yml
```

### `pr-quality.yml`

```
Ruff
format
type checking
unit
property tests
coverage
architecture
frontend
```

### `security.yml`

```
CodeQL
dependency review
secret scanning
dependency audit
workflow security checks
```

### `integration.yml`

```
database
API
exchange mocks
WebSocket
storage
```

### `regression.yml`

```
signal fixtures
model fixtures
risk invariants
historical defects
backtest consistency
```

### `nightly-quality.yml`

```
mutation
fuzz
chaos
performance
large datasets
full regression
```

### `release.yml`

```
build
SBOM
artifact hash
attestation
release verification
```

### `production.yml`

```
environment approval
artifact verification
deployment
smoke
health
rollback
```

## 4. Mapping the target onto what exists

The repository has six workflows today. The target is reached by extending and
splitting them, not by abandoning them.

| Target workflow | Today | Gap and owning phase |
|---|---|---|
| `pr-quality.yml` | `ci.yml` — lint, format, tests, coverage floors, architecture, frontend | Type checking and a dedicated property-test job. PR-006. |
| `security.yml` | `security.yml` + `codeql.yml` | Dependency review, dependency audit, workflow-security checks, action-pin and permission assertions. PR-009. |
| `integration.yml` | Inside `ci.yml` (TimescaleDB service) | Split out once exchange mocks and WebSocket integration exist. PR-005, PR-007. |
| `regression.yml` | — | Created with the golden fixtures it runs. PR-003, PR-004, PR-006. |
| `nightly-quality.yml` | **Exists** *(PR-006)* — mutation matrix, extended fuzz, metrics | Chaos and performance jobs. PR-010. |
| `release.yml` | — | Created with SBOM, hash and attestation. PR-009. |
| `production.yml` | — | Created with the protected environment and canary. PR-012. |
| (advisory) | `claude-review.yml` | Stays advisory. It never approves and never merges. |
| (advisory) | `workflow-lint.yml` | Runs on every pull request, not only workflow changes. |
| (scheduled) | `rag-ingest.yml` | Does not run on pull requests, so it has no gate. |

**Every new workflow above must ship with its own `gate` job in the same PR.**
`tests/test_assert_jobs_green.py` parametrises over `.github/workflows/*.yml`
and will fail the moment a pull-request workflow appears without one.

## 5. Branch protection for `main`

Refused:

```
❌ direct push
❌ force push
❌ bypass CI
❌ merge failing PR
❌ unreviewed critical change
```

Required:

```
✓ PR
✓ required checks
✓ review
✓ security checks
✓ regression
✓ branch up to date
```

Applied as code by `scripts/apply_repo_ruleset.py` and verified by
`tests/test_apply_repo_ruleset.py` (`GOV-008`).

## 6. Production environment

Production is a protected GitHub Environment, not a branch:

```
main
 ↓
release candidate
 ↓
CI
 ↓
artifact
 ↓
security verification
 ↓
paper
 ↓
production approval
 ↓
deploy
```

Required:

```
✓ protected environment
✓ approved deployment
✓ trusted artifact
✓ no production credentials in PR jobs
```

**The production environment must not be reachable simply because somebody
pushed to `main`** (`REL-002`).

## 7. Secrets in CI

```
Public PR
   ↓
tests
   ↓
NO production secrets
```

```
Production deployment
   ↓
trusted branch + trusted workflow + environment approval + short-lived credentials
```

A malicious PR must never be able to print an exchange key by modifying a test
(`SUP-003`). `retrieve-context` in `claude-review.yml` is the single entry in
any gate's `ALLOW_SKIPPED` today, precisely because it is skipped on fork pull
requests.

## 8. Action hardening

- Every workflow declares `permissions:` explicitly, starting from
  `contents: read`, and grants only what that workflow needs (`SUP-001`).
- Every third-party action is pinned to a full 40-character commit SHA, never a
  tag or branch (`SUP-002`). The existing workflows already do this; PR-009
  turns the convention into an assertion.
- `permissions: write-all` and equivalent broad grants are refused.
- A dependency-update PR runs the full quality gate and can never trigger the
  production workflow on its own (`SUP-007`).
