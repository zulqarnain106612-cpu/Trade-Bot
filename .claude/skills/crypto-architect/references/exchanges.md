# Exchanges Reference

## Exchange Taxonomy

### Centralized (CEX)
- Off-chain matching engine; instant fill notification
- Counter-party risk: exchange custody of funds (FTX 2022; Bybit hack 2025)
- Withdraw limits, KYC/AML, geo-restrictions apply
- Key players (2025): Binance, OKX, Bybit, Kraken, Coinbase Advanced, HTX, Gate.io

### Decentralized (DEX)
- On-chain settlement; subject to finality, gas, MEV (see `defi.md`)
- AMM: Uniswap v3/v4, Curve — price from liquidity pool formula
- Order book DEX: dYdX v4 (Cosmos chain), Hyperliquid (custom L1), CLOB on Solana
- No counter-party custody risk; smart contract risk instead

### Hyperliquid (2024–2025)
- Custom L1 chain (HyperBFT consensus): fully on-chain order book + matching engine
- Sub-100ms finality; EVM-compatible (HyperEVM) for smart contracts
- Native token (HYPE): used for fees and staking; launched Nov 2024
- Dec 2024 cascade event: $200M ETH whale liquidation cascaded; HLP (liquidity pool)
  absorbed $4M loss via socialized loss mechanism
  Lessons: monitor HLP vault P&L; large OI → cascade risk; ADL rank monitoring mandatory
- API: REST + WebSocket; similar authentication to CEX; Ed25519 signing preferred
- Risk: smaller validator set than ETH; concentrated team; infrastructure single points
- HyperEVM: deploy contracts on Hyperliquid's EVM; interacts with perp state;
  powerful but novel risk surface; treat as alpha-stage infrastructure

### Derivatives Venues
- Perp futures: Binance Futures, Bybit, OKX, dYdX v4, Hyperliquid
- Options: Deribit (dominant), Lyra v2 (Arbitrum), Premia v3, Aevo
- Prediction markets: Polymarket (Polygon, USDC-settled), Limitless (Gnosis)

---

## Order Types — Architect Constraints

| Order Type | Use Case | Risk |
|---|---|---|
| Limit | Entry/exit at target price | Non-fill risk, partial fill |
| Market | Immediate execution | Slippage, price impact |
| Stop-Limit | Stop loss with price control | Gap risk |
| Stop-Market | Guaranteed stop exit | Slippage in illiquid conditions |
| Iceberg | Large order concealment | Latency cost, partial fill tracking |
| TWAP/VWAP | Large order execution | Timing risk, information leakage |
| Post-Only | Maker-only (zero/negative fee) | Non-fill on aggressive market |
| Reduce-Only | Close existing position only | Rejected if no position |
| TP/SL linked | Exchange-native OCO | Exchange dependency for risk control |
| RFQ | Large block, off-screen | Counterparty trust, fill certainty |

- **Stop-Market preferred for protective stops** — gap risk outweighs slippage
- Reduce-Only: mandatory on closing orders in perpetual systems to prevent
  accidental position flip
- RFQ: request-for-quote systems (Binance OTC, OKX block trading, Paradigm) for
  large orders; better price than lit market; requires ISDA/ISMA-equivalent agreement

---

## Prediction Markets (Polymarket et al.)

- Binary outcome markets settled by UMA or Chainlink oracle resolution
- Liquidity: varies wildly by event; check depth before sizing
- Settlement risk: oracle manipulation or disputed resolution can delay/misdirect payout
- Gas cost: Polygon-based; cheap but add gas to cost model
- Strategy: event-driven, information asymmetry (sentiment, news analytics)
- Regulatory: CFTC jurisdiction over US participants unclear; enforce geo-restrictions

---

## FIX Protocol

- Binance: FIX 4.2 API; requires dedicated session key
- Message types: `D`=NewOrderSingle, `F`=Cancel, `G`=CancelReplace, `8`=ExecutionReport
- Sequence numbers: track per session; gap = disconnect and resync
- Latency: FIX ~0.5–2ms vs REST ~5–20ms; worth it for HFT only
- Never implement FIX parser from scratch — use QuickFIX/n, SimplyFix, or Stretto

---

## Exchange API Architecture

### WebSocket Management
- Heartbeat/ping-pong: send ping every 20–30s; missing pong → reconnect
- Reconnect: exponential backoff 1s, 2s, 4s, 8s, max 60s
- On reconnect: **reconcile all open orders via REST before resuming execution**
- Sequence number gap = missed message → reconcile immediately

### Rate Limits
- Maintain in-memory rate limit budget; throttle before limit hit
- Weight-based (Binance): track per endpoint per window
- 429 → back off immediately; do not retry within window
- HTTP 418 (IP ban): rotate to secondary IP immediately; alert

### Authentication
- HMAC-SHA256 (most CEX): sign `timestamp + method + path + body`
- Ed25519 (Bybit, Hyperliquid): preferred — smaller signature, faster
- Timestamp window: exchange rejects outside ±5s; NTP sync mandatory
- API key permissions: trade + read only; **never withdrawal permission on trading key**
- IP allowlist: restrict to trading server IPs; any API key without IP allowlist = HIGH risk

### Venue Health Scoring (new in v3)
```python
@dataclass
class VenueHealth:
    avg_latency_ms: float  # rolling 5-min p95 API latency
    fill_rate: float  # recent fills / submitted orders
    api_error_rate: float  # 5xx / total requests
    funding_anomaly: bool  # extreme funding rate vs composite
    status: str  # HEALTHY | DEGRADED | UNHEALTHY


def score_venue(h: VenueHealth) -> str:
    if h.api_error_rate > 0.05 or h.avg_latency_ms > 500:
        return "UNHEALTHY"
    if h.fill_rate < 0.85 or h.funding_anomaly:
        return "DEGRADED"
    return "HEALTHY"
```
- DEGRADED → reduce position size and order rate on that venue
- UNHEALTHY → route new orders to secondary venue; manage existing positions only

---

## Order Book Mechanics

### Depth and Liquidity
- Level 1: best bid/ask — insufficient for large orders
- Level 2: full depth (top 20–50 levels) — required for slippage estimation
- Level 3: individual orders — OFI analysis (see `market-microstructure.md`)
- Imbalance ratio: `(bid_vol - ask_vol) / (bid_vol + ask_vol)` — directional signal

### Slippage Estimation
```
Expected Slippage = Σ(level_price - mid) × level_size / order_size
```
- Compute before submission for any order > 0.1% of visible depth
- Dynamic slippage model: calibrate to actual fill data per exchange

### Market Impact Model (Almgren-Chriss)
```
Temporary impact:  η × (v / σ) × sign(v)
Permanent impact:  γ × v
Square-root approx: Impact ≈ σ × (Q / ADV)^0.5
```
- `v` = participation rate (fraction of ADV)
- Calibrate `η` and `γ` per exchange on actual fill data

---

## Co-location and Market Access

| Access Type | RTT to Exchange | Cost | Use Case |
|---|---|---|---|
| Remote (cloud) | 5–50 ms | Low | Swing, day trading |
| Same-region cloud | 1–5 ms | Medium | Day trading, arb |
| Co-located (exchange DC) | < 1 ms | High | HFT, latency arb |
| Kernel bypass (co-lo) | < 200 μs | Very high | Pure HFT |

---

## Exchange Counter-party Risk

- Never hold > 30% of trading capital on single exchange
- Withdrawal schedule: sweep profits off-exchange on defined schedule
- Cold storage ratio: > 60% of total capital in cold storage
- Proof of Reserves (PoR): check audit recency; Merkle-tree PoR preferred
- Bybit Feb 2025 hack ($1.5B): custody of funds at risk even with PoR;
  include exchange hack in tail-risk scenario; position insurance accordingly
- Insurance fund (perps): monitor size; depletion → socialized loss risk for profitable positions

---

## Funding Rate (Perpetual Futures)

```
Funding Payment = Position Size × Funding Rate (every 8h typically)
```
- Positive funding: longs pay shorts (market bullish)
- Extreme funding (> 0.1% per 8h): liquidity risk signal; reduce position
- Funding arbitrage: long spot + short perp when funding positive; account for borrow cost
- Hyperliquid Dec 2024: funding spiked to extreme on ETH before cascade;
  extreme funding = early cascade warning signal; monitor cross-venue composite
- OI-weighted funding composite: compute effective funding across positions
  to get true carry cost
