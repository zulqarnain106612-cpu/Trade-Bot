# Disaster recovery

This document exists to be read at 3am by somebody who did not write it. It is
ordered by what you do first, not by what is most interesting, and every
number in it is enforced by a test — so if the document and the code disagree,
the test suite is already red and you should trust neither until it is green.

Related: `docs/security/INCIDENT_RESPONSE.md` (security incidents),
`docs/quality/CI_GATE_ARCHITECTURE.md` (what the gates check).

## 0. The first decision

**Is the bot holding positions?**

Everything below branches on that, because the cost of being down is entirely
different in the two cases and the recovery-time objective says so:

| Book state | RTO | Why |
|---|---|---|
| Open positions | **15 minutes** | Nothing is managing the stops. An unmanaged position stops being an inconvenience quickly. |
| Flat | **8 hours** | The cost is opportunity, not risk. A fire drill here would be theatre. |

If you cannot tell within a minute or two, assume open positions.

## 1. Halt first, diagnose second

The kill switch is `POST /execution-mode` with `manual`, and it is durable —
`src/execution/mode_persistence.py` writes it before the audit record, so a
halt survives the restart that a supervisor is probably about to perform
(`EXEC-005`). Halting is cheap and reversible. Diagnosing while the strategy
keeps trading is neither.

If the API is unreachable, the same effect comes from stopping the process:
`src/security/../execution` opens no new positions without a running loop, and
the venue keeps the open ones exactly as they were.

## 2. What the system does on its own

`src/diagnostics/failsafe_policy.py` is the executable version of this table.
It is not a copy — the runtime asks that module, and
`tests/recovery/test_failsafe_policy.py` asserts every component has an entry.

| Component | Response | What that means |
|---|---|---|
| Price feed | **Halt all** | A stale price makes exits wrong too. Closing at a price that no longer exists is not a safe exit. |
| Risk engine | **Halt all** | It is the only thing that says no. |
| Order book stream | Halt new entries | Sizing becomes guesswork; exits still work on last trade. |
| Exchange status | Halt new entries | Not knowing whether the venue is in maintenance is not the same as knowing it is fine. |
| Model inference | Halt new entries | No model, no signal. Exits are rule-based. |
| Audit log | Halt new entries | Trading that is not recorded cannot be reconstructed, and the incident where that matters is the one where the log failed. |
| Exchange API | Retry 60s → halt all | A blip is seconds. Unbounded retries against a venue that is actually down is how duplicate orders appear on recovery. |
| Database | Retry 120s → halt all | State lives in memory too, so a short outage is survivable; a long one means fills stop being persisted. |
| Analytics | Degrade 1h | Observability, not control. |
| Intelligence feed | Degrade 30m | Lowers conviction; does not invalidate the model. |
| Self-tuning | Degrade 24h | Parameters stay where they are, which is a safe place for them. |
| API server | Degrade 15m | The operator loses visibility, which matters — but halting the strategy because a dashboard is down would itself be an incident. |

**An undeclared component halts everything.** Adding one to the enum without a
policy fails the suite rather than quietly degrading to "carry on".

## 3. Restarting: what to check before you unhalt

A restart reconstructs position state from local records. Those records can be
wrong in two opposite ways, and only the venue can tell you which:

- the process died **after** the venue accepted an order and **before** the
  local record existed → a real position nobody knows about, and every risk
  limit is being computed against a book that is missing it;
- the process died **after** the local record and **before** the venue
  accepted → a phantom position the system may try to "close".

So: **reconcile against the venue before unhalting** (`GET /debug/reconcile`).
`src/diagnostics/disaster_recovery.py` compares the two snapshots and reports
discrepancies. It deliberately does *not* correct them — an automated
correction against a venue that is merely slow to report is how a position
gets doubled. A human decides.

`tests/recovery/test_crash_replay.py` walks every stage of the order lifecycle
and asserts each one either reconciles clean or reports the discrepancy.

## 4. Restoring from backup

Objectives, from `src/diagnostics/recovery_objectives.py`:

| Data | RPO | Why |
|---|---|---|
| Trade records | **0** | A lost fill is a position the system does not know it holds. |
| Position state | **0** | Reconstructable from trade records, and worthless if those are gone too. |
| Audit trail | **0** | Its only job is to survive the incident that makes somebody read it. |
| Configuration | **0** | In version control. Stated anyway, so config drifting out of the repository shows as a gap. |
| Market history | 60s | Re-fetchable from the venue. Losing a minute costs a backfill. |
| Model artifacts | 24h | Reproducible from data and a training run. |

Backups are encrypted with `src/security/at_rest.py` (AES-256-GCM, versioned
envelope). Decryption failures all raise one exception type deliberately — see
that module's docstring before you write a loop that tries several keys.

### The drill

```
python3 -c "from src.diagnostics.recovery_objectives import *; \
    print(run_restore_drill(DataClass.TRADE_RECORDS, my_restore))"
```

`run_restore_drill` measures rather than asserts: it returns the elapsed time
and the age of the newest recovered datum, compares both against the
objectives, and reports a pass or a fail. A restore that throws is a **failed
drill**, not a crashed drill run.

Run it on a schedule. A backup nobody has restored is not a backup, and a
drill that needs a maintenance window is a drill that gets skipped.

## 5. Performance regressions

`config/performance_baselines.json` holds the budgets, and
`src/diagnostics/performance_baseline.py` compares a measured run against
them. Percentiles rather than means, because a mean hides the tail that
actually hurts: one request in a hundred taking two seconds is invisible in an
average and fatal in a trading loop.

The tolerance is 25%. Wide enough to survive a noisy shared runner, narrow
enough that an accidental O(n²) cannot hide in it. An operation with no
declared budget raises rather than passing — silence there would make every
new hot path free.

## 6. What this document does not cover

- **Killing real infrastructure.** The chaos suite here exercises the
  *decisions* (what the system does when the database dies), not the
  infrastructure. Actually killing a database belongs in the nightly job.
- **Venue-side incidents.** If the exchange itself is wrong about your
  positions, no amount of local recovery helps; that is a support ticket and a
  halt, in that order.
- **The security path.** A compromise is not an outage. See
  `docs/security/INCIDENT_RESPONSE.md` §3 — the ordering there is deliberate,
  and rotating keys first destroys the evidence of how they were used.
