# Quality metrics

## 1. What is tracked

| Metric | Source | Why it is here |
|---|---|---|
| Test pass rate | CI | The floor. A flaky suite is a suite nobody reads. |
| Branch coverage | `pytest --cov-branch`, `--cov-fail-under=99` | Input to the argument, never the argument. |
| Per-file coverage | `scripts/check_coverage_floors.py` | An aggregate hides a single file rotting from 99% to 40%. |
| Mutation score | `nightly-quality.yml` (PR-006) | Coverage without it is weak evidence. |
| Regression count | `REG-####` entries in the registry | How many distinct defects have reached a running system. |
| Escaped defects per layer | Incident records | **The most important metric here.** See §2. |
| Critical vulnerabilities | CodeQL, dependency audit | |
| Dependency vulnerabilities | Dependabot, `pip-audit` | |
| Secret incidents | Secret scanning, push protection | Any non-zero value is an incident, not a metric. |
| Mean time to detect | Incident records | |
| Mean time to recover | Incident records | |
| Deployment failure rate | `production.yml` (PR-012) | |
| Rollback rate | `production.yml` (PR-012) | |
| Recovery-test success | Restore drills (PR-010) | A backup never restored is not a proven backup. |
| Model drift | `src/risk/performance_drift.py` | |
| Signal drift | Golden fixtures (PR-003) | |
| Data freshness | `src/data/quality_gate.py` | |
| Order reconciliation failures | Ledger vs exchange (PR-010) | Each one is a potential `INV-009` violation. |

## 2. The metric that matters most

> **How many defects escaped each verification layer?**

Every other number describes effort. This one describes effectiveness. A defect
that reached production passed through unit, property, integration, regression,
E2E, gauntlet, paper and canary. The useful question is not "was there a bug"
but **which layer should have caught it and did not**.

So each defect record names the layer that should have caught it:

```
REG-0007
Layer that should have caught it: property (position sizing)
Why it did not: the generator never produced a zero-equity account
Corrective action: equity=0 added to the generator's edge cases
```

A layer that keeps appearing in that field is a layer that needs work, and that
is the signal the metric exists to produce.

## 3. Root-cause analysis

For every serious defect, five questions — and the last one is the only one
that changes anything:

```
What happened?
Why wasn't it detected?
Why did the existing control fail?
Why did the design permit it?
What new control prevents recurrence?
```

Stopping at *"developer made a mistake"* is not an answer. The engineering
question is:

> **"Why could one mistake reach production?"**

### Worked example

Duplicate orders occurred.

```
Why?  Two execution requests were processed.
Why?  No idempotency key.
Why?  Executor assumed requests were serialized.
Why?  Architecture didn't enforce idempotency.
Why?  Requirement didn't specify duplicate-request behavior.
```

The corrective action is therefore not "be careful". It is:

```
idempotency requirement   (EXEC-001)
+ implementation          (src/execution/idempotency.py)
+ unit test
+ concurrency test        (RES-005)
+ regression test         (REG-####)
+ architecture rule       (INV-007)
```

Five artifacts, each independently checkable. That is what continuous
improvement looks like in a diff.

## 4. The loop

```
Production
    ↓
Metrics
    ↓
Defects
    ↓
Incidents
    ↓
Root cause
    ↓
Corrective action
    ↓
New test/control
    ↓
CI gate
    ↓
Release
    ↓
Production
```

Every failure should make the system harder to break again. A root-cause
analysis that produces no new registry entry has not closed the loop.

## 5. Where the numbers live

Metric collection is built in PR-006 (`GOV-007`). Until then this document is
the specification of what will be collected, not a claim that it is being
collected — the distinction the registry's `planned` status exists to make.
