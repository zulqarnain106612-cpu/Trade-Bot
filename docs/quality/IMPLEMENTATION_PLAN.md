# Implementation plan and branch mapping

The source document (`docs/Quality-Engineering`) is a 90-section quality and
security engineering programme. This file is the mapping from that document to
branches, pull requests and registry ids, so that "implement the whole thing"
is a finite, checkable sequence rather than an intention.

## 1. Why a sequence and not one PR

The source document is explicit: do not implement all of this in one giant PR.
The specific danger for a trading system is **improving quality while
accidentally changing trading behaviour**. A sequence of small, reviewable PRs
keeps behavioural change visible; PR-001 deliberately changes none.

## 2. The sequence

```
PR-001  Quality/Security Foundation
   ↓
PR-002  Risk Invariants + Boundary Tests
   ↓
PR-003  Signal/Feature Verification
   ↓
PR-004  Model/Leakage Verification
   ↓
PR-005  Execution/FSM/Exchange Contracts
   ↓
PR-006  Regression + Property Testing
   ↓
PR-007  API/WebSocket Security
   ↓
PR-008  Cryptographic/Secret Architecture
   ↓
PR-009  Supply-Chain + Artifact Security
   ↓
PR-010  Recovery/Chaos/Performance
   ↓
PR-011  Paper-Trading Qualification
   ↓
PR-012  Production/Canary Security Gate
```

The chain is stacked: each branch is cut from the previous one, because every
phase after the first edits `config/quality_registry.json` and regenerates
`docs/quality/REQUIREMENTS_TRACEABILITY.md`. Cutting all twelve from `main`
would guarantee a conflict in those two files on every merge.

## 3. Branch mapping

| PR | Branch | Phase title | Registry ids it closes |
|---|---|---|---|
| PR-001 | `claude/qe-001-foundation` | Quality/Security Foundation | `GOV-001`, `GOV-002`, `GOV-005`, `GOV-008` |
| PR-002 | `claude/qe-002-risk-invariants` | Risk Invariants + Boundary Tests | `INV-001`, `INV-002`, `INV-004`, `INV-006`, `RISK-001`…`RISK-004`, `PORT-001` |
| PR-003 | `claude/qe-003-signal-feature-verification` | Signal/Feature Verification | `INV-008`, `DATA-001`…`DATA-004`, `SIG-001`, `SIG-002` |
| PR-004 | `claude/qe-004-model-leakage-verification` | Model/Leakage Verification | `MODL-001`…`MODL-007` |
| PR-005 | `claude/qe-005-execution-fsm-contracts` | Execution/FSM/Exchange Contracts | `INV-003`, `INV-005`, `INV-007`, `EXEC-001`…`EXEC-004`, `EXEC-006`, `DATA-005` |
| PR-006 | `claude/qe-006-regression-property` | Regression + Property Testing | `RISK-006`, `EXEC-007`, `SIG-003`, `RES-008`, `GOV-003`, `GOV-004`, `GOV-006`, `GOV-007` |
| PR-007 | `claude/qe-007-api-ws-security` | API/WebSocket Security | `EXEC-005`, `API-001`…`API-009` |
| PR-008 | `claude/qe-008-crypto-secret-architecture` | Cryptographic/Secret Architecture | `SECR-001`…`SECR-010` |
| PR-009 | `claude/qe-009-supply-chain-artifact` | Supply-Chain + Artifact Security | `SUP-001`…`SUP-007` |
| PR-010 | `claude/qe-010-recovery-chaos-performance` | Recovery/Chaos/Performance | `INV-009`, `RES-001`…`RES-007` |
| PR-011 | `claude/qe-011-paper-qualification` | Paper-Trading Qualification | `INV-010`, `RISK-005`, `REL-001` |
| PR-012 | `claude/qe-012-production-canary-gate` | Production/Canary Security Gate | `REL-002`…`REL-008`, `GOV-009`, `GOV-010` |

The authoritative, always-current version of the right-hand column is the
"Outstanding work by phase" table in
`docs/quality/REQUIREMENTS_TRACEABILITY.md`, generated from the registry.

## 4. Source-section coverage

Every numbered section of `docs/Quality-Engineering` lands somewhere. The
`source` field on each registry entry cites the sections it comes from
(`QE-42` is section 42), so this table and the registry cannot drift apart
silently.

| Sections | Subject | Delivered by |
|---|---|---|
| 1–5 | Objective, quality architecture, test matrix, V&V, traceability | PR-001 |
| 6–7 | Security model, threat model | PR-001 (`docs/security/THREAT_MODEL.md`) |
| 8, 12 | Cryptographic layer, cryptographic testing | PR-008 |
| 9–11, 13 | Secrets architecture, exchange keys, key rotation, encryption at rest | PR-008 (`docs/security/KEY_MANAGEMENT.md` in PR-001) |
| 14–15 | Encryption in transit, certificate and TLS testing | PR-008 |
| 16–17 | Audit-log integrity, log-based secret leakage | PR-008 |
| 18–24 | API security, authorization matrix, IDOR, injection, SSRF, WebSocket, rate limiting | PR-007 |
| 25 | Kill-switch security | PR-007 |
| 26 | Fail-safe architecture | PR-010 |
| 27–30 | Dependency security, Actions hardening, PR secrets, fork attacks | PR-009 |
| 31–36 | Deployment separation, isolation, segmentation, firewall, SSH, containers | PR-009 and `docs/security/SECURITY_POLICY.md` §2 (environment-side) |
| 37 | Artifact security | PR-009 |
| 38–41 | Database security, backups, disaster recovery, RTO/RPO | PR-010 |
| 42 | Trading invariants | PR-002, PR-003, PR-005, PR-010, PR-011 |
| 43 | Signal verification | PR-003 |
| 44–47 | Model verification, model safety, drift, lookahead | PR-004 |
| 48 | Regression engineering | PR-001 (registry) and PR-006 (suite) |
| 49–53 | Mutation, property, fuzz, concurrency, chaos | PR-006 and PR-010 |
| 54–55 | Performance, security-performance interaction | PR-010 |
| 56–59 | Continuous improvement, RCA, five whys, metrics | PR-001 (policy) and PR-006 (mechanism) |
| 60–62 | Quality gates, workflow architecture, production gate | PR-001 (architecture) and PR-009/PR-012 (workflows) |
| 63–64 | Canary, automatic halt | PR-012 |
| 65–67 | Attacker detection, suspicious trading behaviour, exchange-side controls | PR-012 and PR-008 |
| 68–71 | Incident response and the three runbooks | PR-001 (`docs/security/INCIDENT_RESPONSE.md`) |
| 72–73 | Dependency surveillance, security regression tests | PR-009, PR-001 (registry) |
| 74–76 | Configuration drift, startup self-test, no silent degradation | PR-012, PR-006 |
| 77–79 | Type safety, money arithmetic, time correctness | PR-003 |
| 80 | Data-quality gates | PR-003 |
| 81–82 | Backtest integrity, backtest/live parity | PR-011 |
| 83–84 | ML research firewall, self-tuning firewall | PR-004, PR-011 |
| 85 | Quality maturity levels | PR-001 (policy), PR-012 (claim) |
| 86 | Repository roadmap | This file |
| 87–88 | Production ready, what GitHub enforces | PR-001 (policy), PR-012 (enforcement) |
| 89–90 | Security boundary, gap check | `docs/security/THREAT_MODEL.md` |

## 5. What each PR delivers

### PR-001 — Quality/Security Foundation *(this PR)*

Establishes the policy, test taxonomy, CI gate architecture, GitHub security
checklist, threat model, requirements traceability and the critical trading
invariants **without changing trading behaviour**. No file under `src/` other
than the new `src/quality/` package is modified.

- `config/quality_registry.json` + `config/quality_registry.schema.json`
- `src/quality/registry.py` — strict loader
- `scripts/generate_quality_docs.py` — generator with `--check`
- `docs/quality/` — policy, baseline, test strategy, CI gate architecture,
  metrics, this plan, and the generated traceability matrix
- `docs/security/` — threat model, security policy, incident response, key
  management, GitHub security checklist
- `tests/quality/` — loader and doc-sync tests
- `ci.yml` gains one step: the registry is valid and its document is in sync

### PR-002 — Risk Invariants + Boundary Tests

Invariant suite under `tests/trading/invariants/`, boundary tests at every
configured limit, property tests over sizing and probability ranges, and
deliberate failure injection into the risk engine so `INV-006` is decided
rather than assumed.

### PR-003 — Signal/Feature Verification

Golden signal fixtures, feature determinism, the data-quality gate's staleness
and monotonicity properties, the clock policy, and the money-representation
decision written down and enforced.

### PR-004 — Model/Leakage Verification

Artifact provenance, round-trip and reproduce, the lookahead gate, purged
cross-validation, drift demotion, and the research firewall.

### PR-005 — Execution/FSM/Exchange Contracts

Exchange-response contract tests, total order FSM, unknown-status handling,
idempotency end to end, partial fills and fees against hand-calculated
fixtures, and venue precision rules.

### PR-006 — Regression + Property Testing

The `REG-####` regression suite, generalised property tests, the fuzz corpus,
nightly mutation testing with per-subsystem thresholds, the no-silent-
degradation static rules, the RCA template and the metrics collection.

### PR-007 — API/WebSocket Security

The executable authorization matrix, IDOR, injection, SSRF, WebSocket message
security, rate limiting, security headers, error hygiene, and the kill switch's
authentication, audit, idempotence and durability.

### PR-008 — Cryptographic/Secret Architecture

`src/security/` completed: secrets, crypto, signing, key rotation, secure
compare, audit integrity. Log-leak provocation tests, TLS verification tests,
CSPRNG assertions, and the documented exchange-key posture.

### PR-009 — Supply-Chain + Artifact Security

Dependabot, dependency review, SBOM, artifact attestation, action pinning
assertions, least-privilege permission assertions, OIDC, container scanning,
and the fork-secret test. Creates `release.yml`.

### PR-010 — Recovery/Chaos/Performance

Fail-safe policy table made executable, crash replay, database and exchange
recovery, restore drills, RTO/RPO, concurrency races, chaos suite, performance
baselines. Creates `docs/operations/DISASTER_RECOVERY.md` and
`nightly-quality.yml`.

### PR-011 — Paper-Trading Qualification

Backtest integrity against hand-calculated fixtures, backtest/live parity, the
paper qualification criteria, and the self-tuning firewall.

### PR-012 — Production/Canary Security Gate

The protected production environment, canary exposure, automatic halt triggers,
the startup self-test, configuration-drift detection, the behavioural security
layer, rollback, and the Level 5 maturity claim per path.

## 6. Exit criteria

A phase is complete when:

1. Every registry id in its row of §3 has status `verified`, with no
   `planned_in`.
2. `python3 scripts/generate_quality_docs.py --check` passes.
3. `pytest -q` passes with the repo-wide 99% coverage gate and the per-file
   floor.
4. Every workflow the phase added or changed has a `gate` job covering every
   job in it.
5. `docs/quality/QUALITY_BASELINE.md` §3 is updated for the subsystems the
   phase moved.

The programme is complete when no registry entry has status `planned` or
`partial`, and nothing has been waived that §6 of
`docs/quality/QUALITY_POLICY.md` forbids waiving.
