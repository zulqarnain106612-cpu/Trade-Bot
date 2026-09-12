# Quality baseline

Where the tree actually stands, as of the Quality & Security Foundation
(PR-001). This document is the honest "before" picture that the rest of the
programme is measured against. It is deliberately unflattering where the
evidence is thin.

## 1. Size

| Measure | Value |
|---|---|
| First-party source files (`src/`) | 221 |
| First-party source lines | ~58,600 |
| Test files (`tests/`) | 324 |
| Test lines | ~82,000 |
| GitHub workflows | 6 |
| PreToolUse/session hooks | 2 |

Test code outweighs source code by roughly 1.4×. That is a good sign about
effort and says nothing on its own about what is verified — which is the gap
`config/quality_registry.json` exists to close.

## 2. What is already enforced

These are real, running controls, not intentions:

| Control | Where | Notes |
|---|---|---|
| Lint (`ruff check`) | `.github/workflows/ci.yml` | E, F, W, ASYNC, I, UP, B, SIM, RUF059, PLW1510 |
| Format (`ruff format --check`) | `ci.yml` | |
| Repo-wide coverage floor | `pyproject.toml` | `--cov-fail-under=99`, branch coverage on, `precision = 2` so the comparison is against the real number |
| Per-file coverage floor | `scripts/check_coverage_floors.py` | `MIN_PERCENT = 90.0`, `EXCEPTIONS` currently empty; a ratchet, with stale exceptions reported |
| Architecture law gate | `scripts/arch_gate.sh`, `config/arch_baseline.json` | SARIF uploaded to code scanning |
| Static invariants | `scripts/check_static_invariants.py`, `tests/test_static_invariants.py` | The seed of `GOV-004` |
| All-green gate per workflow | `scripts/assert_jobs_green.py` | `needs:`-coverage asserted by `tests/test_assert_jobs_green.py` |
| Branch protection as code | `scripts/apply_repo_ruleset.py` | `GOV-008` |
| Command-execution schema | `common/command_schema.py`, `common/shell_exec.py`, `.claude/hooks/pre_tool_use.py` | Output caps, effect classification, secret redaction on by default |
| CodeQL | `.github/workflows/codeql.yml` | |
| Security workflow | `.github/workflows/security.yml` | |
| Cloud review | `.github/workflows/claude-review.yml` | Advisory only; never approves or merges |
| Math foundations registry | `config/math_registry.json`, `src/mathcore/registry.py` | The governance pattern `src/quality/registry.py` follows |
| TimescaleDB service in CI | `ci.yml` | Added because 92 skips were hiding the storage backend |

## 3. Maturity assessment by subsystem

Against the levels in `docs/quality/QUALITY_POLICY.md` §10.

| Subsystem | Level today | What is missing for the next level |
|---|---|---|
| Risk | 3 *(PR-002)* | Mutation score (`RISK-006`, PR-006). Boundaries, sizing properties and gate failure-injection are now covered. |
| Execution | 2 → 3 | Exchange-response contract tests, total-FSM proof, concurrency and duplicate-response races |
| Portfolio | 3 *(PR-002)* | Both scalars are now asserted on the notional that reaches the sizer rather than on the number the tracker reports. |
| Signal | 3 *(PR-003)* | Mutation score (`SIG-003`, PR-006). Golden fixtures and cross-process determinism are now covered. |
| Model | 3 *(PR-004)* | Lookahead gate, purged-CV assertions, artifact provenance and round-trip, drift demotion and the research firewall are all covered. |
| Data | 3 *(PR-003)* | Venue precision rules (`DATA-005`, PR-005). Freshness budgets, the clock policy and the money representation are now declared and enforced — and the quality gate is actually wired. |
| API | 2 | Authorization matrix not exhaustive, no IDOR/injection/SSRF suites, headers untested |
| Security (crypto/secrets) | 2 | No key-rotation drill, no TLS-failure suite, no CSPRNG assertion, no log-leak provocation suite |
| Supply chain | 2 | No SBOM, no attestation, no pin/permission assertions, no fork-secret test |
| Resilience | 1 → 2 | No crash-replay, no restore drill, no declared RTO/RPO, no performance baselines |
| Release | 1 | No protected environment, no canary, no startup self-test, no behavioural halt |
| Governance | 3 | Traceability now machine-checked; metrics and mutation still missing |

**Target: Level 5 on the trading-critical path** — risk, execution, signal,
model, data and the security controls around credentials.

## 4. Registry snapshot at PR-001

91 entries: 10 trading invariants and 81 requirements. No regressions
(`REG-####`) or security regressions (`SEC-####`) are recorded yet, because
none has been raised through the process this programme establishes; the first
of each will be filed the first time a defect or finding is found.

Status distribution is deliberately honest rather than flattering:

- `verified` — only where an existing test genuinely decides the statement.
  That is four governance entries whose verification is the registry machinery
  added in this PR.
- `partial` — the common case: the tree already touches the requirement and a
  named test exists, but the statement is not yet fully pinned. Each names the
  PR that finishes it.
- `planned` — no test exists. Each names the PR that will add one.
- `accepted_gap` — none. Nothing has been waived.

The exact counts are generated into
`docs/quality/REQUIREMENTS_TRACEABILITY.md` and will change as the programme
runs; this section describes the shape, not the numbers.

## 5. Known structural weaknesses

Stated plainly, because a baseline that omits them is not a baseline.

1. **`tests/` is nearly flat.** 324 files, mostly at the top level, named by
   module rather than by the question they answer. The taxonomy in
   `docs/quality/TEST_STRATEGY.md` is the target; migration is gradual and
   classification comes before movement.
2. **A number of test files are named `test_coverage_boost*.py` and
   `test_residual_gaps_batch*.py`.** Those names describe why they were
   written, not what they verify. They keep the coverage gate green; they do
   not tell a reader what property holds.
3. **Coverage is high and mutation score is unknown.** The policy is explicit
   that the first without the second is weak evidence (`RISK-006`,
   `EXEC-007`, `SIG-003`, `GOV-003`).
4. **There is no deployment pipeline to verify.** Release, artifact, canary
   and production controls (`REL-002`…`REL-008`, `SUP-004`) describe a
   pipeline that does not exist yet; PR-009 and PR-012 build it and its tests
   together.
5. **A control can exist, be tested, and never be called.** `DataQualityGate`
   had its own test file from the day it was written and no module in `src/`
   ever invoked it, so every check ran in the suite and nowhere else. PR-003
   wired it; the general lesson is that "is it tested" and "is it reachable"
   are separate questions, and the registry's `owning_modules` field is where
   the second one is recorded.
6. **Environment-side security is out of scope for the repository.** Exchange
   key restrictions, server hardening, network segmentation and backup
   encryption are recorded as requirements with documented procedures, but the
   repository can only verify the parts that reach code and CI. The split is
   spelled out in `docs/security/SECURITY_POLICY.md` §2.

## 6. How this document stays true

`QUALITY_BASELINE.md` is written by hand and is therefore the file in this
directory most likely to drift. It is updated at the close of each phase in
`docs/quality/IMPLEMENTATION_PLAN.md`, and the numbers in §1 and the status
shape in §4 are the parts to re-check. The generated matrix
(`REQUIREMENTS_TRACEABILITY.md`) is always authoritative where the two disagree.
