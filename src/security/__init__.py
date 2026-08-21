"""
Trade-Bot security hardening layer (Part II: Cryptographic Foundations).

Modules:
  api_signer        — Ed25519 deterministic request signing
  auth_keys         — Ed25519/X25519 key management
  credential_vault  — BIP-32 HD key derivation for exchange credentials
  constant_time     — Side-channel-safe comparisons
  pq_transport      — Quantum-safe transport stub (Kyber-768)
"""
