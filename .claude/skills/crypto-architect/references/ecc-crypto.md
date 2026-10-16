# ECC Cryptography & Post-Quantum Reference

## Trade-Bot integration (verified against the repository)

- `src/ecc/` is **on-chain analytics** (ECDSA nonce-reuse scanning, Schnorr/Taproot,
  secp256k1 clustering, UTXO curves, zkSNARK detection). Its output reaches the
  optional horizon pipeline in `src/intel.py` (`cluster_flow_score`); it is not
  key management and does not sign anything.
- Key handling lives in `src/security/` (see `security.md`);
  `src/security/pq_transport.py` is a stub, not a deployed PQC transport.
- Prime-field/NTT arithmetic for lattice parameters: `src/mathcore/fields/ntt.py`.

## Classical Curves Used in Crypto

| Curve | Used By | Security Bits | Form |
|---|---|---|---|
| secp256k1 | Bitcoin, Ethereum, most CEX | 128 | Weierstrass |
| Ed25519 | Solana, Monero, Binance Ed25519 API keys | 128 | Edwards (twisted) |
| P-256 (secp256r1) | TLS, some HSM | 128 | Weierstrass (NIST) |
| BLS12-381 | ETH2 consensus, ZK proofs | 128 | Barreto-Lynn-Scott pairing |
| Ristretto255 | Modern protocols, MPC | 128 | Edwards (canonical encoding) |
| X25519 | Key agreement (ECDH) | 128 | Montgomery |

- secp256k1: `a=0, b=7`; parameters are rigid (little room for hidden choices)
- Ed25519: deterministic, no k-reuse vulnerability — **preferred for new systems**
- P-256: no known practical weakness; seed-rigidity concerns are debated, not
  demonstrated. FIPS-approved and often required by HSMs — acceptable
- BLS12-381: pairing operations for threshold signatures and ZK proofs
- Ristretto255: eliminates Ed25519 cofactor issues; prefer for MPC implementations
- X25519: use for key agreement only; never for signing

---

## NIST Post-Quantum Cryptography — Final Standards (2024)

NIST published final PQC standards in August 2024. These are the only
architect-approved PQC algorithms for new systems.

### ML-KEM (CRYSTALS-Kyber) — FIPS 203
- **Purpose**: Key Encapsulation Mechanism (replaces RSA/ECDH for key exchange)
- **Security levels**: ML-KEM-512 (L1, ~128-bit), ML-KEM-768 (L3, ~192-bit), ML-KEM-1024 (L5, ~256-bit)
- **Architect default**: ML-KEM-768 for standard systems; ML-KEM-1024 for long-lived keys (> 5 years)
- **Performance**: fast; encapsulation ~100μs on modern hardware; ciphertext 1.1KB (L3)
- **Use**: replace ECDH in all new transport and key-wrapping applications

### ML-DSA (CRYSTALS-Dilithium) — FIPS 204
- **Purpose**: Digital signature (replaces ECDSA/Ed25519 for signatures)
- **Security levels**: ML-DSA-44 (L2), ML-DSA-65 (L3), ML-DSA-87 (L5)
- **Architect default**: ML-DSA-65 for most signing; ML-DSA-87 for root CA or long-lived certs
- **Signature size**: ML-DSA-65 = 3309 bytes (FIPS 204; vs ECDSA 64 bytes) — design protocols accordingly
- **Public key size**: ML-DSA-65 ≈ 1952 bytes — bandwidth budget must account for PQC keys
- **Use**: new signing infrastructure; hybrid with ECDSA during transition

### SLH-DSA (SPHINCS+) — FIPS 205
- **Purpose**: Stateless hash-based signature (conservative alternative to ML-DSA)
- **Advantage**: security based only on hash function security; no lattice assumptions
- **Disadvantage**: larger signatures (8KB–50KB) and slower signing
- **Use**: high-assurance root keys, code signing; where performance is secondary to
  security conservatism

### FN-DSA (Falcon) — Under FIPS standardization
- **Purpose**: Digital signature; lattice-based, smaller signatures than ML-DSA
- **Signature size**: Falcon-512 ≈ 666 bytes; Falcon-1024 ≈ 1280 bytes
- **Caution**: signing requires careful RNG; use constant-time implementation only
- **Status**: FIPS 206 (FN-DSA) standardization — historical; current status requires confirmation; until final, prefer ML-DSA and use Falcon only
  where bandwidth is constrained and ML-DSA signature size is prohibitive

---

## Hybrid Classical + PQC (Mandatory for Transition)

### Rationale
- No publicly demonstrated quantum computer can break classical ECC (as of 2025; historical; current status requires confirmation)
- "Harvest now, decrypt later": adversaries store ciphertext; decrypt when quantum available
- Hybrid: both classical and PQC algorithms must verify, so a break of either one
  alone does not forge a signature or recover a key

### Implementation Pattern
```python
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

# Use a FIPS 204 ML-DSA implementation (e.g. liboqs). Round-3 "Dilithium3" is NOT
# interoperable with ML-DSA-65. Pure-Python packages are not constant-time:
# acceptable for tests, never for production keys.
from dilithium_py.ml_dsa import ML_DSA_65  # API: sign(sk, m), verify(pk, m, sig)


def hybrid_sign(message: bytes, ed25519_key: Ed25519PrivateKey, ml_dsa_key: bytes) -> bytes:
    """Both signatures must be produced and verified."""
    sig_classical = ed25519_key.sign(message)
    sig_pqc = ML_DSA_65.sign(ml_dsa_key, message)
    # Concatenate: [2B len_classical][sig_classical][2B len_pqc][sig_pqc]
    return (
        len(sig_classical).to_bytes(2, "big")
        + sig_classical
        + len(sig_pqc).to_bytes(2, "big")
        + sig_pqc
    )


def hybrid_verify(message: bytes, hybrid_sig: bytes, ed25519_pubkey, ml_dsa_pubkey: bytes) -> bool:
    """BOTH must pass — neither alone is sufficient."""
    offset = 0
    len_c = int.from_bytes(hybrid_sig[offset : offset + 2], "big")
    offset += 2
    sig_c = hybrid_sig[offset : offset + len_c]
    offset += len_c
    len_pqc = int.from_bytes(hybrid_sig[offset : offset + 2], "big")
    offset += 2
    sig_pqc = hybrid_sig[offset : offset + len_pqc]
    if len(sig_c) != len_c or len(sig_pqc) != len_pqc or offset + len_pqc != len(hybrid_sig):
        return False  # truncated or trailing bytes: reject, never parse leniently
    ed25519_pubkey.verify(sig_c, message)  # raises on failure
    return ML_DSA_65.verify(ml_dsa_pubkey, message, sig_pqc)
```

### X25519MLKEM768 (Key Exchange Hybrid)
- `X25519MLKEM768`: X25519 + ML-KEM-768 hybrid TLS group (IETF draft; codepoint and
  status: historical; current status requires confirmation)
- Library support (Go, BoringSSL, OpenSSL, quiche) changes by release; historical; current status requires confirmation
- Use for: all new TLS/QUIC transport where library supports it
- Shared secret: the ML-KEM-768 and X25519 shared secrets are concatenated (ML-KEM
  first, per the draft — confirm) and fed to the TLS 1.3 key schedule; breaking
  either component alone does not recover the session key

---

## PQC Migration Roadmap (Required Artifact)

Every system with new key infrastructure must produce this artifact:

```markdown
## PQC Migration Roadmap — [System Name]

### Exposure Inventory
| Key Type | Current Algorithm | Quantum Threat | Priority |
|---|---|---|---|
| Vault master key | AES-256-GCM (symmetric) | Low (Grover: 256→128 bit) | MEDIUM |
| API key signing | HMAC-SHA256 | Low (Grover) | MEDIUM |
| TLS transport | X25519 | HIGH (Shor) | CRITICAL |
| Wallet signing | secp256k1/ECDSA | HIGH (Shor) | HIGH |
| JWT signing | RS256 | HIGH (Shor) | HIGH |

### Migration Phases
Phase 1 (example date): Hybrid TLS — X25519MLKEM768 for all new connections
Phase 2 (example date): API signing — Hybrid HMAC+ML-DSA-65 for new API infrastructure
Phase 3 (example date): Wallet — hybrid key generation for all new wallet derivations

### Constraints
- secp256k1 on-chain: cannot change until ETH/BTC protocol upgrades; monitor BIP proposals
- HSM vendor timeline: [vendor] ML-KEM support in firmware [version] ETA [date]
```

---

## ECDSA (secp256k1)

### Critical Security Rules
- Nonce `k` must be unique and random per signature — reusing k reveals private key
- Use RFC 6979 deterministic nonce generation — eliminates RNG dependency
- Low-s normalization: Bitcoin/Ethereum require `s ≤ n/2`; enforce in signing code
- Never implement ECDSA from scratch — use `cryptography` (Python) or `noble-secp256k1` (JS)

---

## EdDSA / Ed25519

- Deterministic: nonce derived from `Hash(privkey_seed || message)` — no RNG at signing
- Cofactor = 8: handle in batch verification; use cofactor-cleared operations
- Faster than ECDSA; no k-reuse vulnerability
- Binance Ed25519 API keys, Solana transaction signing

```python
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

privkey = Ed25519PrivateKey.generate()
signature = privkey.sign(message)
pubkey = privkey.public_key()
pubkey.verify(signature, message)  # raises if invalid
```

---

## BLS Signatures (BLS12-381)

- Pairing-based: signature aggregation across many signers into single signature
- ETH2 consensus, Filecoin, Chia
- Threshold BLS: m-of-n threshold signing without interaction after DKG
- EIP-2537 (Pectra): BLS precompile on EVM — on-chain BLS verification now cheap;
  enables on-chain threshold signature verification

### Threshold BLS for Trading (high-value wallets)
```
Setup: DKG among n parties; each holds key share
Sign:  m parties sign independently; coordinator aggregates
Verify: standard BLS verify against group public key
```
- Architect rule: threshold BLS for any wallet > $1M; minimum 3-of-5 configuration
  (an absolute floor in addition to Law 6's > 10%-of-capital MPC rule)
- Distributed Key Generation (DKG): no single party holds full key at any time

---

## MPC / Threshold ECDSA (TSS)

- Threshold ECDSA: distributed key generation + signing; compatible with secp256k1
- Libraries: tss-lib (Go), ZenGo-X (Rust), Silence Laboratories SDK
- No trusted dealer: key shares generated without any party seeing full key
- Active security: resist malicious (not just honest-but-curious) parties

---

## Key Management Architecture

### Key Hierarchy
```
Master Seed (BIP39 mnemonic, 256-bit entropy)
    └── Master Key (BIP32)
        ├── Exchange API Keys (signing only, no withdrawal)
        ├── Hot Wallet Keys (limited capital, MPC-protected)
        └── Cold Wallet Keys (bulk capital, airgapped, Schnorr multisig on BTC)
```

### Storage Rules
- Private keys never stored in plaintext — AES-256-GCM at rest
- Encryption key: from HSM or Argon2id (memory-hard); never stored alongside
- Memory hygiene: zero key bytes after signing (`ctypes.memset` on a mutable buffer;
  Python `bytes` are immutable and copies may persist — keep keys out of Python
  objects where possible, e.g. inside the HSM/vault process)
- Disable core dumps: `resource.setrlimit(resource.RLIMIT_CORE, (0, 0))`
- Swap encryption mandatory on any host handling key material

### Blind Signing Prevention (mandatory post-Bybit 2025)
- Hardware signers and software signers must decode raw transaction bytes before signing
- Decoded display must show: destination address, value, calldata function selector,
  decoded calldata parameters (for known contract ABIs)
- If decode fails → refuse to sign; alert; manual investigation required
- Gnosis Safe: verify implementation address and delegate call targets before every
  signing session; compare against previously verified addresses
- Ledger/Trezor: use clear-signing plugins where available; never approve "unrecognized transaction"

---

## Encryption for Data at Rest / in Transit

- AES-256-GCM: authenticated encryption for data at rest; 12-byte nonce, never reuse
- Key derivation: Argon2id (password-based); HKDF (key-based)
- ECIES: ephemeral X25519 + AES-GCM for encrypting to a known public key
- TLS minimum: 1.2 with forward secrecy; prefer TLS 1.3 with X25519MLKEM768
- Certificate pinning: pin exchange API certs; halt on pin mismatch — no retry, alert
- OCSP stapling: revocation check mandatory; never skip
