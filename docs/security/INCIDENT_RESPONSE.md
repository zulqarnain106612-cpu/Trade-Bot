# Incident response

Aligned with NIST SP 800-61 Rev. 3, which is the current NIST incident-response
guidance and maps onto CSF 2.0.

## 1. Lifecycle

```
Prepare
   ↓
Detect
   ↓
Analyze
   ↓
Contain
   ↓
Eradicate
   ↓
Recover
   ↓
Learn
```

**Learn** is not optional and not the same as "closed". An incident is closed
when it has produced a registry entry and a test (§7).

## 2. Severity

| Severity | Definition | First action |
|---|---|---|
| **SEV-1** | Funds at risk, credentials exposed, or unauthorised code in production | Kill trading immediately, then analyse |
| **SEV-2** | A safety control is degraded or a security weakness is confirmed but unexploited | Halt the affected path; analyse before resuming |
| **SEV-3** | A control is weaker than intended, no evidence of exploitation | Normal triage; fix on the next release |

When in doubt between SEV-1 and SEV-2, act as SEV-1. Halting trading is cheap
and reversible; the alternative is not.

## 3. Credential-compromise runbook

Written before it is needed, because it must be executable under pressure.

```
1.  Kill trading
2.  Disable exchange API key
3.  Revoke sessions
4.  Revoke cloud credentials
5.  Revoke GitHub tokens if affected
6.  Preserve logs
7.  Preserve forensic evidence
8.  Determine exposure
9.  Rotate secrets
10. Rebuild from trusted artifact
11. Verify integrity
12. Reconcile exchange positions
13. Restore
14. Paper test
15. Resume only after approval
```

Notes on the steps that are easy to get wrong:

- **Step 1 before step 2.** Disabling the key first can leave open positions
  the bot can no longer manage.
- **Steps 6 and 7 before step 9.** Rotating a secret can destroy the evidence
  of how it was used.
- **Step 10 means the artifact, not the repository.** Rebuilding from source
  that may itself be compromised repeats the incident. Verify provenance
  (`SUP-004`).
- **Step 12 is where money is found or lost.** Reconcile the ledger against
  the exchange before believing any local position figure (`INV-009`).
- **Step 15 is a person, not a check.** No automation resumes live trading
  after a credential compromise.

## 4. GitHub-compromise runbook

If a maintainer's GitHub account is compromised:

```
revoke sessions/tokens
rotate credentials
review repository audit events
review workflow changes
review branch protection
review collaborators
review deploy keys
review Actions secrets
review releases
review artifacts
review dependencies
```

Then **rebuild production from a known-good commit** — one identified before
the compromise window, not merely one that looks fine.

Particular attention to workflow changes: a single added line in a workflow can
exfiltrate every Actions secret, and it will not look like an application
change in review.

## 5. Supply-chain-compromise runbook

If a dependency is compromised:

```
identify versions
↓
freeze deployments
↓
determine affected artifacts
↓
revoke/rotate secrets if code executed with them
↓
remove dependency
↓
rebuild
↓
test
↓
scan
↓
attest
↓
redeploy
```

The fourth step is the one people skip. If the compromised package ran in a job
that had a secret in its environment, that secret is compromised, whether or
not exfiltration can be proven. This is why least-privileged CI matters
(`SUP-001`): it decides how long that list is.

## 6. Continuous dependency surveillance

Do not only scan at deployment.

```
daily:     security alerts
weekly:    dependency review
monthly:   full dependency audit
on critical CVE: immediate emergency pipeline
```

(`SUP-005`.)

## 7. Closing an incident

An incident is closed when **all** of the following exist:

1. A root-cause analysis answering the five questions in
   `docs/quality/QUALITY_METRICS.md` §3, including which verification layer
   should have caught it.
2. A registry entry: `REG-####` for a defect, `SEC-####` for a security
   weakness.
3. A permanent test named by that entry, which fails against the pre-fix code.
4. The fix.
5. The registry entry at status `verified` and the traceability document
   regenerated.

Not closed when the symptom stops. A symptom that stopped without a control
being added will come back.

## 8. Contacts and escalation

Operational contact details are deliberately not in the repository. They live
with the operator, alongside the exchange account credentials and the
secret-manager access. What is recorded here is the obligation: **a SEV-1 has a
named human who can halt trading and disable the exchange key, reachable
without access to GitHub.** If GitHub is the compromise, GitHub is not the
channel.
