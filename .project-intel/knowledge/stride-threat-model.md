# STRIDE Threat Modeling
**Domain**: risk | **Tags**: stride, threat, security, model, spoofing, tampering, repudiation, disclosure, denial, elevation

## STRIDE Threat Model — Trade Bot Risk Assessment

### S — Spoofing
Risk: Fake API responses from exchange (MITM)
Mitigation: TLS pinning on ccxt requests; verify exchange SSL cert
Status: OPEN — ccxt handles TLS but no pinning configured

### T — Tampering
Risk: ORDER_SECRET or API_KEY leaked → tampered orders
Mitigation: detect-secrets baseline + bandit SAST (CI enforced)
Risk: SESSION_STATE.json modified externally → wrong trade context
Mitigation: checksum SESSION_STATE on load

### R — Repudiation
Risk: Trade audit log tampered after the fact
Status: TradeAuditor logs to SQLite — not append-only
Mitigation: consider SQLite write-ahead log + periodic hash chain

### I — Information Disclosure
Risk: API keys in .env exposed via /debug endpoints
Mitigation: /debug/* requires X-API-Key (implemented in auth.py)
Gap: /debug/audit exposes full trade decisions — add operator-only gate

### D — Denial of Service
Risk: WebSocket flood → memory exhaustion (512MB alert)
Mitigation: RuntimeMonitor watches memory; restart on 1GB
Gap: No rate limiting on WebSocket connections

### E — Elevation of Privilege
Risk: EXECUTION_MODE=live without operator approval
Mitigation: OPERATOR_SECRET required for mode switch (implemented)
