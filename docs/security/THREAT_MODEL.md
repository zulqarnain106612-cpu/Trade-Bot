# Threat model

## 1. The premise

> **An attacker who gains control of the application, API credentials, GitHub
> workflow, cloud account, database, or exchange API can potentially cause
> financial loss.**

That sentence is what makes this project different from most web applications.
A vulnerability here is not a data-breach risk in the abstract; it is a path to
someone else's money. So the security boundary is not the application. It is:

```
Developer workstation
        ↓
GitHub
        ↓
CI/CD
        ↓
Container/artifact
        ↓
Cloud/server
        ↓
Database
        ↓
Secrets
        ↓
Exchange API
        ↓
Trading account
```

Every link is in scope. `docs/security/SECURITY_POLICY.md` §2 separates the
links the repository can enforce from the links that must be verified in the
deployment environment.

## 2. Attackers modelled

### A. Internet attacker

Reaches the public surface and attempts:

```
API exploitation
credential attacks
injection
DoS
authentication bypass
authorization bypass
```

Controls: `API-001`…`API-009`, `SECR-006`. Layered defence in §4.

### B. Credential thief

Obtains one of:

```
exchange API key
database password
JWT/session token
cloud credential
GitHub token
```

Controls: `SECR-001` (secrets never reach logs or images), `SECR-003`
(environment separation and rotation), `SECR-010` (the key cannot withdraw),
`REL-007` (behavioural detection — the thief uses *valid* credentials, so
authentication will not notice them).

Runbook: `docs/security/INCIDENT_RESPONSE.md` §3.

### C. Malicious dependency

A compromised Python or npm package attempts:

```
credential theft
code execution
data exfiltration
CI compromise
```

Controls: `SUP-005` (surveillance), `SUP-007` (an update cannot deploy itself),
`SUP-001` (least-privilege CI limits what executed code can reach),
`SUP-004` (provenance makes "what actually shipped" answerable).

Runbook: `docs/security/INCIDENT_RESPONSE.md` §5.

### D. Compromised developer account

Attacker gains GitHub account access and attempts:

```
malicious PR
workflow modification
secret extraction
release modification
```

Controls: `GOV-008` (protected `main`, required review, no bypass), `SUP-003`
(PR jobs cannot see production secrets), `REL-002` (deployment requires
environment approval, which a merge does not confer).

Runbook: `docs/security/INCIDENT_RESPONSE.md` §4.

### E. Supply-chain attacker

Attempts to compromise:

```
GitHub Action
Docker image
PyPI package
npm dependency
base image
build artifact
```

Controls: `SUP-002` (SHA-pinned actions), `SUP-004` (SBOM, hash, attestation),
`SUP-006` (minimal, non-root images), `SUP-005`.

### F. Exchange/API attacker

Attempts to manipulate:

```
order responses
market data
authentication
websocket messages
```

Controls: `SECR-006` (TLS verified, never disabled), `EXEC-003` (responses
parsed against a contract, no silent defaults), `API-005` (a WebSocket message
is never trusted because the connection is established), `DATA-001` (data
quality gate), `SECR-005` (signature and replay checks).

### G. Insider

Has legitimate access and abuses it.

Controls: `SECR-004` (append-only audit hash chain — modification of an old
event breaks the chain), `GOV-008` (no unreviewed critical change), `REL-002`
(deployment approval is a second person), `REL-007` (behavioural detection does
not care whose credentials were used).

## 3. Assets, ranked

1. **Exchange API credentials and live-trading authorization.** The crown
   jewels. Everything else is a route to these.
2. **The production host and its ability to deploy code.** Arbitrary code in
   production is equivalent to holding the credentials.
3. **The GitHub repository and its Actions secrets.** Same, one step earlier.
4. **The audit trail.** Not because it prevents loss, but because without its
   integrity nothing after an incident can be established.
5. **Trading models and parameters.** Economically valuable and, if altered,
   a way to lose money that looks like bad luck.
6. **Market and on-chain data.** Integrity matters more than confidentiality:
   corrupted input produces bad orders.

## 4. Layered defence

```
WAF / reverse proxy
        ↓
network firewall
        ↓
TLS
        ↓
authentication
        ↓
authorization
        ↓
input validation
        ↓
rate limiting
        ↓
application security
        ↓
database least privilege
        ↓
secret isolation
        ↓
exchange key restrictions
        ↓
audit
        ↓
monitoring
        ↓
alerting
        ↓
kill switch
```

**No single layer is trusted.** The design assumption is that any one of them
will fail, and the question asked of each control is what the layer below it
does when that happens.

## 5. The security boundary around the money

```
                       INTERNET
                           │
                           ▼
                    ┌─────────────┐
                    │ WAF / TLS   │
                    └──────┬──────┘
                           │
                           ▼
                    ┌─────────────┐
                    │ API AUTH     │
                    └──────┬──────┘
                           │
                           ▼
                    ┌─────────────┐
                    │ AUTHZ/RATE   │
                    └──────┬──────┘
                           │
                           ▼
                    ┌─────────────┐
                    │ TRADE BOT    │
                    └──────┬──────┘
                           │
                ┌──────────┴──────────┐
                ▼                     ▼
          ┌───────────┐        ┌───────────┐
          │ RISK      │        │ SIGNAL    │
          │ ENGINE    │        │ ENGINE    │
          └─────┬─────┘        └─────┬─────┘
                │                     │
                └──────────┬──────────┘
                           ▼
                    ┌─────────────┐
                    │ SAFETY GATE │
                    └──────┬──────┘
                           │
                           ▼
                    ┌─────────────┐
                    │ EXECUTION   │
                    └──────┬──────┘
                           │
                           ▼
                    ┌─────────────┐
                    │ EXCHANGE    │
                    └──────┬──────┘
                           │
                     RESTRICTED KEY
                           │
                     ┌─────▼─────┐
                     │ NO WITHDRAW│
                     └───────────┘
```

And, independently of the trading path:

```
                    SECURITY MONITOR
                           │
             ┌─────────────┼─────────────┐
             ▼             ▼             ▼
          GitHub        Server        Exchange
             │             │             │
             └─────────────┼─────────────┘
                           ▼
                         ALERT
                           ▼
                       KILL SWITCH
```

The monitor is drawn separately on purpose. A monitor that depends on the
system it watches reports "healthy" for exactly as long as the outage lasts.

## 6. The last line of defence is the exchange

Application security alone **cannot** guarantee protection of trading funds.
Configure the exchange account so that even a fully compromised Trade-Bot is
bounded:

```
withdrawals disabled
IP restricted
permissions minimized
position limits
subaccount isolation
```

Then:

```
Attacker
   ↓
Trade-Bot
   ↓
Exchange API
   ↓
❌ withdrawal unavailable
```

The attacker can still lose money by trading badly. They cannot take it. That
difference is the single highest-value control in this document, and it is
configured at the exchange, not in this repository (`SECR-010`).

## 7. The standard this model is held to

A compromised Trade-Bot instance must be:

- **unable to withdraw funds**,
- **unable to bypass deterministic risk controls**,
- **unable to silently change live configuration**,
- **unable to deploy arbitrary code to production**.

That is the definition of maximum practical assurance used here. It is
deliberately not a claim of zero vulnerabilities; NIST frames secure
development the same way — reducing likelihood and impact, not promising
elimination.

## 8. Gap check

The categories a "testing plan" commonly misses, and where each is covered:

Unit · component · integration · contract · API · E2E · regression ·
verification · validation · acceptance · property-based · fuzzing · mutation ·
feature · signal · model · model leakage · lookahead · CPCV · backtest ·
backtest/live parity · risk invariants · order FSM · exchange failure ·
reconciliation · crash recovery · disaster recovery · chaos · concurrency ·
performance · load · data quality · time/clock · frontend · authentication ·
authorization · IDOR · injection · SSRF · WebSocket security · rate limiting ·
secret scanning · credential isolation · key rotation · cryptographic controls ·
TLS · audit-log integrity · encryption at rest and in transit · dependency
security · SBOM · artifact integrity · artifact provenance · Actions hardening ·
OIDC · supply chain · container security · network segmentation · firewalling ·
production isolation · backup security · incident response ·
credential-compromise response · continuous improvement · root-cause analysis ·
security regression · quality metrics · requirements traceability · protected
branches · protected environments · paper qualification · canary deployment ·
automatic halt · rollback · external attacker detection · exchange-side
blast-radius reduction.

Each maps to at least one entry in `config/quality_registry.json`; the mapping
by source section is in `docs/quality/IMPLEMENTATION_PLAN.md` §4.
