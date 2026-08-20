# Security Reference

## Threat Model (required artifact per Law 13)

Every system must produce and maintain a threat model covering:

| Attacker Class | Capability | Primary Vectors |
|---|---|---|
| External remote | Internet access | API abuse, credential theft, DDoS, supply chain |
| Insider / compromised employee | Internal access | Key exfiltration, rogue order injection, log tampering |
| Nation-state | Advanced persistent, supply chain | Firmware implants, protocol-level interception |
| MEV bot / searcher | Mempool visibility | Front-run, sandwich, JIT liquidity |
| Social engineer | Impersonation, urgency | Phishing, multisig UI spoofing (Bybit 2025) |
| Physical | Data center access | Hardware implant, cold boot, TEMPEST |

---

## Zero-Trust Architecture

### Principles
- Verify every request; never implicit trust based on network location
- Least privilege: each service has only the permissions it needs, nothing more
- Assume breach: design assuming attacker has already compromised the network
- Mutual TLS (mTLS) between all internal services: both sides present certificates
- Service mesh (Istio/Linkerd): enforce mTLS, policy, and observability automatically

### Identity-Based Access
- Services: SPIFFE/SPIRE (X.509 SVIDs); workload identity independent of IP
- Humans: hardware token (FIDO2/WebAuthn) required; TOTP insufficient for privileged access
- Just-in-time access (JIT): ephemeral credentials provisioned for specific tasks;
  revoked automatically after time window
- Privileged Access Workstation (PAW): dedicated, hardened machine for key operations;
  no browsing, email, or general use

---

## Bybit Hack Post-Mortem (Feb 2025) — Security Architecture Lessons

### Attack Summary
- Compromised Safe{Wallet} frontend (UI injection)
- Multi-sig signers saw legitimate-looking Gnosis Safe interface
- Actually approved malicious contract transaction draining cold wallet
- $1.5B ETH transferred to attacker; later routed through Tornado Cash (sanctioned)

### Mandatory Technical Controls (post-Bybit)

**1. Transaction Decode-Before-Sign (architectural requirement)**
```python
def signing_workflow(raw_tx: bytes, hw_signer) -> bytes:
    decoded = decode_transaction(raw_tx)  # Must succeed or ABORT
    if decoded is None:
        raise SecurityError("Cannot decode transaction — signing refused")
    display_to_operator(decoded)  # Human must read and confirm
    confirmation = require_dual_operator_approval(decoded, timeout_s=120)
    if not confirmation.approved:
        raise SecurityError("Operator did not approve")
    return hw_signer.sign(raw_tx)
```

**2. Gnosis Safe / Multisig Verification (before every session)**
- Verify `GnosisSafe` implementation address hasn't changed (store hash)
- Verify no pending proxy upgrade in Safe modules
- Verify delegate call targets match allowlist
- If any check fails: halt session, security incident, do not sign

**3. Out-of-Band Confirmation for Large Transactions**
- Any multisig tx > $500K: confirmation via out-of-band encrypted channel (Signal, not email)
- All signers verify independently on separate devices
- Video recording of signing ceremony for transactions > $5M

**4. UI Integrity Verification**
- Subresource Integrity (SRI): all loaded scripts must have `integrity` attribute
- Content Security Policy (CSP): strict; no inline scripts; report violations
- Browser extension isolation: no browser extensions on signing workstation
- DNS validation: verify DNS record hasn't changed before loading signing UI

---

## Hardware Security Modules (HSMs)

### Selection Criteria
- FIPS 140-2 Level 3 minimum (Level 4 for root CA / master key)
- PQC roadmap: require ML-KEM/ML-DSA firmware upgrade path (2025–2026 timeframe)
- Supported vendors: AWS CloudHSM (FIPS 140-2 L3), Thales Luna (L3/L4),
  Yubico YubiHSM2 (L3, lower cost), Securosys Primus

### Key Operations Model
```
Application → Vault API → HSM Driver → HSM Hardware
                                            │
                               Key never leaves HSM boundary
                               Signing happens inside HSM
```
- Signing request: application sends message hash to HSM; receives signature
- Private key material: never extracted from HSM in any form

---

## Trusted Execution Environments (TEE)

### Intel TDX (Trust Domain Extensions, 2024)
- VM-level isolation: entire guest VM runs in encrypted memory
- Host (hypervisor) cannot read guest memory even with root access
- Remote attestation: cryptographic proof that code is running inside genuine TDX TD
- Use case: key management service, signing service — attested to remote verifier
  before keys provisioned

### AMD SEV-SNP (Secure Encrypted Virtualization - Secure Nested Paging)
- Similar to TDX; VM memory encrypted with VM-specific key
- SNP adds: integrity protection of memory pages; prevents hypervisor from
  swapping or modifying guest pages
- Attestation report: signed by AMD root key; verifiable by any party

### TEE Use in Trading Systems
- Key ceremony: run DKG inside TEE; attestation proves code hasn't been tampered
- Signing service: hot wallet signing logic inside TEE; keys never in host memory
- Confidential computing: strategy logic inside TEE; IP protected from cloud provider

---

## Supply Chain Security

### SLSA (Supply-chain Levels for Software Artifacts)

| Level | Requirement | Provides |
|---|---|---|
| 1 | Build script exists | Basic provenance |
| 2 | Hosted build, signed provenance | Source + build integrity |
| 3 | Hardened build, non-falsifiable provenance | Strong audit trail |
| 4 | Hermetic, reproducible build, dual review | Highest assurance |

- Target: **SLSA Level 3** minimum for all production components
- SLSA Level 4: for key management service, signing service, risk engine
- Provenance attestation: store alongside every build artifact

### SBOM (Software Bill of Materials)
- Generate SBOM for every build: CycloneDX or SPDX format
- Scan SBOM against: OSV (Google), NVD, GitHub Advisory Database
- Block deployment: any CRITICAL CVE unpatched in direct dependency
- Transitive dependencies: HIGH CVEs also block; MEDIUM = 30-day remediation SLA

### Dependency Pinning
```toml
# pyproject.toml — pin all direct AND transitive deps
[tool.pip-tools]
generate-hashes = true  # Include hash for every package

# XZ utils (Apr 2024) — supply chain attack case study:
# Pinning + hash verification would have detected the tampered liblzma
```

### Reproducible Builds
- Identical source + build environment → identical binary (bit-for-bit)
- Enables independent verification; detects build-system compromise
- Tools: `reprotest`, Nix, Bazel with `--worker_max_instances` pinning
- Reference: Bitcoin builds achieve reproducibility across OSes

---

## Application Security

### Input Validation
- All external inputs validated against strict schema before use
- Reject-by-default: whitelist valid inputs; deny anything else
- Numeric bounds: price, quantity, timestamps checked against realistic ranges
  (reject BTC price > $10M or < $100 as anomaly)
- LLM prompt injection: if market data is passed to LLM, sanitize for
  instruction-override patterns before inclusion

### SQL / Injection
- Parameterized queries only — no string concatenation in SQL
- ORM: use query builder with bound parameters; never raw user input in queries
- Audit log writes: append-only connection; no SELECT, no DELETE on audit log user

### Output Encoding
- Exchange order payloads: JSON; validate schema before serialization
- Log output: structured (JSON); never interpolate user-controlled data directly
- Webhook callbacks: validate HMAC signature before processing any callback

---

## Operational Security

### Secrets Lifecycle
```
Generate → Store (Vault) → Rotate (automated, ≤ 90d) → Revoke
                                      ↑
                    Detect compromise → emergency revoke (< 5 min SLA)
```
- Secret zero: how Vault itself is unsealed; Shamir secret sharing; 3-of-5 key holders
- Audit log for every secret access: `who, when, which secret, which service`
- Dead secrets: leaked secrets are revoked immediately; assume compromise is public

### Network Hardening
- Security groups: deny-all default; whitelist specific ports per service
- Exchange API: connect from dedicated IP range; IP allowlisted on exchange
- Egress filtering: only necessary outbound connections; alert on new destinations
- SSH: key-only, no password; ProxyJump via bastion; session recording (Teleport)
- No direct production SSH access: all changes via CI/CD pipeline with human approval

### Container Security
- Non-root user inside container: `USER 1000:1000` in Dockerfile
- Read-only root filesystem: `--read-only`; explicit writable volume mounts only
- No `--privileged`: never; minimal `--cap-add` only for specific requirements
- Image signing: Sigstore/Cosign; verify signature before deployment
- Runtime security: Falco or equivalent; alert on unexpected syscalls

---

## Incident Response

### SECURITY_EVENT types
| Event | Response SLA | Notification |
|---|---|---|
| API key suspected compromise | 5 min: rotate; 15 min: review access log | Security + Ops |
| Wallet drain detected | Immediate: pause all withdrawals | CISO + Legal |
| Signing attempt on undecodable tx | Immediate: halt signing session | Security |
| Sanctions match | Immediate: block; 30 days: SAR | Compliance + Legal |
| Dependency CVE Critical | 24h patch | Security + Engineering |
| DORA Major Incident | 4h notify regulator | Compliance + CISO |
| Blind signing path discovered | PR blocked; fix same day | Security |

### Forensics Readiness
- Immutable audit log: CloudTrail / Vault audit / order audit — cannot be modified
- Log retention: 7 years; encrypted; access-controlled; separate account/project
- Memory forensics: for incident analysis; enable core dumps off (normal), on (investigation)
- Network capture: short rolling PCAP on management interfaces (5 min rolling)
  for incident reconstruction; legal review of retention requirements
