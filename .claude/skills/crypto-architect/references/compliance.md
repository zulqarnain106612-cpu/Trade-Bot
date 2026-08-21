# Regulatory Compliance Reference

## Jurisdiction Matrix

| Jurisdiction | Framework | Key Obligations | Effective |
|---|---|---|---|
| EU | MiFID II, MiCA, DORA | Best execution, CASP registration, ICT resilience | MiCA: Jun 2024 / DORA: Jan 2025 |
| US | SEC, CFTC, FinCEN | MSB registration, SAR filing, position limits | Ongoing |
| UK | FCA | Crypto asset registration, market abuse rules | Ongoing |
| Singapore | MAS PS Act | Digital Payment Token licence, AML/CFT | Ongoing |
| UAE | VARA | VASP licence, advertising rules | Ongoing |
| Cayman | CIMA VASP | Registration, KYC/AML policies | Ongoing |

Architect rule: legal review mandatory before live deployment in any new jurisdiction.
This reference covers technical implementation patterns; legal advice is separate.

---

## MiCA (Markets in Crypto-Assets Regulation) — EU

### CASP Registration (Jun 2024 onwards)
- Crypto-Asset Service Providers: exchange, custody, trading, transfer, advice, portfolio mgmt
- Single passport: register in one EU member state; operate across all EU
- Capital requirements: tier by service type (€50K–€150K; €125K for trading)
- Whitelist enforcement: only MiCA-compliant asset classes permitted; stablecoins
  (e-money tokens, asset-referenced tokens) have additional issuer requirements
- Technical obligation: order entry system must enforce asset whitelist at pre-trade level;
  MiCA-non-compliant tokens blocked before order submission

### Stablecoin Rules (Title III/IV)
- E-money tokens (USDC, EURC): must be 1:1 fiat backed; issuer must be licensed
- Asset-referenced tokens (multi-collateral): stricter issuer requirements
- Significant EMT/ART: volume caps enforced by issuer; trading restriction may affect
  liquidity assumptions for EUR-pegged strategies
- Non-MiCA-compliant stablecoins (USDT status uncertain in EU): assess before including
  in EU-facing strategies

### Record-Keeping (MiCA Art. 72-75)
- All orders, quotes, positions logged; available to regulator on request
- 5-year retention minimum; tamper-evident storage
- Client order handling: fair order routing; conflicts of interest documented

---

## DORA (Digital Operational Resilience Act) — EU, Jan 2025

### ICT Risk Management Framework
- Written ICT risk management policy: mandatory; reviewed annually
- Business continuity and disaster recovery plans: documented, tested
- ICT-related incident classification: major incident = regulatory notification within
  4 hours of classification; root cause report within 1 month
- ICT third-party risk: register of all critical ICT providers; cloud providers,
  exchange APIs, data feeds, vault providers = critical ICT third parties

### Third-Party Provider Register (required)
```json
{
  "provider": "AWS",
  "services": ["compute", "storage", "KMS"],
  "criticality": "HIGH",
  "contract_review_date": "2025-01-01",
  "concentration_risk": "multi-region mitigated",
  "exit_strategy": "runbook: aws-exit-v2.md"
}
```
- Concentration risk: dependency on single cloud provider = DORA concentration risk;
  document mitigation (multi-region, multi-cloud strategy)
- Exit strategy: for each critical provider, runbook exists for transition within RTO

### Incident Classification
| Category | Definition | Notification |
|---|---|---|
| Major | > 5% users affected, > 2h downtime, > €100K impact | 4h (initial) + 72h (intermediate) + 1 month (final) |
| Significant | Repeated incidents, data integrity issues | Internal escalation only |
| Minor | Single user, short duration | Log only |

---

## Wash Trading Detection

### Definition
A wash trade is any transaction where the buyer and seller are the same economic
entity (directly or via related accounts), or where there is no genuine change of
beneficial ownership.

### Detection Patterns
```python
def is_wash_trade(order_a: Order, order_b: Order, config: WashConfig) -> bool:
    same_account = order_a.account_id == order_b.account_id
    opposing_sides = order_a.side != order_b.side
    same_symbol = order_a.symbol == order_b.symbol
    within_window = (
        abs(order_a.timestamp - order_b.timestamp) < config.window_ms
    )
    size_match = (
        abs(order_a.quantity - order_b.quantity)
        / max(order_a.quantity, order_b.quantity)
        < config.size_tolerance
    )
    return same_account and opposing_sides and same_symbol and within_window and size_match
```
- Wash window: configurable; typically 60s for same-account, 300s for related accounts
- Related accounts: maintain graph of related entities; apply wash check across graph
- Pre-submission check: block wash-creating order before it reaches exchange
- Log every detected wash pattern: `event_type=WASH_TRADE_BLOCKED`, include both order IDs

### Layering / Spoofing Detection
- Layering: placing and rapidly cancelling orders to create false depth impression
- Detection: cancel rate > 90% on orders resting < 500ms → flag for review
- Spoofing: large order placed, then cancelled when market moves toward it
- Alert threshold: order-to-fill ratio > 50:1 on a single symbol in any 5-minute window

---

## Best Execution

### Obligation (MiFID II Art. 27 / MiCA equivalent)
Execute client orders on terms most favorable with respect to:
price, costs, speed, likelihood of execution, size, nature, and other factors.

### Best Execution Logging
```json
{
  "order_id": "...",
  "symbol": "BTC/USDT",
  "timestamp_order": "...",
  "timestamp_fill": "...",
  "benchmark_price": 50000.0,
  "benchmark_type": "TWAP_60s",
  "fill_price": 50012.5,
  "slippage_bps": 2.5,
  "venue": "Binance",
  "alternative_venues_checked": ["OKX", "Bybit"],
  "best_available_price": 50010.0,
  "execution_quality": "ACCEPTABLE"
}
```
- Benchmark options: arrival price, TWAP (60s/300s), VWAP, close price
- Slippage vs benchmark: tracked per strategy, per symbol; regression = review
- Venue selection: documented decision; must consider price improvement, fee, reliability

---

## Trade Reporting

### MiFID II / EMIR (EU)
Required fields (subset): LEI, ISIN/venue-specific ID, quantity, price, timestamp
(microsecond), side, capacity (principal vs agent), venue, trader ID

### CFTC Part 43 / 45 (US)
- Swap data reporting: T+0 for real-time public reporting; T+1 for regulatory reporting
- Large trader reporting: any position exceeding CFTC thresholds reported daily

### Implementation
```
Trade Event → Reporting Adapter → Regulatory Report Format → ARM/SDR
                                         ↑
                              Reconciliation against exchange confirmations
```
- Report queue: separate from trading path; never block order on report submission
- Failure handling: failed report retried with backoff; unresolved after 4h → alert compliance
- Report retention: 5 years minimum (EU/CFTC); encrypted, queryable

---

## AML / KYC

### Transaction Monitoring
| Trigger | Threshold | Action |
|---|---|---|
| Single withdrawal | > $10,000 equivalent | Enhanced review |
| Aggregate daily | > $25,000 | SAR consideration |
| Structuring detection | Multiple < $10K in 24h | SAR filing |
| Sanctions match | Any OFAC/UN/EU hit | Block, freeze, report immediately |

### Travel Rule (FATF R.16)
- Wire transfers > $3,000: originator and beneficiary VASP information must travel with transfer
- Implementation: OpenVASP, TRP, TRUST, Notabene protocols for VASP-to-VASP messaging
- Counterparty VASP verification: verify licence status before transfer; maintain registry
- Non-custodial wallet: enhanced due diligence required; some jurisdictions require proof
  of ownership before transfer to unhosted wallet

### Sanctions Screening
- Screen: counterparty addresses, withdrawal destinations, any identified wallet
- Blockchain analytics: cluster analysis for indirect exposure (hop analysis)
  — funds passing through sanctioned entity within N hops = risk event
  Chainalysis, Elliptic, TRM Labs: integrate at withdrawal initiation
- False positive rate: track and tune; high false positives = operational drag
- SAR filing: within 30 days of detection (FinCEN); filing confidential from subject

### Wallet Screening Implementation
```python
async def screen_withdrawal(
    address: str, amount: float, currency: str
) -> ScreenResult:
    result = await chainalysis_client.screen(address)
    # Direct hit
    if result.risk_score > config.block_threshold:
        await compliance_alert(address, amount, result)
        raise ComplianceBlock(f"Address {address} blocked: risk={result.risk_score}")
    # Indirect / hop exposure
    if result.indirect_exposure and result.indirect_exposure.risk > config.indirect_threshold:
        await queue_for_review(address, amount, result)
    return result
```

---

## Market Manipulation Controls

### Control Implementation
- Order purpose field: each order includes machine-readable purpose
  (HEDGE, SIGNAL, REBALANCE, LIQUIDITY_PROVISION)
- Concentration check: if our orders represent > 20% of market volume in any 5m window
  → throttle and alert
- Close period restriction: last 5 minutes before settlement on options/futures → manual
  review required for any new position
- Cross-market correlation monitor: unexpected correlation spike between our activity
  and price in a linked market → alert

---

## Data Privacy (GDPR / equivalent)

- PII in trading systems: trader names, IPs, account identifiers
- Pseudonymize in logs: replace PII with deterministic hash + salt
- Data subject requests: right-to-erasure within 30 days
  (except records required for regulatory retention)
- Data export: audit log includes only minimum required PII
- Third-party data sharing: DPA required for EU processors; map all data flows
- Data residency: trade data stored per jurisdiction requirements;
  EU data must not transit to non-adequate countries without SCCs

---

## Bybit Hack (Feb 2025) — Regulatory Lessons

- Attack: social engineering of Safe{Wallet} frontend; multisig signers shown legitimate
  UI but signed malicious transaction; $1.5B ETH drained
- Regulatory implication: VARA (UAE) increased scrutiny of custody controls;
  expect similar regulatory focus globally post-incident
- Technical controls (mandatory post-incident):
  1. Hardware wallet policy: always decode raw calldata before signing; refuse to sign
     any transaction where decoded intent differs from expected
  2. Safe{Wallet} / Gnosis Safe: verify implementation address hasn't changed
     before any signing session; alert on any proxy upgrade
  3. Multisig ceremony: video recording mandatory; all signers verify independently
  4. Out-of-band confirmation: for any multisig tx > $500K, confirm via separate
     authenticated channel before signing session begins
- Incident classification under DORA: this attack class constitutes a major ICT incident;
  mandatory regulatory notification within 4h of classification
