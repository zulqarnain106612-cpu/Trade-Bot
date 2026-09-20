# Root-cause analysis template

`GOV-001`, `GOV-002`, `GOV-006`. Copy this file into the pull request that
fixes the defect. It is short on purpose: a template nobody finishes is worse
than five honest sentences.

The one rule: **stop at a control, not at a person.** "Developer made a
mistake" is never a root cause. The engineering question is

> Why could one mistake reach production?

---

## REG-#### / SEC-#### — one-line summary

**Severity:** SEV-1 / SEV-2 / SEV-3 (see `docs/security/INCIDENT_RESPONSE.md` §2)
**Detected:** how, and by whom or what
**Detected at:** ISO timestamp
**Started at:** ISO timestamp, if known — the gap between the two is MTTD

### 1. What happened?

What the system did, in terms an operator would recognise. Not the code path
yet.

### 2. Why wasn't it detected?

The layer that should have caught it: unit · property · integration ·
regression · E2E · gauntlet · paper · canary · monitoring.

**Name one.** This field is the input to the metric
`docs/quality/QUALITY_METRICS.md` §2 calls the most important one — *how many
defects escaped each verification layer* — and a layer that keeps appearing
here is a layer that needs work.

In the registry entry, record it as `layer: <name>` in `notes` so
`scripts/collect_quality_metrics.py` can count it.

### 3. Why did the existing control fail?

There usually *was* a control. It ran and returned the wrong answer, or it did
not run, or it ran somewhere the failure could not reach it. Which one?

### 4. Why did the design permit it?

The step that turns an incident into an improvement. A defect that was
possible is a design that allowed it.

### 5. What new control prevents recurrence?

Not "be careful". A control is something that fails the build. From the source
document's worked example, one defect produced five artifacts:

```
idempotency requirement   (EXEC-001)
+ implementation          (src/execution/idempotency.py)
+ unit test
+ concurrency test        (RES-005)
+ regression test         (REG-####)
+ architecture rule       (INV-007)
```

List yours.

---

## Closing checklist

An incident is closed when **all** of these exist — not when the symptom
stops. A symptom that stopped without a control being added will come back.

- [ ] A `REG-####` (defect) or `SEC-####` (security weakness) entry in
      `config/quality_registry.json`
- [ ] `notes` on that entry carrying `layer: <the layer from §2>`
- [ ] A permanent test named by that entry, which **fails against the pre-fix
      code** — verify this by stashing the fix and watching it go red
- [ ] The fix
- [ ] The entry at status `verified` and
      `python3 scripts/generate_quality_docs.py` re-run
- [ ] For a SEV-1: the credential-compromise steps in
      `docs/security/INCIDENT_RESPONSE.md` §3 completed and recorded

**The regression test is never deleted because the bug is fixed.** That is
what makes it a regression test rather than a bug report.
