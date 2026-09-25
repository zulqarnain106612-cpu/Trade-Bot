# Compliance checklist

**This document is a skeleton, not legal advice.** Trading crypto for
own-account, for clients, or via a hosted service carries jurisdiction-
specific obligations that only a licensed lawyer / compliance officer
in your jurisdiction can answer. Fill this out with them before going
live.

## Threshold questions

- [ ] **Whose money is being traded?** Own-account only, or on behalf of
      others? The latter typically triggers licensing (MSB / VASP /
      broker-dealer / MiCA CASP / equivalents).
- [ ] **What jurisdictions are the operator, servers, and exchange
      accounts in?** All three matter and can each impose separate
      obligations.
- [ ] **Are any counterparties or funding sources subject to sanctions
      screening?** OFAC / EU / UK / UN lists must be checked against
      every counterparty for a business operation.

## Common obligations (verify per jurisdiction)

- [ ] **Registration / licensing** — MSB registration (US FinCEN),
      VASP registration (EU MiCA CASP once fully in force), FCA
      registration (UK), equivalents elsewhere. Trading own money at
      small scale is often exempt; scale changes that.
- [ ] **KYC / AML** if serving others: identity verification, source of
      funds, ongoing monitoring, SAR filing thresholds.
- [ ] **Tax reporting** — realized PnL, wash-sale rules if applicable
      (US crypto historically not covered, watch for change).
      Trade-Bot's `logs/AUTOMATED_DECISION_LOG.md` and the storage
      backend's `trades` table are the record. Export formats and
      retention need policy.
- [ ] **Record retention** — typical 5–7 years for regulated activity.
      Ensure logs, decision log, and DB backups are retained
      accordingly, and that key rotation
      ([KEY_ROTATION.md](KEY_ROTATION.md)) does not destroy audit
      history.
- [ ] **Market abuse** — algorithmic trading rules (spoofing, layering,
      wash trading) apply on regulated venues and increasingly on
      crypto venues. The kill-switches and gates in
      [src/risk/](../../src/risk/) are your defence; document them.
- [ ] **Consumer protection / disclosure** if any external users.

## Documentation to produce (with counsel)

- [ ] Statement of who owns the system, the funds, and the risk.
- [ ] Risk disclosure appropriate for the jurisdiction.
- [ ] Data-handling notice covering exchange API keys and any PII
      (there is minimal PII in this codebase by design — confirm).
- [ ] Incident-notification policy: what triggers a regulator or
      exchange notification, and who signs off.

## Sign-off

Do not proceed to live trading until:

- [ ] Named counsel has reviewed and approved the operational plan.
- [ ] Named compliance officer (may be same person for small ops) has
      signed off on this checklist.
- [ ] Signature and date recorded outside this repo, in
      version-controlled corporate records.

This file is intentionally minimal because the wrong template is worse
than none. Do the work with a real professional.
