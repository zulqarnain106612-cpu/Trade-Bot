# On-Chain Data Reference (New in v3)

## Data Provider Taxonomy

| Provider | Strengths | Primary Use |
|---|---|---|
| Glassnode | Deep Bitcoin metrics, on-chain fundamentals, SOPR/NUPL | Bitcoin cycle analysis |
| CryptoQuant | Exchange flow, miner data, derivatives OI, stablecoin supply | Short-term flow signals |
| Nansen | Wallet labeling (smart money, whales), DeFi analytics | Wallet tracking, DeFi flows |
| Dune Analytics | Custom SQL on raw chain data; community dashboards | Custom metrics, protocol analytics |
| Flipside Crypto | Similar to Dune; rewards model; better Solana/NEAR | Solana + cross-chain custom |
| Artemis | Protocol metrics, institutional-grade; API + terminal | Revenue, fees, active users |
| Token Terminal | Revenue-based fundamental metrics; P/S, P/F ratios | Fundamental protocol valuation |
| Chainalysis | AML, compliance, cluster tracing, risk scoring | Compliance (see compliance.md) |
| Elliptic | Similar to Chainalysis; stronger EU presence | Compliance |
| TRM Labs | Risk API; chain analytics; real-time monitoring | Compliance |
| DefiLlama | TVL, protocol fees, DEX volume; free tier available | DeFi monitoring |
| Coinalyze | Derivatives OI, funding rates, liquidations across venues | Derivatives analytics |

---

## Key On-Chain Metrics

### Bitcoin-Specific
```
SOPR (Spent Output Profit Ratio):
  SOPR = Price_sold / Price_paid (for moved coins)
  SOPR > 1: coins moved in profit (sell pressure); < 1: moved at loss
  Correction bounces from SOPR = 1 (holders reluctant to sell at break-even in bull)

MVRV (Market Value to Realized Value):
  MVRV = Market Cap / Realized Cap
  > 3.5: historically overvalued (top signal); < 1: undervalued (bottom signal)

Puell Multiple:
  Daily miner revenue (USD) / 365-day MA of daily miner revenue (USD)
  > 4: miners heavily profitable → selling pressure; < 0.5: capitulation (buy zone)

RHODL Ratio (Realized HODLer Ratio):
  Relative strength of 1-week realized cap vs 1-2 year realized cap
  Peaks in euphoria; troughs in accumulation
```

### Multi-Chain Metrics
```python
def stablecoin_dominance_signal(total_crypto_mcap: float,
                                 stablecoin_mcap: float) -> float:
    """Rising stablecoin dominance = risk-off; falling = risk-on deployment"""
    return stablecoin_mcap / total_crypto_mcap

def exchange_netflow(inflow: float, outflow: float) -> float:
    """Negative (outflow > inflow) = accumulation = bullish"""
    return inflow - outflow

def etf_flow_signal(daily_flows: pd.Series, smoothing: int = 7) -> pd.Series:
    """7-day EMA of BTC spot ETF net flows as institutional demand proxy"""
    return daily_flows.ewm(span=smoothing).mean()
```

### DeFi Protocol Metrics
```
Revenue (fees distributed to protocol / stakers, not all fees)
P/S Ratio = Market Cap / Annualized Protocol Revenue
Fees = Total fees paid by users (supply side + demand side)
TVL = Total Value Locked (use DefiLlama; verify it's excluding double-counting)
TVL Velocity = Transaction volume / TVL (capital efficiency)
```

---

## Signal Construction from On-Chain Data

### Exchange Flow Signal
```python
def exchange_flow_signal(
    inflows: pd.Series,  # Daily BTC inflows to exchanges
    outflows: pd.Series,
    window: int = 7,
) -> pd.Series:
    net_flow = inflows - outflows
    z_score = (net_flow - net_flow.rolling(90).mean()) / net_flow.rolling(90).std()
    # Negative z-score = unusual outflows = accumulation signal (bullish)
    return -z_score.clip(-3, 3)

def whale_accumulation_signal(
    whale_wallet_balances: pd.DataFrame,  # Rows: dates, Cols: wallet addresses
    threshold: float = 1000,  # BTC
) -> pd.Series:
    whale_total = whale_wallet_balances[
        whale_wallet_balances.iloc[-1] > threshold
    ].sum(axis=1)
    return whale_total.pct_change(7)  # 7-day accumulation rate
```

### On-Chain Composite Index
```python
def onchain_composite(
    nupl: float, mvrv: float, sopr: float, exchange_netflow_z: float
) -> float:
    """Composite bull/bear score; range [-1, 1]"""
    components = {
        "nupl": np.clip(nupl, -0.5, 1.0) / 1.0,           # Normalize to [-0.5, 1]
        "mvrv": np.clip((mvrv - 2.0) / 3.0, -1, 1),       # Normalize around 2
        "sopr": np.clip(sopr - 1.0, -0.5, 0.5) / 0.5,    # Distance from 1
        "flow": np.clip(-exchange_netflow_z / 3, -1, 1),   # Inverted
    }
    weights = {"nupl": 0.30, "mvrv": 0.30, "sopr": 0.20, "flow": 0.20}
    return sum(w * components[k] for k, w in weights.items())
```

---

## Mempool Data

### Use Cases
- Pending large transactions: whale alert; adjust expectations for slippage
- Fee rate distribution: estimate inclusion time for our transactions
- MEV opportunity detection: large DEX trades pending → frontrun / backrun analysis
- Network congestion: high mempool size → elevated gas; defer non-urgent txs

### Data Sources
- BTC: mempool.space API (`/api/mempool`, `/api/fees/recommended`)
- ETH: Blocknative (real-time mempool stream), Alchemy Pending Transactions
  WebSocket, Infura Pending Transaction Notifications
- Flashbots bundle API: stream of pending bundles (limited visibility by design)

```python
async def get_btc_fee_estimate() -> dict:
    """Returns recommended fee rates in sat/vByte"""
    async with aiohttp.ClientSession() as s:
        r = await s.get("https://mempool.space/api/fees/recommended")
        return await r.json()
    # Returns: {"fastestFee": 15, "halfHourFee": 12, "hourFee": 8, "minimumFee": 2}

async def watch_pending_large_eth_txs(web3, min_value_eth: float = 100):
    """Stream pending transactions above threshold"""
    sub = await web3.eth.subscribe("pendingTransactions")
    async for tx_hash in sub:
        tx = await web3.eth.get_transaction(tx_hash)
        if tx and tx.value > web3.to_wei(min_value_eth, "ether"):
            yield tx
```

---

## Blockchain Data Engineering

### Node Types
- Archive node: full historical state; required for historical balance queries
- Full node: recent state (default ~128 blocks); cheaper storage; sufficient for real-time
- Light node: headers only; cannot query state independently
- RPC endpoint: Alchemy, Infura, QuickNode, Ankr (managed); self-hosted (most reliable for prod)

### Rate Limits and Reliability
- Public RPCs: unreliable; never use in production
- Alchemy: 330M CUs/month on Growth plan; monitor usage
- Multiple providers: failover between Alchemy, Infura, QuickNode; circuit breaker
- Self-hosted node: optimal for latency-sensitive on-chain data; 6–12TB SSD for ETH archive

### Event Indexing
```python
# Preferred: use event logs (efficient, indexed)
from web3.middleware import geth_poa_middleware

def index_transfer_events(contract, from_block: int, to_block: int):
    event_filter = contract.events.Transfer.create_filter(
        fromBlock=from_block, toBlock=to_block
    )
    return event_filter.get_all_entries()

# For large ranges: use eth_getLogs directly with chunking
async def get_logs_chunked(w3, filter_params, chunk=2000):
    from_block = filter_params["fromBlock"]
    to_block = filter_params["toBlock"]
    results = []
    for start in range(from_block, to_block, chunk):
        end = min(start + chunk - 1, to_block)
        chunk_logs = await w3.eth.get_logs({**filter_params,
                                             "fromBlock": start, "toBlock": end})
        results.extend(chunk_logs)
        await asyncio.sleep(0.05)  # Rate limit protection
    return results
```

### Data Pipeline Architecture
```
Blockchain Node (RPC)
      │
      ▼
Ingestion Layer (event polling / WebSocket subscription)
      │
      ▼
Normalization & Decode (ABI decoding, address labeling)
      │
      ▼
TimescaleDB / ClickHouse (time-series storage, fast aggregation)
      │
      ▼
Feature Engine (aggregate metrics → trading signals)
```

- TimescaleDB: Postgres extension; compression; continuous aggregates (materialized views)
- ClickHouse: better for large-scale aggregations; native columnar; sub-second queries
  on billions of rows
- Point-in-time correctness: store block timestamp alongside every record;
  never assume record time = block time; use `block.timestamp` from chain

---

## Data Quality and Freshness

### Validation Rules
- Freshness: on-chain data must be < 2 blocks old for real-time signals; alert otherwise
- Completeness: reconcile indexed event count against expected (spot-check block range)
- Re-org handling: store block hash per record; detect re-org on hash change → replay
- Schema validation: every decoded event validated against expected ABI schema;
  unexpected field = alert, skip record

### Staleness Handling
```python
def is_stale(record_block: int, current_block: int, max_lag: int = 5) -> bool:
    return (current_block - record_block) > max_lag

def get_validated_onchain_signal(
    raw_signal: float, record_block: int, current_block: int
) -> Optional[float]:
    if is_stale(record_block, current_block):
        logger.warning("on_chain_signal_stale", extra={
            "record_block": record_block, "current": current_block,
            "lag": current_block - record_block
        })
        return None  # Do not use stale on-chain data in signal
    return raw_signal
```

---

## Solana-Specific

- Program event logs: parse via `Program data:` log lines; base58 decode + Borsh/Anchor deserialization
- Account state: subscribe via `accountSubscribe` WebSocket for real-time balance changes
- Compressed NFTs: Merkle tree state; do not use standard `getTokenAccountsByOwner`
- Transaction finalization: use `confirmed` commitment for signals; `finalized` for settlement
- Data providers: Flipside, Helius (RPC + webhook), Dune (Solana tables since 2024)
