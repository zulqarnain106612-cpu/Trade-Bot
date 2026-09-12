# Production readiness

## The claim this document refuses to make

"We're at 87%."

That number is an average over things that are not commensurable. A missing
dashboard and a missing kill switch move it by the same point, and the second
one is the entire difference between a bad week and an empty account. Any
readiness figure above zero and below one hundred is a way of saying "not
ready" while sounding like progress.

So readiness here is a **conjunction** (`GOV-010`). Each condition below is
individually sufficient to say no, and the answer is a list of blockers rather
than a score:

1. every **critical** registry entry is `verified` — not partial, not
   planned, not waived;
2. no critical entry is an accepted gap;
3. every entry claiming a test names one that exists on disk;
4. nothing is still `planned` for a phase that has already shipped;
5. `REQUIREMENTS_TRACEABILITY.md` is in sync with the registry.

```bash
python3 scripts/check_production_readiness.py
```

Exit `0` ready, `1` not ready, `2` **could not evaluate**. The third is
deliberately distinct: a checker that cannot run has not said yes, and
collapsing it into "not ready" hides a broken gate behind a failing one. The
deploy workflow runs this in preflight, before anything reaches an
environment.

## What Level 5 means here (`GOV-009`)

A maturity level is a set of artifacts somebody can open, not an adjective in
a status report. This is the list, and `tests/production/test_production_gate.py`
asserts each file exists:

| Capability | Artifact |
|---|---|
| Supply-chain security | `scripts/check_supply_chain.py` |
| Artifact provenance | `scripts/write_provenance.py`, `.github/workflows/release.yml` |
| Cryptographic controls | `src/security/` (at-rest, signing, key lifecycle, TLS, randomness) |
| Continuous threat modelling | `docs/security/THREAT_MODEL.md` |
| Recovery | `docs/operations/DISASTER_RECOVERY.md`, `src/diagnostics/recovery_objectives.py` |
| Traceability | `config/quality_registry.json` → `docs/quality/REQUIREMENTS_TRACEABILITY.md` |
| The conjunction | `scripts/check_production_readiness.py` |

The registry is the load-bearing one. Everything else in this table can be
faked by creating a file; the registry cannot, because the loader stats every
test path a claim names and refuses to load when one is missing.

## The production path

```
Release (tag or dispatch)          Deploy (dispatch only)
  build + SBOM + attest      →       preflight: readiness, supply chain, provenance
  verify from the artifact           canary: protected environment, ≤25% exposure
                                     promote: separate environment, second approval
                                     rollback: the same path, exercised as a drill
```

Four properties, each invisible at runtime and therefore tested as
configuration:

- **Production is a protected GitHub environment** (`REL-002`). The
  `environment:` key is what makes GitHub demand approval and restrict which
  branches may deploy. A workflow without it deploys perfectly well — right up
  to the day it deploys something nobody approved. One-time setup:
  `docs/security/GITHUB_SECURITY_CHECKLIST.md`.
- **Canary precedes full exposure** (`REL-003`), bounded at 25%. A canary at
  full size is a deployment with a different name.
- **Rollback is a job, not a paragraph** (`REL-008`), exercised by the `drill`
  input. A rollback path that has never been taken is a hypothesis, and the
  moment you need it is the worst moment to test it.
- **Nothing automatic can deploy** (`SUP-007`). `workflow_dispatch` only: a
  merged dependency bump must not be able to reach production.

## While it is running

Three layers, each catching what the others cannot:

| Layer | Module | Catches |
|---|---|---|
| Halt triggers | `src/diagnostics/halt_triggers.py` | Conditions where a human would stop the bot, if they were watching — a position that appeared from nowhere, a frozen feed, a reconciliation failure. They arrive at 4am; the worst are silent. |
| Startup self-test | `src/diagnostics/startup_selftest.py` | Configuration incidents: debug left on, a testnet endpoint in production, `20` where `0.20` was meant. None of them raise; all are checkable in two seconds. |
| Behavioural guard | `src/diagnostics/behavioural_guard.py` | Legal orders in an illegal *pattern* — stolen credentials used correctly, a model that has started doing something else, a retry loop resubmitting. |

Two conventions run through all three, and both are the opposite of the
obvious choice:

- **An unevaluable check fires.** If the data a check needs is missing, that
  is not a pass. It is the moment the system knows least about its own state,
  which is the worst possible moment to assume everything is fine.
- **Every condition is evaluated, never short-circuited.** A compound incident
  must resolve to the strictest response, and an operator needs the whole list
  rather than whichever condition happened to be checked first.

## What this does not claim

- **The deployment steps are placeholders.** This repository has no target
  infrastructure yet. Inventing one would produce a workflow that cannot run
  rather than one that has not run; what is real is everything around the
  deploy step — the gates, the environments, the promotion condition, the
  rollback job.
- **The key posture is a declaration** (`SECR-010`). Nothing in this process
  can prove what the key on the venue's side may do. The verification is a
  human step in `docs/security/KEY_MANAGEMENT.md`.
- **Chaos tests exercise decisions, not infrastructure.** Killing a real
  database belongs in the nightly job; deciding what to do when one dies
  belongs in the unit suite.
