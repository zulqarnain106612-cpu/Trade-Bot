# Security policy

This is the engineering policy. For coordinated vulnerability disclosure, see
`SECURITY.md` at the repository root.

## 1. Baseline

| Standard | Applied to |
|---|---|
| NIST SSDF (SP 800-218) | The development lifecycle as a whole |
| NIST CSF 2.0 | Risk-management structure |
| NIST SP 800-61 Rev. 3 | Incident response |
| OWASP ASVS 5.0.0 | The FastAPI and WebSocket layer |

## 2. What the repository can enforce, and what it cannot

This distinction is the reason the policy is credible. A repository can be read
in full and still tell you nothing about whether a production server is
hardened.

**Enforced in the repository (CI fails):**

- Lint, format, type checking, unit, integration and regression tests
- Coverage floors, repo-wide and per file
- Architecture laws (`scripts/arch_gate.sh`)
- Static invariants, including the no-silent-degradation rules (`GOV-004`)
- Requirement-to-test traceability (`GOV-005`)
- All-green gates on every pull-request workflow (`GOV-008`)
- Least-privilege workflow permissions and SHA-pinned actions
  (`SUP-001`, `SUP-002`)
- Absence of production secrets from pull-request jobs (`SUP-003`)
- Dependency and code scanning (CodeQL, dependency review, audit)
- Secret scanning and push protection
- Artifact SBOM, hash and attestation (`SUP-004`)
- Application-level security properties: authz matrix, IDOR, injection, SSRF,
  WebSocket validation, rate limiting, headers, log hygiene, constant-time
  comparison, TLS verification, audit-chain integrity

**Verified in the deployment environment (documented, drilled, not CI-checkable):**

- Exchange API key restrictions (`SECR-010`) — §6 below
- Production host hardening, SSH policy, non-root service account — §7
- Network segmentation, firewall default-deny, no directly exposed database,
  Redis, TimescaleDB, admin interface or internal monitoring — §8
- Backup encryption, off-host storage, immutability and restore drills
  (`RES-003`)
- Secret-manager configuration and OIDC trust policies
- Database account separation and least privilege — §9
- Cloud account and identity configuration, MFA on administrative identities

A requirement in the second list is still a registry entry with a criticality
and a named procedure. It is simply honest about where the evidence comes from.

## 3. Secrets

Never in:

```
source code
.env committed to Git
Docker image
frontend
logs
database plaintext
GitHub workflow YAML
```

Where they belong:

```
Developer
    ↓
local secret store

CI
    ↓
GitHub environment secret / OIDC / secret manager

Production
    ↓
dedicated secret manager
```

Prefer short-lived OIDC-based authentication over long-lived cloud credentials
wherever the provider supports it.

GitHub secret scanning covers the Git history, and push protection prevents new
leaks. Neither removes the need for `SECR-001`: the test that deliberately
provokes errors involving a key, secret, JWT, database password, authorization
header and exchange credential, then asserts none of them appears in the output.

## 4. Cryptography

```
Application
    ↓
Cryptographic library
    ↓
Established algorithm
```

never:

```
Application
    ↓
custom crypto implementation
```

**Do not invent cryptographic algorithms.** `SECR-008` makes this checkable;
`src/security/pq_transport.py` is honest about being a stub precisely so that
nobody mistakes it for a primitive.

Specific obligations:

- Randomness for anything that must be unpredictable comes from the OS CSPRNG
  (`SECR-007`).
- Secret and token comparisons are constant-time (`SECR-002`,
  `src/security/constant_time.py`).
- Keys are not logged, not serialised accidentally, not included in exception
  messages, and not exposed through API responses (`SECR-001`).
- Signed requests reject modified requests, modified timestamps, modified
  parameters and replays (`SECR-005`).

## 5. Encryption

**At rest** — database backups, production databases, sensitive model
artifacts, audit archives, configuration backups, secret-manager backups
(`SECR-009`).

But do not mistake *encrypted* for *secure*. If the attacker obtains

```
ciphertext + encryption key
```

the encryption bought nothing. **Key management matters more than calling
encrypt().**

**In transit** — HTTPS/TLS, secure WebSocket, TLS database connections, TLS to
external providers. Production endpoints must not silently fall back to
plaintext, and automated checks cover expired certificates, hostname mismatch,
weak protocols, invalid chains, plaintext endpoints, incorrect redirects and
mixed content (`SECR-006`).

**Never disable TLS verification to make an exchange integration test pass.**

## 6. Exchange API keys

The most important security decision in the project, and it is made at the
exchange rather than in this code:

```
✓ trading permission only if required
✓ withdrawal disabled
✓ IP allowlisting where supported
✓ minimal account permissions
✓ separate paper/testnet keys
✓ separate production keys
✓ separate exchange accounts/subaccounts
```

**The application must not possess withdrawal capability.** Even with the
server fully compromised, the blast radius is then bad trading rather than
theft.

Key rotation policy, including the compromise cases, is in
`docs/security/KEY_MANAGEMENT.md`.

## 7. Administrative access

If the production host is administered directly:

```
no password SSH
no shared accounts
MFA on administrative identity
SSH keys
restricted source IP/VPN where possible
logging
minimal privileges
```

**The trading application does not run as root.**

## 8. Network

```
Internet
   │
   ▼
Reverse proxy / WAF
   │
   ▼
API
   │
   ├── database
   ├── queue
   └── internal services
```

Never exposed directly to the Internet:

```
database
Redis
TimescaleDB
admin interfaces
internal monitoring
```

Production segmentation:

```
Public subnet
     │
     ▼
API gateway
     │
     ▼
Private application network
     │
     ├── database
     ├── model service
     └── internal services
```

Firewall default-deny. Allow only HTTPS, the required exchange endpoints,
internal database traffic, monitoring, and administrative access over a secure
channel. Do not expose a port because a service happens to listen on it.

## 9. Database

```
least-privilege DB account
TLS
no public exposure
backup encryption
restore testing
migration safety
SQL injection
connection limits
credential rotation
```

Separate, where practical:

```
application DB user
migration user
administrative DB user
```

**The application does not need database superuser privileges.**

## 10. Deployment separation

```
development
    ↓
CI
    ↓
staging
    ↓
paper
    ↓
production
```

Separate credentials at every stage. Never:

```
staging → production exchange account
```

## 11. Containers

```
non-root user
read-only filesystem where possible
minimal base image
drop Linux capabilities
no privileged container
no host networking unless required
resource limits
image scanning
SBOM
signed/attested image
```

Those properties are tested in CI, not merely written here (`SUP-006`).

## 12. Fail-safe policy

| Component failure | Behaviour |
|---|---|
| Price feed unavailable | FAIL CLOSED |
| Risk engine unavailable | FAIL CLOSED |
| Exchange status unknown | STOP / RECONCILE |
| Optional analytics unavailable | DEGRADED |
| Model unavailable | NO TRADE |
| Audit system unavailable | NO LIVE TRADE |
| Database unavailable | Safe halt / recovery |
| WebSocket unavailable | Safe recovery mode |

**Fail open is never the universal rule.** `RES-001` requires each row to be
decided by a test rather than by whatever the code happens to do.

And the interaction runs both ways: a security control failing must not create
a trading vulnerability. An unavailable authentication service must not make
the trade endpoint unauthenticated (`API-008`); an overloaded logging system
must not crash the process into an unsafe state.

## 13. No silent degradation

Forbidden around security or risk logic:

```python
try:
    risk_check()
except:
    pass
```

```python
except Exception:
    return True
```

A swallowed exception in a trading system is a financial-security
vulnerability. Static analysis flags bare excepts, broad exception handlers,
`pass` in a critical path, and default-allow security decisions; **every
finding is reviewed by a human** (`GOV-004`).

## 14. Type and arithmetic discipline

Do not let a raw `float` represent everything. Distinguish, at least
conceptually:

```
Price
Quantity
Notional
Probability
Percentage
Timestamp
OrderId
```

Money arithmetic states where `Decimal`, integer smallest units and floating
point are each used, and why. Rounding, precision, fees, tick size, lot size,
minimum quantity and currency conversion are tested (`DATA-004`, `DATA-005`).

**Never assume floating-point arithmetic behaves like exact decimal money
arithmetic.**

## 15. Incident handling

See `docs/security/INCIDENT_RESPONSE.md`. Every security finding becomes a
permanent `SEC-####` registry entry with a test, so the weakness cannot
silently return (`GOV-002`).
