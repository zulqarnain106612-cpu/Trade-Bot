# GitHub security configuration checklist

GitHub Actions is a **major attack surface** for this project: it holds
credentials, it runs third-party code, and it is one configuration mistake away
from being a deployment path. This checklist is the configuration half of the
supply-chain requirements (`SUP-001`…`SUP-007`, `GOV-008`, `REL-002`).

Items marked **[CI]** are asserted by a test. Items marked **[console]** must be
verified in the GitHub UI or API, because the repository cannot see them.

## 1. Branch protection for `main`

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

- **[CI]** The ruleset is applied as code by `scripts/apply_repo_ruleset.py`
  and covered by `tests/test_apply_repo_ruleset.py`.
- **[console]** Required checks are the **workflow gate jobs**, by name, not
  the individual jobs. Requiring a job directly reintroduces the
  check-absent-means-satisfied hole. Names are in `docs/REQUIRED_CHECKS.md`.
- **[console]** Administrators are not exempt. An exemption that exists "for
  emergencies" is the path an attacker uses.

## 2. Workflow permissions

- **[CI]** Every workflow declares `permissions:` at the top level, starting
  from `contents: read` (`SUP-001`).
- **[CI]** No workflow uses `permissions: write-all` or an equivalent broad
  grant.
- **[CI]** A job that needs more (for example `security-events: write` for
  SARIF upload) declares it at the **job** level, not the workflow level.
- **[console]** Default workflow permissions for the repository are set to
  read-only, so a workflow that forgets the key does not inherit write.

## 3. Action pinning

- **[CI]** Every third-party `uses:` is pinned to a full 40-character commit
  SHA with the version in a trailing comment (`SUP-002`). The existing
  workflows already follow this; PR-009 turns the convention into an assertion.
- **Review action updates** rather than accepting them automatically. A pinned
  SHA that is bumped without reading the diff provides the pinning ceremony
  without the pinning benefit.

## 4. Secrets

- **[CI]** No workflow triggered by a fork pull request has access to a
  production secret (`SUP-003`).
- **[console]** Production secrets live in **environment** secrets on the
  protected `production` environment, not in repository secrets. Repository
  secrets are visible to any workflow that runs on a branch.
- **[console]** Secret scanning is enabled.
- **[console]** Push protection is enabled.
- **[console]** The Actions secret inventory is reviewed at each rotation; a
  secret nobody can name the consumer of is removed.

The model:

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

**A malicious PR must never be able to print the exchange key simply by
modifying a test.**

## 5. Fork pull requests

For a public repository, explicitly test:

```
fork PR
   ↓
workflow
   ↓
Can attacker access secrets?
```

Expected:

```
NO
```

- **[CI]** This is a security acceptance test, not an assumption (`SUP-003`).
- **[console]** `pull_request_target` is not used for any workflow that checks
  out and executes PR code. If a workflow needs both PR code and a token, the
  two halves are split across separate workflows.
- **[console]** "Require approval for all external contributors" is enabled for
  workflow runs from forks.

## 6. Environments

- **[console]** A `production` environment exists, with required reviewers and
  a deployment branch rule restricting it to `main` or a release tag
  (`REL-002`).
- **[console]** Separate environments for `staging` and `paper`, each with its
  own credentials. Never `staging → production exchange account`.
- **[CI]** `production.yml` verifies artifact provenance before deploying —
  "is this exactly the artifact CI produced?", not "someone copied some files
  onto the server" (`SUP-004`).

## 7. Dependencies

- **[console]** Dependabot alerts and security updates are enabled.
- **[CI]** Dependency review runs on pull requests.
- **[CI]** `pip-audit` (and `npm audit` for `frontend/`) run on a schedule
  (`SUP-005`).
- **[CI]** A dependency-update PR runs the full quality gate and cannot trigger
  the production workflow on its own (`SUP-007`).

Schedule:

```
daily:     security alerts
weekly:    dependency review
monthly:   full dependency audit
on critical CVE: immediate emergency pipeline
```

## 8. Code scanning

- **[CI]** CodeQL runs with an explicit `languages:` input — the one it
  actually reads.
- **[CI]** The architecture gate uploads SARIF so law violations appear as
  annotations on the PR.
- **[console]** Code scanning alerts are triaged, not accumulated. A dismissed
  alert carries a reason.

## 9. Artifacts and provenance

Every deployable artifact carries:

```
version
git commit
dependency lock
build timestamp
SBOM
hash
provenance
signature/attestation
```

- **[CI]** Generated by `release.yml` (`SUP-004`).
- **[CI]** Verified by `production.yml` before deployment.

## 10. Audit

- **[console]** Repository audit log reviewed after any suspected compromise
  (`docs/security/INCIDENT_RESPONSE.md` §4), with specific attention to
  workflow file changes, branch-protection changes, collaborator changes,
  deploy keys and secret access.
- **[console]** Collaborator list reviewed at each rotation; least privilege
  applies to people as well as tokens.

## 11. Verification cadence

| Item | Cadence |
|---|---|
| **[CI]** items | Every pull request |
| Secret inventory and collaborator review | At each key rotation |
| Environment and branch-protection settings | Monthly, and after any incident |
| Fork-secret acceptance test | Every pull request (`SUP-003`) |
| Full checklist walkthrough | Quarterly, and before any production launch |
