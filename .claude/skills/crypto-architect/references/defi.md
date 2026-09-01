# DeFi Reference

## AMM Mechanics

### Constant Product (Uniswap v2)
```
x × y = k
Price = y / x
Price impact = ΔP/P = order_size / (pool_depth + order_size)
```
- Slippage grows super-linearly with order size relative to pool depth

### Concentrated Liquidity (Uniswap v3 / v4)
- Liquidity concentrated in tick ranges `[P_lower, P_upper]`
- Active liquidity only from in-range positions — depth varies dramatically by price
- LP impermanent loss: magnified vs v2 when price exits range
- Uniswap v4 (2024): singleton pool contract; hooks architecture for custom logic
  before/after swap, mint, burn; "just-in-time" liquidity hook possible
  Architect: hooks are custom code; treat any pool with non-standard hooks as
  unaudited smart contract risk unless the hook itself is audited

### Curve StableSwap
```
A × n^n × Σx_i + D = A × D × n^n + D^(n+1) / (n^n × Πx_i)
```
- Low slippage near peg; diverges rapidly off-peg
- Depeg event: liquidity drains asymmetrically; slippage spikes to market-level
- Curve's Lending (crvUSD, 2023–2024): LLAMMA (Lending-Liquidating AMM) soft-liquidations
  via continuous rebalancing — novel liquidation mechanism; factor into cascade models

---

## Intent-Based Protocols

### UniswapX (2023–2024)
- Signed intents (off-chain): user signs order specifying token-in, token-out, min amount,
  deadline; no gas until fill
- Fillers (solvers): competitive auction; fill via any liquidity source; user gets MEV rebate
- Dutch auction: price decays from optimal to minimum over deadline → better fills than AMM
- Architect: intent-based orders bypass traditional order flow; adjust fill-rate monitoring;
  unfilled intents expire silently — add expiry alert

### CoW Protocol / CoW Swap
- Coincidence of Wants (CoW): batch orders matched peer-to-peer before routing to AMM
- Solvers compete; best price wins; MEV protected (batch settlement, no frontrunning)
- Orders may not fill if no solver bids profitably — design for fill uncertainty

### 1inch Fusion / Fusion+
- Resolvers fill dutch-auction orders; cross-chain via Fusion+
- Slippage protection: amount out guaranteed above minimum; partial fills supported

### ERC-7683 Cross-Chain Intents Standard (2024)
- Standardizes intent format across chains; enables cross-chain atomic fills
- Filler registry: on-chain; filler posts bond; slashed for non-performance
- Architect: monitor ERC-7683 adoption; cross-chain arb strategies can use this

---

## MEV (Maximal Extractable Value)

### Attack Vectors
| Attack | Mechanism | Defense |
|---|---|---|
| Frontrunning | Copy tx, submit higher gas | Private mempool (Flashbots Protect) |
| Sandwich | Buy before + sell after target tx | Tight slippage + private mempool |
| Backrunning | Arbitrage after large trade | Unavoidable; price into expected post-trade |
| JIT Liquidity | LP right before large trade | Use Curve/Balancer; less JIT-exploitable |
| Liquidation MEV | Race to liquidate | Own liquidation bot; don't compete |
| Intent MEV | Solver underpays user | CoW/1inch Fusion with on-chain settlement |

### MEV-Share and PBS (Proposer-Builder Separation)
- MEV-Share (Flashbots 2023): user opts in to share order flow hints; solvers compete;
  user receives MEV rebate — evaluate for large orders
- PBS: block builders separate from validators; maximize MEV extraction;
  OFAC-compliant builders selectively censor → monitor inclusion rate
- `inclusion_rate_by_builder` metric: track if our txs excluded by certain builders

### Private Mempool Integration
```python
bundle = {
    "txs": [signed_tx_hex],
    "blockNumber": hex(target_block),
    "minTimestamp": 0,
    "maxTimestamp": 0,
    "revertingTxHashes": [],
}
# Submit to https://relay.flashbots.net (bundle signer key separate from trading key)
# MEV Blocker alternative: routes to builder with no-sandwich guarantee
```

---

## Flash Loans

### Mechanics
- Borrow arbitrary amount within single transaction; repay + fee in same tx
- Aave v3 = 0.05%; dYdX = 0; Uniswap v3 = 0.05%
- Atomicity: if repayment fails, entire tx reverts — zero credit risk

### Flash Loan Attack Defense (positions at risk)
- Oracle manipulation: borrow → inflate/deflate spot price → exploit TWAP-less protocol
- Defense: use 15-minute TWAP oracles; monitor unusual pool movements in same block

---

## Restaking (EigenLayer) — 2024

### Mechanics
- Ethereum validators or liquid staking token (LST) holders restake ETH to secure
  additional protocols (AVSes — Actively Validated Services)
- Operators: run AVS software; accept slashing conditions from multiple AVSes
- Slashing conditions: AVS-defined; can slash restaked ETH for misbehavior
  **This is additional slashing risk beyond standard PoS slashing**

### Architect Restaking Risk Model
- Slashing correlation: if operator misbehaves on one AVS → all restaked ETH at risk
  across all AVSes that operator secures; treat as correlated risk factor
- Operator concentration: check if > 30% of staked ETH flows through single operator
- AVS security audit: treat unaudited AVS contracts as HIGH smart contract risk
- Capital allocation: restaked ETH is illiquid during challenge window (7 days);
  exclude from liquid capital calculations
- Eigen slashing (live 2024): first mainnet slashings demonstrate real risk;
  update yield strategies to subtract realistic slashing probability

### Liquid Restaking Tokens (LRTs) — 2024
- LRT protocols (EtherFi, Puffer, Renzo, Kelp, Swell): issue ERC-20 representing
  restaked ETH + accumulated rewards
- LRT risks (beyond LST risks):
  1. Operator risk: concentrated operator → correlated slashing
  2. AVS risk: protocol bugs in AVS contracts → slashing
  3. Liquidity risk: LRT de-pegs from ETH in stress; depeg events occurred in 2024
  4. Upgrade risk: LRT protocol itself is upgradeable; check timelock
- LRT depeg model: simulate 15% depeg under correlated restaking crash scenario
- LRT yield accounting: gross APY − slashing probability estimate − IL (if LP) −
  smart contract risk premium − depeg risk premium = net yield

### EigenLayer Risk in Yield Strategies
```python
def net_restaking_yield(
    gross_apy: float,
    slashing_probability: float,  # Annual probability of slashing event
    slash_fraction: float,  # Fraction of stake slashed per event
    sc_risk_premium: float,  # Smart contract risk adjustment
) -> float:
    # Expected loss from slashing (actuarial)
    slashing_cost = slashing_probability * slash_fraction
    return gross_apy - slashing_cost - sc_risk_premium
```

---

## Lending Protocol Mechanics

### Collateralization
```
Health Factor = Σ(collateral_value × LTV) / Σ(debt_value)
Liquidation threshold: HF < 1.0
```
- Monitor HF in real-time for any leveraged DeFi position
- Alert at HF < 1.3 (30% buffer); auto-deleverage at HF < 1.15
- Aave v3 e-mode: allows higher LTV between correlated assets (ETH/wstETH 93% LTV)
  — increases liquidation cascade risk; model accordingly

### Liquidation Cascade
- Correlated collateral (wstETH/ETH e-mode): ETH price drop → mass liquidation
- Cascade amplification: our liquidation + others → price drops further → more liquidations
- Hyperliquid Dec 2024 reference: $200M whale, ETH-heavy, cascaded on funding flip
  Lesson: monitor OI/open leverage ratio on perp exchanges; elevated = cascade risk

---

## Vault Standards (ERC-4626)

- Standardized tokenized vault interface: `deposit`, `withdraw`, `mint`, `redeem`
- `previewDeposit` / `previewWithdraw`: estimate shares/assets before execution
- Inflation attack (small vaults): first depositor can donate assets to inflate share price;
  use minimum initial deposit or virtual shares mitigation
- Yield source opacity: ERC-4626 vault doesn't reveal underlying strategy;
  verify underlying strategy before capital allocation
- Composability risk: vaults nested in vaults create layered smart contract risk

---

## Cross-Chain Bridges

### Bridge Risk Taxonomy
| Risk | Mitigation |
|---|---|
| Smart contract exploit (Ronin, Wormhole, Bybit-adjacent) | Audit status required; TVL cap per bridge |
| Validator collusion | Prefer bridges with large, decentralized validator sets |
| Liquidity fragmentation | Check destination chain liquidity before bridging |
| Finality mismatch | Source chain finality confirmed before bridge acceptance |
| MEV on destination | Use private relay if available on destination chain |

### Bridge Selection Criteria
- Audit: ≥ 2 audits from Tier-1 firms; check audit date (< 6 months preferred)
- Validator set: ≥ 15 independent validators; no single validator > 20% weight
- Maximum capital per bridge per day: 5% of capital; never all-at-once
- Monitor: TVL, recent security events, governance proposals

---

## RWA (Real World Assets) — 2024–2025

- Tokenized T-bills, money market funds (Ondo USDY, Blackrock BUIDL, Superstate):
  on-chain yield backed by off-chain assets
- Risk: off-chain custodian failure, legal enforceability, KYC gate (permissioned tokens)
- RWA as collateral: Aave/MakerDAO accept RWA; adds off-chain counterparty risk to DeFi positions
- Yield accounting: RWA yield is more stable than DeFi yield; use as low-risk anchor
  in yield allocation; subtract custody/legal risk premium (0.5–1%)

---

## DeFi-Specific Monitoring

### On-chain Monitoring (mandatory for any DeFi position)
- Protocol governance: any proposal affecting our positions → review
- TVL anomaly: > 20% TVL drop in 1h → alert; may signal exploit or bank run
- Oracle price deviation: protocol price vs Chainlink reference > 2% → alert
- Liquidation queue depth: approaching our position's HF → prepare preemptive repayment
- Gas price spike: > 3× baseline → defer non-urgent transactions

### Contract Upgrade Monitoring
- Upgradeable proxy: monitor implementation address; change = alert + review
- Admin key transfer: unexpected transfer = possible exploit
- Emergency pause: immediately assess exposure and exit path
- Timelock reduction: admin shortening timelock = governance attack signal
