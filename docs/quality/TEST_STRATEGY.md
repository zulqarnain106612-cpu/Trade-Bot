# Test strategy

## 1. "Test" is not one thing

Every test in this repository answers exactly one question. The taxonomy exists
so that a gap is visible as a gap, rather than hidden inside a large number of
tests that all answer the same easy question.

| Test type | Main question |
|---|---|
| Unit | Does this function work? |
| Component | Does this subsystem work? |
| Contract | Does an interface behave as promised? |
| Integration | Do components work together? |
| API | Does the HTTP surface behave and refuse correctly? |
| E2E | Does the complete system work? |
| Regression | Did an old bug come back? |
| Verification | Does the implementation satisfy the requirement? |
| Validation | Does the system solve the intended problem? |
| Property | Does a general invariant always hold? |
| Fuzz | What happens with unexpected input? |
| Security | Can an attacker violate a security property? |
| Model | Is ML inference or training correct? |
| Signal | Does the strategy generate the intended signal? |
| Risk | Can an unsafe trade pass? |
| Execution | Does order state remain correct? |
| Performance | Is it fast and stable enough? |
| Resilience | Does it survive failures? |
| Recovery | Can it restore correct state? |
| Chaos | Does it fail safely under disruption? |
| Mutation | Would the suite notice if the logic were subtly wrong? |

This table is not decorative. It is the `test_types` vocabulary in
`config/quality_registry.json`, and the loader refuses a verification entry
whose `test_type` is not one of these. A test that cannot be classified is a
test whose purpose has not been decided.

## 2. Target directory taxonomy

```
tests/
│
├── unit/
├── component/
├── contract/
├── integration/
├── api/
├── e2e/
│
├── features/
├── signals/
├── models/
├── regime/
│
├── risk/
├── execution/
├── portfolio/
│
├── regression/
├── property/
├── fuzz/
│
├── security/
├── authentication/
├── authorization/
├── cryptography/
├── secrets/
├── supply_chain/
│
├── resilience/
├── recovery/
├── chaos/
│
├── performance/
├── load/
│
├── backtest/
├── validation/
├── paper/
│
├── trading/
│   ├── invariants/
│   ├── scenarios/
│   ├── crash_replays/
│   └── market_regimes/
│
└── fixtures/
    ├── market/
    ├── exchange/
    ├── models/
    ├── api/
    └── security/
```

## 3. Migration rule: classify before moving

**Do not bulk-move the existing 324 test files.** A mass rename produces an
enormous diff that no one can review, breaks every `config/quality_registry.json`
verification path at once, and proves nothing about coverage.

The rule each phase follows:

1. **Classify.** For the requirements that phase owns, decide which existing
   tests actually verify them and with which `test_type`. Record that in the
   registry as `partial`.
2. **Identify the gap.** What the statement claims and the existing tests do
   not decide is the work of the phase.
3. **Write the new tests in their taxonomy directory.** New tests are born in
   the right place: `tests/risk/`, `tests/property/`, `tests/security/` and so
   on.
4. **Move an old test only when it is touched anyway**, and then only with its
   registry path updated in the same commit. The loader will fail the build if
   the two disagree, which is the safety net that makes gradual migration safe.

Directories are created by the phase that first needs them, not up front. An
empty directory is a claim that something is tested there.

## 4. Where each phase writes

| Phase | New directories |
|---|---|
| PR-001 | `tests/quality/` |
| PR-002 | `tests/risk/`, `tests/portfolio/`, `tests/trading/invariants/` |
| PR-003 | `tests/component/`, `tests/features/`, `tests/signals/`, `tests/fixtures/signals/` |
| PR-004 | `tests/models/`, `tests/validation/`, `tests/fixtures/models/` |
| PR-005 | `tests/execution/`, `tests/contract/`, `tests/fixtures/exchange/` |
| PR-006 | `tests/fuzz/`, `tests/regression/` (`tests/property/` from PR-002) |
| PR-007 | `tests/api/`, `tests/authentication/`, `tests/authorization/`, `tests/fixtures/api/` |
| PR-008 | `tests/cryptography/`, `tests/secrets/`, `tests/fixtures/security/` |
| PR-009 | `tests/supply_chain/` |
| PR-010 | `tests/resilience/`, `tests/recovery/`, `tests/chaos/`, `tests/performance/`, `tests/trading/crash_replays/` |
| PR-011 | `tests/paper/`, `tests/backtest/`, `tests/trading/scenarios/`, `tests/trading/market_regimes/` |
| PR-012 | `tests/e2e/` |

## 5. What each test type is expected to do here

### Unit and component

The bulk of what exists today. A unit test that only asserts "the function
returned something" is not counted as verification of a requirement; the
registry entry stays `partial` until a test decides the statement.

### Contract

Applies to every boundary the project does not own: exchange REST responses,
WebSocket frames, on-chain providers, the database schema. A contract test
asserts the parse is total — unknown, missing and malformed fields produce an
explicit failure, never a silent default (`EXEC-003`).

### Property

Generated inputs against an invariant. The properties this project cares about
most:

```
position_size >= 0
position_size <= limit
risk <= ceiling
probabilities ∈ [0,1]
invalid inputs never produce executable orders
```

Thousands of generated combinations find the edge case the hand-written test
did not imagine.

### Fuzz

API payloads, market data, exchange responses, WebSocket messages,
configuration, model inputs and serialised state. The passing condition is not
"the application did not crash". It is:

```
malformed input → rejected safely → no corrupted state
    → no unauthorized action → no secret leakage → audit event
```

### Security

Written from the attacker's side: authentication, authorization, session
handling, input validation, output encoding, injection, security headers, error
handling, rate limiting, logging and API abuse, per OWASP ASVS. Every finding
becomes a permanent `SEC-####` entry.

### Signal

Golden datasets under `tests/fixtures/signals/`, one JSON per case:

```
tests/fixtures/signals/
    BTCUSDT_15m_case_001.json
    BTCUSDT_15m_case_002.json
    ETHUSDT_15m_case_001.json
```

Each carries market input, features, regime, model outputs, signal, risk
decision and expected final action, so CI detects a behavioural change rather
than only a crash.

### Model

Load, predict, serialise, deserialise, reproduce. Plus the leakage checks:
lookahead (`MODL-003`) and purged cross-validation (`MODL-007`).

### Mutation

Nightly, on the critical subsystems — risk, execution, signals, position
sizing, the order FSM. Targets:

```
Risk:       ≥ 90%
Execution:  ≥ 90%
Signal:     ≥ 85%
General:    ≥ 80%
```

These are this project's engineering targets, not universal standards. They
exist because **a high coverage number with a poor mutation score is weak
evidence.**

### Chaos and recovery

Kill the database, kill the WebSocket, delay the exchange, drop packets, return
500s and malformed bodies, restart the process, fill an order during the
restart. The expected outcome is `SAFE STATE`, not `PROCESS DIDN'T CRASH`.

### Performance

Baselines with p50, p95, p99, error rate, CPU and memory for feature
generation, signal generation, model inference, risk evaluation, order
processing, database, API and WebSocket. A PR that makes a critical path
dramatically slower fails or escalates.

## 6. Fixtures

`tests/fixtures/` holds data, never logic. A fixture that computes an expected
value has stopped being a fixture and become a second implementation of the
thing under test — the exact failure mode that makes a backtester agree with
itself and with nothing else. Expected values in trading fixtures are
hand-calculated and the calculation is written down in the fixture file.

## 7. What a test may never do

- **Disable TLS verification to make an integration test pass.** If the
  integration needs it off, the integration is wrong (`SECR-006`).
- **Print or log a credential**, including in a failure message (`SECR-001`).
- **Assert on a mock's call count as a substitute for asserting on behaviour**,
  where the behaviour is observable.
- **Be deleted because the bug it covers is fixed.** Regression tests are
  permanent (`GOV-001`, `GOV-002`).
- **Skip silently in CI.** A skipped test in a required job is a green check
  that verified nothing; 92 such skips were once hiding the entire storage
  backend, which is why CI now runs a TimescaleDB service.
