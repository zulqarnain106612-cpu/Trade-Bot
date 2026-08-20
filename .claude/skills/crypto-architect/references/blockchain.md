# Blockchain Reference

## Consensus Mechanisms — Architect Implications

### Proof of Work (PoW)
- Finality: probabilistic — 6-block confirmation standard for BTC (~60 min)
- Reorg risk: non-zero below 6 confirmations; never treat unconfirmed tx as final
- Mempool congestion directly impacts fee rate and confirmation time
- Hashrate drop → increased reorg probability → tighten confirmation requirements

### Proof of Stake (PoS)
- ETH: finality at 2 epochs (~12 min); Single Slot Finality (SSF) on roadmap
- SSF target: finality per slot (12s) — when deployed, confirmation requirements drop
  dramatically; monitor EIP progress before assuming SSF is live
- Slashing risk: validator misbehavior → stake loss; impacts restaking strategies
- Checkpoint finality: treat pre-finality blocks as reversible in settlement logic
- LMD-GHOST fork choice: heaviest-attested chain wins; affects reorg depth

### Delegated PoS / BFT variants (SOL, BNB, AVAX, etc.)
- Near-instant finality (< 1s on Solana) → lower confirmation requirement
- Validator set concentration = centralization risk; factor into counter-party risk model
- Solana: tower BFT + PoH (Proof of History); network halts have occurred — runbook required
- Network partition susceptibility: monitor validator health endpoints

---

## Layer 2 Scaling

### Optimistic Rollups (Arbitrum One, Optimism, Base)
- Transactions batched to L1 as calldata / blobs (post-EIP-4844)
- Fraud proof window: 7 days before L1 finality for withdrawals
- Instant finality on L2 sequencer; do NOT trust for large withdrawals without L1 confirmation
- Sequencer centralization risk: single sequencer failure = L2 halt; check failsafe
  (Arbitrum: delayed inbox force-include; Optimism: same)
- Forced inclusion: transactions can be forced through L1 if sequencer censors
- Fault proofs live on Optimism and Base mainnet (2024); Arbitrum BOLD imminent

### ZK Rollups (zkSync Era, Starknet, Polygon zkEVM, Scroll, Linea)
- Validity proof generated per batch → cryptographic finality, no fraud window
- Withdrawal time: proof generation time (minutes to hours depending on prover)
- EVM equivalence varies: check opcode support before deploying contracts
- Prover cost: ZK proof generation expensive; reflected in L2 transaction fees
- Proof aggregation (2024–2025): multiple rollup proofs aggregated into one L1 proof
  (AggLayer / Polygon 2.0, Succinct Plonky3) — reduces per-tx proving cost

### Validium / Volition
- Data stored off-chain (committee or DAC); cheaper than rollup, weaker data availability
- Risk: if data committee colludes, funds may be unrecoverable
- Never hold significant capital in Validium without understanding the DA committee

### EIP-4844 (Proto-Danksharding, live Mar 2024)
- Blob transactions reduce L2 data posting costs by 10–100×
- Blobs pruned after ~18 days — do not rely on blob data for long-term record-keeping
- Blob fee market separate from gas fee market; `blob_base_fee` tracks blob demand
- Impact: L2 fees collapsed post-4844; strategies depending on high L2 fee arbitrage
  must be re-evaluated

### L2 Interoperability (2024–2025)
- OP Superchain (Base, Optimism, Zora, Mode): shared sequencing roadmap; atomic
  cross-chain transactions within Superchain eventually; not yet live
- AggLayer (Polygon): unified ZK proof aggregation for multiple chains
- ERC-7683 (Cross-Chain Intents Standard): standardized intent expression across
  chains — monitor adoption for cross-chain arbitrage strategies

---

## Finality Reference Table

| Chain | Mechanism | Practical Finality | Reorg Risk |
|---|---|---|---|
| Bitcoin | PoW | ~60 min (6 blocks) | Low after 3 blocks |
| Ethereum | PoS | ~12 min (2 epochs) | Negligible post-finality |
| Solana | PoH+PoS | ~400 ms | Low; monitor validator health |
| BNB Chain | DPoS | ~3 s (15 blocks) | Low |
| Avalanche | Snowball | ~1 s | Very low |
| Polygon PoS | PoS | ~2 min | Checkpoint to ETH |
| Arbitrum | Optimistic rollup | 7 days (L1) / instant (sequencer) | Sequencer SPOF |
| zkSync Era | ZK rollup | ~1 h (proof gen) | Cryptographic post-proof |
| Starknet | ZK rollup | Hours (proof gen) | Cryptographic post-proof |
| Base | Optimistic rollup | 7 days (L1) / instant (sequencer) | Sequencer SPOF |

---

## Pectra Upgrade (ETH, 2025)

- EIP-7702: account abstraction for EOAs — EOAs can temporarily act as smart accounts
  by setting code for a transaction; enables session keys, batch transactions, gas
  sponsorship without full ERC-4337 deployment
  Architect: EOA behavior is now programmable; validate that contract detection logic
  (`code.length > 0`) still correctly identifies contract vs EOA contexts
- EIP-7251: increase max effective balance for validators (from 32 ETH to 2048 ETH)
  — consolidation of validators; reduces p2p load; affects restaking projections
- EIP-7002: execution-layer triggerable withdrawals — validators can initiate withdrawals
  from smart contracts without validator signing key online
- EIP-2537: BLS12-381 precompile — cheaper BLS operations on-chain; enables
  efficient on-chain BLS signature verification for threshold protocols

---

## Mempool Mechanics

- Unconfirmed transactions are not guaranteed to execute
- RBF (Replace-by-Fee): higher fee replaces pending tx with same nonce (ETH) or inputs (BTC)
- MEV: front-running, sandwich, arbitrage by block builders — see `defi.md`
- Use private mempools (Flashbots Protect, MEV Blocker) for sensitive orders
- Nonce management (ETH): sequential, per-address; out-of-order nonce = stuck tx
  Maintain nonce counter in persistent state; sync with chain on startup
- EIP-4337 UserOp mempool: separate mempool; bundlers aggregate; same MEV considerations apply

---

## Gas / Fee Estimation

### EVM Chains (ETH, BNB, AVAX, Polygon)
- EIP-1559: `baseFee` (burned) + `maxPriorityFee` (tip) + `maxFee` cap
- `baseFee` auto-adjusts per block (target 50% full); fetch via `eth_gasPrice` or fee history
- Blob fee market (post-EIP-4844): separate `blob_base_fee`; required for L2 calldata tx
- Under-estimate gas → tx reverts (gas consumed, value not transferred)
- Strategy: estimate gas × 1.2 buffer; cap at `maxFee`; monitor inclusion latency
- `eth_feeHistory` API: use for percentile-based fee estimation across N blocks

### Non-EVM
- BTC: fee = sat/vByte; estimate from mempool fee histogram (mempool.space API)
  Taproot inputs save ~30% fee vs legacy; default to bech32m for new outputs
- SOL: fixed 5000 lamports base + compute unit price; priority fees for faster inclusion
  Compute Budget Program: `setComputeUnitPrice` for priority; `setComputeUnitLimit` to cap

---

## Account Abstraction

### ERC-4337 (EntryPoint v0.7, 2024)
- UserOperations processed via EntryPoint contract; supports paymasters and aggregators
- Paymasters: allow gas sponsorship or payment in ERC-20 tokens
- Session keys: limited-capability keys for dApp interactions (reduced key exposure)
- Bundlers: aggregate UserOps; competitive market; use multiple bundler endpoints
- ERC-4337 v0.7 changes: reduced calldata costs, improved gas accounting, new
  `PAYMASTER_POST_OP_GAS` mechanics — test against v0.7 if migrating from v0.6

### EIP-7702 (Pectra)
- EOAs temporarily adopt smart account code for one transaction
- Use cases: batch approve+swap atomically, sponsor gas for user, authorize session key
- Security: the code set can be malicious; validate delegate contract address strictly
- Does not persist: code reverts to empty after tx unless re-set; stateless

---

## Smart Contract Risk

- Audit status: ≥ 2 audits from Tier-1 firms (OpenZeppelin, Trail of Bits, Spearbit,
  Cantina, Code4rena top auditors); unaudited = HIGH risk, block capital allocation
- TVL concentration: large TVL ≠ safe; check audit recency and scope
- Upgrade proxy risk: admin key can alter contract logic; check multisig/timelock
  (< 24h timelock = HIGH risk; no timelock = CRITICAL risk)
- Oracle manipulation: price feed attacks common — see `defi.md`
- TWAP (time-weighted average price) not spot for any on-chain valuation
- Chainlink + secondary oracle for redundancy on any price-sensitive logic
- Reentrancy: check-effects-interactions pattern; ReentrancyGuard on all state mutations
- Transient storage (EIP-1153, live Cancun 2024): `TSTORE`/`TLOAD` for same-tx state;
  can be used for more efficient reentrancy guards; understand semantics before use

## Chain Reorganization Handling

- Maintain block hash with every on-chain event recorded
- On reorg: replay events from fork point; flag affected positions
- Reorg detection: compare current head to stored head — hash mismatch = reorg
- Never update portfolio state from on-chain events without reorg protection
- Reorg depth monitoring: alert on any reorg > 2 blocks on PoS chains (unusual)
- Solana: epochs and forks handled differently; use confirmed/finalized commitment levels
