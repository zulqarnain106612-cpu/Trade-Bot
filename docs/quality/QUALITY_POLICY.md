# Quality policy

## 1. The objective

The goal is not "zero bugs". A promise of zero defects is unfalsifiable, and a
team that has made one stops reporting the defects it finds. NIST frames secure
development the same way: the aim is to reduce the likelihood and the impact of
vulnerabilities, not to claim their elimination.

The engineering objective for Trade-Bot is:

> **No known critical defect, no unverified security-critical change, no
> untested trading invariant, no unreviewed production change, and no live
> deployment unless every mandatory quality and security gate passes.**

Each clause is checkable. That is the point of stating it this way.

## 2. The lifecycle this policy describes

```
                    REQUIREMENT
                         │
                         ▼
                 RISK CLASSIFICATION
                         │
                         ▼
                  THREAT MODEL
                         │
                         ▼
                   IMPLEMENTATION
                         │
                         ▼
                 STATIC VERIFICATION
                         │
             ┌───────────┴───────────┐
             ▼                       ▼
       SECURITY TESTS          QUALITY TESTS
             │                       │
             └───────────┬───────────┘
                         ▼
                   UNIT TESTS
                         ▼
               PROPERTY/FUZZ TESTS
                         ▼
             FEATURE/SIGNAL TESTS
                         ▼
                 MODEL TESTS
                         ▼
                INTEGRATION TESTS
                         ▼
                REGRESSION TESTS
                         ▼
               SYSTEM/E2E TESTS
                         ▼
              TRADING GAUNTLET
                         ▼
                   PAPER TRADING
                         ▼
                    CANARY
                         ▼
                     LIVE
                         ▼
                 MONITORING
                         │
                         ▼
                  INCIDENT/DEFECT
                         │
                         ▼
                 ROOT-CAUSE ANALYSIS
                         │
                         ▼
                  NEW REGRESSION TEST
                         │
                         └──────► CI
```

The loop at the bottom is the part that matters most. Every failure must make
the system harder to break the same way again.

## 3. Standards basis

The baseline is deliberate rather than aspirational:

| Standard | What it gives us |
|---|---|
| NIST SSDF (SP 800-218) | The secure-development foundation: practices that reduce vulnerability likelihood and impact across the lifecycle. |
| NIST CSF 2.0 | The broader cybersecurity risk-management structure the controls hang from. |
| NIST SP 800-61 Rev. 3 | Incident response, aligned with CSF 2.0. See `docs/security/INCIDENT_RESPONSE.md`. |
| OWASP ASVS 5.0.0 | Application-security verification requirements for the FastAPI and WebSocket layer. |

## 4. Verification and validation are both required

**Verification** asks: did we build the system correctly?

```
Risk limit = 5%
Code actually enforces ≤ 5%
```

**Validation** asks: did we build the correct system?

```
The trading strategy behaves as intended
The risk policy matches business requirements
The live gate actually protects the intended capital constraints
```

A system can pass every verification test and still be the wrong system.
Acceptance testing covers both, and the registry's `test_type` vocabulary keeps
the two distinguishable rather than collapsing both into "tests".

## 5. Requirements are registry entries, not prose

Every requirement, trading invariant, regression and security regression lives
in `config/quality_registry.json` with an id, a testable statement, a
criticality, an owning module and a named verifying test.

`src/quality/registry.py` validates the file on load. It refuses:

- an entry claiming a test that is not on disk;
- an entry claiming an owning module that is not on disk;
- an entry with no test **and** no PR that will add one;
- a `critical` entry waived through `accepted_gap`;
- a declared kind that contradicts the id prefix;
- a dangling or cyclic `depends_on`.

`docs/quality/REQUIREMENTS_TRACEABILITY.md` is generated from the registry, and
CI fails when the generated file is stale. The matrix therefore cannot claim
coverage the tree does not have.

## 6. Criticality and what it buys

| Criticality | Meaning | Consequence |
|---|---|---|
| `critical` | Violation can lose money, leak a credential, or let unreviewed code reach production. | Cannot be waived. Must be verified before the associated capability goes live. |
| `high` | Violation degrades a safety control or hides a failure without directly moving funds. | May be waived only with a dated, attributed waiver. |
| `medium` | Violation costs correctness or operability with a bounded blast radius. | Waivable. |
| `low` | Hygiene. | Waivable. |

A waiver carries a reason, a person and a review date. A waiver with no expiry
is a permanent hole, so the schema refuses one.

## 7. Quality gates

Five gates, described in full in `docs/quality/CI_GATE_ARCHITECTURE.md`:

| Gate | Where | What must pass |
|---|---|---|
| A — developer | Local | lint, format, unit, type check |
| B — pull request | CI | security, coverage, unit, integration, architecture, regression |
| C — merge | Branch protection | all required checks green, approved review |
| D — release | CI | full regression, model verification, trading invariants, security, E2E, artifact verification |
| E — live | Operator + CI | paper qualification, risk qualification, operational readiness, manual approval, canary |

"Green" means `success`. Neutral, skipped, cancelled and failed are all
"not green", which is why every pull-request workflow ends in a `gate` job that
`needs:` every other job and runs `if: always()`.

## 8. The definition of production ready

Production readiness is a conjunction, not a percentage:

```
PRODUCTION READY =
    all mandatory tests pass
    AND all critical security controls pass
    AND no unresolved critical/high defects
    AND risk invariants pass
    AND model verification passes
    AND regression suite passes
    AND recovery test passes
    AND paper qualification passes
    AND artifact integrity verified
    AND production approval obtained
```

Not:

```
coverage > 90%
```

Coverage is an input to the argument, never the argument. A high coverage
number with a poor mutation score is weak evidence, which is why
`config/quality_registry.json` carries per-subsystem mutation targets
(`RISK-006`, `EXEC-007`, `SIG-003`) alongside the coverage floor CI already
enforces.

## 9. Continuous improvement

```
Production → Metrics → Defects → Incidents → Root cause
    → Corrective action → New test/control → CI gate → Release → Production
```

Two rules give the loop teeth:

- **Every production defect gets a `REG-####` registry entry** with a root
  cause and a permanent test. Regression tests are never deleted because the
  bug is fixed (`GOV-001`).
- **Every security finding gets a `SEC-####` registry entry** with a permanent
  test, so a fixed weakness cannot silently return (`GOV-002`).

Root-cause analysis stops at a control, not at a person. The question is never
"who made the mistake" but **"why could one mistake reach production?"**
(`GOV-006`).

## 10. Maturity target

| Level | Contents |
|---|---|
| 1 — Basic | unit tests, lint, CI |
| 2 — Controlled | integration, coverage, branch protection, security scanning |
| 3 — Verified | regression, property tests, risk invariants, model verification, E2E |
| 4 — Resilient | fuzz, chaos, recovery, performance, disaster recovery |
| 5 — High assurance | supply-chain security, artifact provenance, cryptographic controls, continuous threat monitoring, formal traceability, mutation testing, red-team exercises, independent security review |

**Level 5 is the target for the trading-critical path.** Not every UI utility
needs Level 5 treatment, and `GOV-009` requires that claim to be made per path
rather than globally on the strength of the easiest subsystem.
`docs/quality/QUALITY_BASELINE.md` records where each subsystem actually sits
today.

## 11. Related documents

- `docs/quality/QUALITY_BASELINE.md` — where the tree stands today.
- `docs/quality/TEST_STRATEGY.md` — the taxonomy and what each test type is for.
- `docs/quality/REQUIREMENTS_TRACEABILITY.md` — generated matrix.
- `docs/quality/CI_GATE_ARCHITECTURE.md` — the gates and the workflows.
- `docs/quality/QUALITY_METRICS.md` — what is tracked and why.
- `docs/quality/IMPLEMENTATION_PLAN.md` — the PR sequence and branch mapping.
- `docs/security/SECURITY_POLICY.md` — the security half of the same programme.
