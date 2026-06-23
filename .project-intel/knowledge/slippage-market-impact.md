# Slippage & Market Impact Model
**Domain**: risk | **Tags**: slippage, impact, market, spread, almgren, chriss, execution, cost, fill

## Slippage & Market Impact — Architecture Reference (GAP-001)

### Almgren-Chriss Model
Expected slippage = spread_cost + market_impact
spread_cost   = 0.5 × bid_ask_spread × order_size
market_impact = η × σ × sqrt(order_size / ADV)

where:
  η   = impact coefficient (≈ 0.1 for liquid markets)
  σ   = daily volatility
  ADV = average daily volume (20-day)

### BTC/USDT on Binance (typical values)
Spread: 0.5-2 bps (tight, liquid)
Market impact: 1-5 bps for orders < 0.1% of ADV
ADV (BTC/USDT): ~$2B/day → orders < $2M negligible impact

### Implementation for this project
class SlippageModel:
    def estimate(self, qty_usd, adv_20d, volatility, spread_bps):
        impact = 0.1 * volatility * sqrt(qty_usd / adv_20d)
        total  = spread_bps/10000 + impact
        return total  # as fraction of trade value

    def veto(self, expected_return, slippage, threshold=0.5):
        # Veto if slippage > threshold × expected_return
        return slippage > threshold * abs(expected_return)

### Wire into gates.py as Gate 0 (before all other gates)
- Fetch ADV from fetcher.py (already has order book data)
- Estimate per-signal before position sizing
- Reduce Kelly fraction by (1 - slippage_fraction)
