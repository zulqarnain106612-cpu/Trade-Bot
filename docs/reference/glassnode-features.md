# Glassnode Feature Replacement Guide
## Free Stack Integration for Claude Desktop

> **Purpose:** This document tells Claude Desktop exactly how to replace Glassnode features
> using free, official APIs during project development. When the project matures, swap
> each section's source for the paid Glassnode tier.

---

## Architecture Overview

```
Your Project
    │
    ├── Wallet & Entity Intelligence   →  Arkham Intel API   (api.arkm.com)
    ├── DeFi / TVL / Protocol Data     →  DeFiLlama API      (api.llama.fi)
    ├── Custom On-Chain Queries        →  Dune Analytics API (api.dune.com)
    ├── Exchange Flows / BTC Signals   →  CryptoQuant        (cryptoquant.com — free tier)
    └── Derivatives / Liquidations     →  Coinglass          (open.coinglass.com)
```

---

## 1. Arkham Intel API — Wallet & Entity Intelligence

**Official Docs:** https://intel.arkm.com/api/docs
**API Reference:** https://docs.intel.arkm.com
**LLM-Friendly Docs:** https://intel.arkm.com/llms.txt
**Request Access:** https://intel.arkm.com/api
**Support:** api@arkm.com
**Base URL:** `https://api.arkm.com`
**Auth:** `API-Key` header required on all requests
**Rate Limit (Basic tier):** 20 requests/second

### What Arkham Replaces from Glassnode
| Glassnode Feature | Arkham Equivalent |
|---|---|
| Exchange wallet balances | Entity portfolio endpoint |
| Whale wallet tracking | Transfer + histogram endpoints |
| Fund flow analysis | Fund flow visualizer data |
| Holder distribution | Entity address aggregation |
| Exchange inflow/outflow | Transfer direction filters (`in`/`out`) |

### Key Endpoints

#### Get Entity Summary
```http
GET https://api.arkm.com/intelligence/entity/{entity}/summary
API-Key: YOUR_API_KEY
```
Returns: total addresses, volume USD, balance USD, first/last transaction.

**Example (Binance):**
```bash
curl -X GET "https://api.arkm.com/intelligence/entity/binance/summary" \
  -H "API-Key: YOUR_API_KEY"
```
```json
{
  "entityId": "binance",
  "numAddresses": 1250,
  "volumeUsd": 15000000000000.50,
  "balanceUsd": 85000000000.25,
  "firstTx": "2017-07-14T04:00:00Z",
  "lastTx": "2026-01-26T12:30:00Z"
}
```

#### Get Transfers (Fund Flows)
```http
GET https://api.arkm.com/intelligence/transfers
API-Key: YOUR_API_KEY
```
| Parameter | Type | Description |
|---|---|---|
| `flow` | string | `in`, `out`, `self`, `all` |
| `from` | string | Comma-separated addresses/entities |
| `to` | string | Comma-separated addresses/entities |
| `tokens` | string | Comma-separated token list |

> ⚠️ Heavy endpoint — rate limit: 1 request/second

#### Get Entity Historical USD Value
```http
GET https://api.arkm.com/intelligence/entity/{entity}/historical
API-Key: YOUR_API_KEY
```
Returns time-series snapshots of entity holdings for tracking evolution over time.

#### Get Supported Chains
```http
GET https://api.arkm.com/chains
API-Key: YOUR_API_KEY
```
Returns array of supported chain identifiers.
Supported in 2026: Bitcoin, Ethereum, Solana, BNB Chain, Avalanche, Tron, Arbitrum, Polygon, Optimism, Base, Mantle, Zcash, and more.

### Python Integration
```python
import requests

ARKHAM_API_KEY = "your_api_key_here"  # pragma: allowlist secret
BASE_URL = "https://api.arkm.com"

headers = {"API-Key": ARKHAM_API_KEY}

def get_entity_summary(entity_id: str):
    url = f"{BASE_URL}/intelligence/entity/{entity_id}/summary"
    response = requests.get(url, headers=headers)
    return response.json()

def get_fund_flows(entity: str, direction: str = "all"):
    url = f"{BASE_URL}/intelligence/transfers"
    params = {"from": entity, "flow": direction}
    response = requests.get(url, headers=headers, params=params)
    return response.json()

# Usage
binance_data = get_entity_summary("binance")
outflows = get_fund_flows("binance", "out")
```

---

## 2. DeFiLlama API — TVL, Protocol & DeFi Data

**Official Docs:** https://api-docs.defillama.com
**Official Python SDK:** https://pypi.org/project/defillama-sdk (by DefiLlama/0xngmi)
**GitHub SDK:** https://github.com/DefiLlama/defillama-sdk
**Base URL (Free):** `https://api.llama.fi`
**Auth:** None required for free tier
**Rate Limit:** No enforced limit for normal traffic

### What DeFiLlama Replaces from Glassnode
| Glassnode Feature | DeFiLlama Equivalent |
|---|---|
| Network health metrics | Chain TVL and protocol TVL |
| Exchange liquidity | DEX volume endpoints |
| Stablecoin supply/flows | Stablecoins endpoints |
| Protocol revenue | Fees and revenue endpoints |
| Yield/lending rates | Yields pools endpoint |

### Key Endpoints

#### Get All Protocols with TVL
```http
GET https://api.llama.fi/protocols
```
Returns: id, name, symbol, category, chains, tvl, chainTvls, change_1d, change_7d

**Example Response:**
```json
[{
  "id": "2269",
  "name": "Aave",
  "symbol": "AAVE",
  "category": "Lending",
  "chains": ["Ethereum", "Polygon"],
  "tvl": 5200000000,
  "chainTvls": { "Ethereum": 3200000000, "Polygon": 2000000000 },
  "change_1d": 2.1,
  "change_7d": -5.3
}]
```

#### Get Historical Chain TVL
```http
GET https://api.llama.fi/v2/historicalChainTvl
GET https://api.llama.fi/v2/historicalChainTvl/{chain}
```

#### Get Protocol Detail
```http
GET https://api.llama.fi/protocol/{protocol_name}
```

#### Get Current Token Prices
```http
GET https://coins.llama.fi/prices/current/{coins}
```
`coins` format: `ethereum:0xTOKENADDRESS` or `coingecko:bitcoin`

#### Get Stablecoin Data
```http
GET https://stablecoins.llama.fi/stablecoins
```

#### Get Yield Pools
```http
GET https://yields.llama.fi/pools
```

### Python Integration (Official SDK)
```python
# Install: pip install defillama-sdk
from defillama_sdk import DefiLlama

# Free tier — no API key needed
client = DefiLlama()

# Get all protocols
protocols = client.tvl.getProtocols()

# Get specific protocol
aave = client.tvl.getProtocol("aave")

# Get chain TVL history
eth_history = client.tvl.getHistoricalChainTvl("Ethereum")
all_chains = client.tvl.getHistoricalChainTvl()

# Get current token prices
prices = client.prices.getCurrentPrices([
    "ethereum:0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",  # USDC
    "coingecko:bitcoin"
])
```

### Error Handling
```python
from defillama_sdk import ApiKeyRequiredError, RateLimitError, NotFoundError, ApiError

try:
    data = client.tvl.getProtocol("aave")
except NotFoundError:
    print("Protocol not found")
except RateLimitError as exc:
    print(f"Rate limited. Retry after {exc.retry_after}s")
except ApiError as exc:
    print(f"API error: {exc.status_code}")
```

---

## 3. Dune Analytics API — Custom On-Chain SQL Queries

**Official Docs:** https://docs.dune.com/api-reference/overview/introduction
**Dashboard:** https://dune.com
**Base URL:** `https://api.dune.com/api/v1`
**Auth:** `X-Dune-API-Key` header
**Free Tier:** Available — limited query executions per month

### What Dune Replaces from Glassnode
| Glassnode Feature | Dune Equivalent |
|---|---|
| MVRV ratio (community) | Public MVRV dashboards |
| SOPR (community) | Public SOPR query dashboards |
| Active addresses | Custom SQL on raw chain data |
| Transaction volume | Custom SQL aggregations |
| Holder distribution | Custom SQL cohort queries |

### Key Endpoints

#### Execute a Query
```http
POST https://api.dune.com/api/v1/query/{query_id}/execute
X-Dune-API-Key: YOUR_API_KEY
Content-Type: application/json
```

#### Get Execution Results
```http
GET https://api.dune.com/api/v1/execution/{execution_id}/results
X-Dune-API-Key: YOUR_API_KEY
```

#### Get Latest Query Results (no execution needed)
```http
GET https://api.dune.com/api/v1/query/{query_id}/results
X-Dune-API-Key: YOUR_API_KEY
```

### Python Integration
```python
import requests
import time

DUNE_API_KEY = "your_dune_api_key"  # pragma: allowlist secret
BASE_URL = "https://api.dune.com/api/v1"
headers = {"X-Dune-API-Key": DUNE_API_KEY}

def get_query_results(query_id: int):
    # Try to get latest cached results first
    url = f"{BASE_URL}/query/{query_id}/results"
    response = requests.get(url, headers=headers)
    return response.json()

def execute_and_wait(query_id: int, parameters: dict = {}):
    # Execute
    exec_url = f"{BASE_URL}/query/{query_id}/execute"
    exec_resp = requests.post(exec_url, headers=headers, json={"query_parameters": parameters})
    execution_id = exec_resp.json()["execution_id"]

    # Poll for results
    while True:
        status_url = f"{BASE_URL}/execution/{execution_id}/results"
        result = requests.get(status_url, headers=headers).json()
        if result.get("state") == "QUERY_STATE_COMPLETED":
            return result["result"]["rows"]
        time.sleep(2)

# Usage — use a public community query ID
# Bitcoin MVRV community query example
mvrv_data = get_query_results(3295227)
```

### Recommended Public Dashboards (use their query IDs)
Search on dune.com for:
- `Bitcoin MVRV` — BTC market value to realized value
- `Bitcoin SOPR` — Spent output profit ratio
- `ETH active addresses` — Daily active Ethereum addresses
- `Stablecoin flows` — USDT/USDC exchange flows

---

## 4. CryptoQuant — Exchange Flows & BTC Signals

**Platform:** https://cryptoquant.com
**Free Tier:** Available (limited metrics, daily resolution)
**Paid starts:** $29/month (Basic)

### What CryptoQuant Replaces from Glassnode
| Glassnode Feature | CryptoQuant Equivalent |
|---|---|
| Exchange reserve (BTC) | Exchange reserve metric |
| Exchange inflow/outflow | Exchange flow metrics |
| Miner outflow | Miner flow metrics |
| Funding rates | Derivatives section |
| Bull/Bear market signals | Market indicator section |

> **Note:** CryptoQuant's free tier does not expose a public REST API.
> Use it manually via the dashboard during development, or upgrade to
> Basic ($29/mo) for API access. When ready to go paid, this is the
> most affordable API entry point before Glassnode.

---

## 5. Coinglass — Derivatives & Liquidation Data

**Official API Docs:** https://docs.coinglass.com
**Base URL:** `https://open-api.coinglass.com/public/v2`
**Auth:** `coinglassSecret` header
**Free Tier:** Available with API key (register at coinglass.com)

### What Coinglass Replaces from Glassnode
| Glassnode Feature | Coinglass Equivalent |
|---|---|
| Open interest | Open interest endpoint |
| Funding rates | Funding rate endpoint |
| Liquidations | Liquidation endpoint |
| Long/short ratio | Long/short ratio endpoint |

### Key Endpoints

#### Funding Rates
```http
GET https://open-api.coinglass.com/public/v2/funding
coinglassSecret: YOUR_KEY
```

#### Open Interest
```http
GET https://open-api.coinglass.com/public/v2/open_interest
coinglassSecret: YOUR_KEY
```

#### Liquidations
```http
GET https://open-api.coinglass.com/public/v2/liquidation_history
coinglassSecret: YOUR_KEY
```

---

## 6. Feature-to-API Mapping (Quick Reference)

| Glassnode Feature | Free Replacement | API / Source |
|---|---|---|
| Exchange reserve | CryptoQuant dashboard | Manual / $29 API |
| Exchange inflow/outflow | Arkham transfers endpoint | `api.arkm.com/intelligence/transfers` |
| Whale wallet tracking | Arkham entity summary | `api.arkm.com/intelligence/entity/{id}/summary` |
| Fund flow visualization | Arkham historical | `api.arkm.com/intelligence/entity/{id}/historical` |
| Protocol TVL | DeFiLlama protocols | `api.llama.fi/protocols` |
| Chain TVL history | DeFiLlama chain TVL | `api.llama.fi/v2/historicalChainTvl` |
| Stablecoin supply | DeFiLlama stablecoins | `stablecoins.llama.fi/stablecoins` |
| Token prices | DeFiLlama prices | `coins.llama.fi/prices/current/{coins}` |
| DeFi yields | DeFiLlama yields | `yields.llama.fi/pools` |
| MVRV / SOPR | Dune community queries | `api.dune.com/api/v1/query/{id}/results` |
| Open interest | Coinglass | `open-api.coinglass.com/public/v2/open_interest` |
| Funding rates | Coinglass | `open-api.coinglass.com/public/v2/funding` |
| Liquidations | Coinglass | `open-api.coinglass.com/public/v2/liquidation_history` |

---

## 7. Migration Path to Glassnode (When Ready)

When the project is production-ready:

```
Step 1: Start with CryptoQuant Basic ($29/mo)
        → Replaces manual CryptoQuant dashboard usage
        → Gives full REST API for exchange flows and BTC signals

Step 2: Upgrade to Glassnode Advanced ($49/mo)
        → 270+ on-chain metrics
        → Daily resolution, 4 years history
        → API Light (14 days history, 50 calls/day)

Step 3: Swap API calls one by one
        → Replace DeFiLlama TVL calls with Glassnode network metrics
        → Replace Dune MVRV/SOPR with Glassnode native metrics
        → Replace Arkham entity summaries with Glassnode exchange data

Step 4: Glassnode Professional (custom pricing)
        → 570+ metrics, 10-minute resolution
        → 15+ years of history
        → Full API access
```

---

## 8. Environment Variables Template

```env
# Arkham Intel API
ARKHAM_API_KEY=your_arkham_api_key_here
ARKHAM_BASE_URL=https://api.arkm.com

# DeFiLlama (no key needed for free tier)
DEFILLAMA_BASE_URL=https://api.llama.fi
DEFILLAMA_PRICES_URL=https://coins.llama.fi
DEFILLAMA_YIELDS_URL=https://yields.llama.fi
DEFILLAMA_STABLECOINS_URL=https://stablecoins.llama.fi

# Dune Analytics
DUNE_API_KEY=your_dune_api_key_here
DUNE_BASE_URL=https://api.dune.com/api/v1

# Coinglass
COINGLASS_API_KEY=your_coinglass_key_here
COINGLASS_BASE_URL=https://open-api.coinglass.com/public/v2

# Future — Glassnode (leave empty until project goes paid)
GLASSNODE_API_KEY=
GLASSNODE_BASE_URL=https://api.glassnode.com/v1
```

---

## 9. Official Links Summary

| Platform | Docs | API Access | Support |
|---|---|---|---|
| Arkham | https://intel.arkm.com/api/docs | https://intel.arkm.com/api | api@arkm.com |
| DeFiLlama | https://api-docs.defillama.com | No key needed | GitHub issues |
| Dune Analytics | https://docs.dune.com | https://dune.com/settings/api | Discord |
| Coinglass | https://docs.coinglass.com | https://coinglass.com | Support page |
| CryptoQuant | https://cryptoquant.com | Upgrade to Basic | In-app support |
| Glassnode (future) | https://docs.glassnode.com | https://studio.glassnode.com | Pro support |

---

*Last updated: July 2026 | All APIs verified against official documentation*
