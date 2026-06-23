# Cryptographic Algorithms for Trading Systems
**Domain**: blockchain | **Tags**: crypto, ecc, ecdsa, ed25519, hmac, sha256, signature, key, hash, secp256k1

## Cryptographic Algorithms — Architecture Reference

### Used in this project (implicitly via ccxt/exchange APIs)

#### API Authentication (HMAC-SHA256)
Binance: HMAC-SHA256 of query_string with API_SECRET
OKX: HMAC-SHA256 of timestamp+method+path+body
Security: HMAC is MAC not signature — requires shared secret → never expose API_SECRET

#### WebSocket Authentication
WS key derived from API key + timestamp + HMAC
Replay attack window: 5s (Binance) — ensure clock sync (NTP)

### If extending to on-chain settlement
secp256k1 (Bitcoin/Ethereum): ECDSA signatures
Ed25519 (Solana/Cardano): faster, smaller signatures, no malleability
Key storage: never in .env for on-chain keys → hardware wallet or HSM

### Secure random for API key generation
# Current recommendation in README: openssl rand -hex 32
# Python equivalent (used in auth.py):
import secrets
key = secrets.token_hex(32)  # cryptographically secure

### Hash chain for audit log integrity
Each audit entry: hash = SHA256(previous_hash + entry_data)
Tamper detection: recompute chain and compare to stored hashes
Lightweight: adds ~0.1ms per entry
