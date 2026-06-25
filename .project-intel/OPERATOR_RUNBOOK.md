# Operator Runbook: Order FSM + Drift Monitoring

## Quick Start

### Check Live System Status

```bash
# Health check
curl http://localhost:8000/health

# Performance drift (most important)
curl http://localhost:8000/performance-drift

# Recent order status
curl http://localhost:8000/orders/{ORDER_ID}/status
```

---

## Monitoring Drift

### Dashboard Metrics

Check `/performance-drift` every 5-15 minutes during live trading:

```json
GET /performance-drift
{
    "drifted": false,
    "metrics": {
        "total_live_trades": 152,
        "rolling_sharpe": 1.42,
        "rolling_winrate": 0.553,
        "rolling_accuracy": 0.589,
        "max_live_drawdown_pct": 0.082
    }
}
```

### Green Zone (Normal Operation)
- `rolling_sharpe` > baseline - 0.5pp (e.g., if baseline 1.5 → >1.0 OK)
- `rolling_accuracy` > baseline - 10pp (e.g., if baseline 58% → >48% OK)
- `rolling_winrate` > baseline - 15pp (e.g., if baseline 55% → >40% OK)
- `max_live_drawdown_pct` < baseline + 10pp (e.g., if baseline 10% → <20% OK)

### Yellow Zone (Caution)
- Any metric within 50% of threshold
- E.g., Sharpe = 1.1 when threshold is 1.0

**Action**: Monitor closely, check for market regime change

### Red Zone (Halt)
- Drift detected: `"drifted": true`
- Any metric crosses threshold
- System automatically blocks NEW POSITIONS

**Action**: 
1. Do not submit new trades
2. Check `/performance-drift` for metric that triggered halt
3. Run diagnostics (see below)
4. Contact model developer

---

## Order FSM Troubleshooting

### Order Status Inquiry

If order confirmation is slow or delayed:

```bash
# Get order FSM state
curl http://localhost:8000/orders/ORDER_ID/status

{
    "order_id": "12345",
    "status": "pending",  # or filling, filled, cancelled, timeout, failed
    "filled_qty": 0.5,
    "average_fill_price": 65000.0,
    "first_confirmed_at_ms": 1719234567000,
    "retry_count": 3,
    "last_error": "",
    "created_at_ms": 1719234560000,
    "last_updated_ms": 1719234570000,
    "filled_at_prices": [[65000, 0.5]],
    "exchange_response": { ... }
}
```

### Interpretation

| Status | Meaning | Action |
|--------|---------|--------|
| **pending** | Placed, awaiting exchange confirmation | Wait (auto-polling) |
| **filling** | Exchange confirmed, partial or full fill in progress | Wait or cancel |
| **filled** | Complete, order closed ✓ | No action needed |
| **cancelled** | User/system cancelled | Check logs for reason |
| **timeout** | Exceeded 30s confirmation timeout | **Manual reconciliation required** |
| **failed** | Permanent error (bad symbol, insufficient funds, auth) | **Cannot recover — check error** |

### Timeout Recovery

If order shows `status: timeout`:

1. **Check exchange manually** (OKX or Binance testnet)
   - Verify order ID `12345` exists
   - Check if filled (partial or full)

2. **If filled on exchange**:
   - Record actual fill price in database
   - Mark order as reconciled
   - Update position tracking

3. **If NOT on exchange**:
   - Order was rejected before transmission
   - Safe to resubmit (no risk of double-fill)

4. **If PENDING on exchange** (unusual):
   - System can retry confirmation (`retry_count` shows attempts)
   - Wait 5-10 minutes for exchange to process
   - Escalate to Binance support if > 10 min

### Partial Fill Monitoring

OrderFSM aggregates partial fills automatically:

```json
{
    "filled_qty": 1.5,
    "average_fill_price": 65050.0,
    "filled_at_prices": [
        [65000, 0.75],   # First fill
        [65100, 0.75]    # Second fill
    ]
}
```

VWAP = (0.75 × 65000 + 0.75 × 65100) / 1.5 = **65050.0** ✓

---

## Drift Detection Scenarios

### Scenario 1: Model Accuracy Degradation

**Observation**:
```json
{
    "drifted": true,
    "metric": "accuracy",
    "live_value": 0.48,
    "baseline_value": 0.58,
    "drift_pp": 0.10,
    "reason": "Accuracy drifted 10.0pp below baseline (48% vs 58%)"
}
```

**Root Causes**:
1. Market regime change (model trained on 2023 data, now 2026)
2. Feature distribution shift (check feature drift monitor)
3. Model overfitting in training
4. Data quality issue (bad prices, exchange glitch)

**Diagnosis**:
```bash
# 1. Check feature drift
curl http://localhost:8000/debug/drift

# 2. Check regime detector
curl http://localhost:8000/regime

# 3. Review recent trades (last 30)
curl http://localhost:8000/trades?limit=30

# 4. Check data quality
curl http://localhost:8000/debug/data-quality
```

**Recovery**:
1. Halt trading (system auto-halts new positions)
2. Retrain models with recent data (last 3 months)
3. Validate OOS Sharpe > 1.5 before resuming
4. Resume with small position size to monitor

### Scenario 2: Drawdown Expansion (Risk Gate)

**Observation**:
```json
{
    "drifted": true,
    "metric": "drawdown",
    "live_value": 0.22,
    "baseline_value": 0.10,
    "drift_pp": 0.12,
    "reason": "Max drawdown expanded 12.0pp beyond baseline (22% vs 10%)"
}
```

**Root Causes**:
1. Market volatility spike (VIX > 25)
2. Slippage worse than expected (illiquid hours)
3. Position sizing too aggressive
4. Consecutive losses (normal variance, but worth investigating)

**Diagnosis**:
```bash
# 1. Check current drawdown
curl http://localhost:8000/equity-curve

# 2. Check recent losing trades
curl http://localhost:8000/trades?limit=30&filter=loss

# 3. Check slippage model
curl http://localhost:8000/debug/slippage

# 4. Check position size
curl http://localhost:8000/risk/kelly
```

**Recovery**:
1. System halts new positions (auto)
2. Close underwater positions gradually (hold winners)
3. Reduce Kelly sizing by 50% temporarily
4. Monitor for recovery over next 50 trades
5. Resume if drawdown contracts back within threshold

### Scenario 3: Sharpe Degradation (Most Common)

**Observation**:
```json
{
    "drifted": true,
    "metric": "sharpe",
    "live_value": 0.69,
    "baseline_value": 1.50,
    "drift_pp": 0.81,
    "reason": "Sharpe drifted 0.81pp below baseline (0.69 vs 1.50)"
}
```

**Root Causes**:
1. Signal quality decline (most likely)
2. Regime shift (sideways vs trending market)
3. Increased noise (bad data, market microstructure)
4. Variance in small sample (need 50+ trades)

**Diagnosis**:
```bash
# 1. Check signal strength (should be declining if Sharpe degraded)
curl http://localhost:8000/signals?limit=30

# 2. Check win rate
curl http://localhost:8000/trades?limit=30&metric=winrate

# 3. Check position duration (too fast or too slow?)
curl http://localhost:8000/trades?limit=30&metric=duration

# 4. Check regime
curl http://localhost:8000/regime
```

**Recovery**:
1. System halts new positions (auto)
2. Check if market is trending or sideways
   - If trending: signal should work, may be temporary noise
   - If sideways: signal is fundamentally broken in this regime
3. If temporary: wait 20 more trades, recheck drift
4. If regime issue: retrain with multi-regime data
5. Resume when Sharpe recovers or after retrain + validation

---

## Performance Drift Baseline Tuning

### Setting Initial Baseline

Baseline is set during model training:

```python
# In trainer.py after backtest:
baseline = PerformanceBaseline(
    train_sharpe=backtest.sharpe,           # In-sample
    oos_sharpe=walk_forward.sharpe,         # Out-of-sample (reliable)
    train_accuracy=backtest.accuracy,
    oos_accuracy=walk_forward.accuracy,
    train_win_rate=backtest.win_rate,
    max_drawdown_pct=backtest.max_dd,
    trades_in_backtest=len(backtest.trades),
)
```

**Rule**: Use OOS metrics, not in-sample (prevents overfitting alert)

### Adjusting Thresholds

Current thresholds (in `src/risk/performance_drift.py`):

```python
_DRIFT_SHARPE_DROP_PP = 0.5          # Halt if drop > 0.5pp
_DRIFT_ACCURACY_DROP_PP = 0.10       # Halt if drop > 10pp
_DRIFT_WINRATE_DROP_PP = 0.15        # Halt if drop > 15pp
_DRIFT_DRAWDOWN_EXPAND_PP = 0.10     # Halt if expands > 10pp
```

**Tuning Guidance**:
- **Tight thresholds** (0.3pp Sharpe): Catch degradation early but more false halts
- **Loose thresholds** (0.8pp Sharpe): Fewer halts but slow to react
- **Default (0.5pp)**: Balanced for typical signals

**Adjustment Process**:
1. Run 100+ live trades with current thresholds
2. Count false halts (good signal but halted) vs true positives (bad signal detected)
3. If false halts > 30%: loosen thresholds by 0.2pp
4. If true positives < 1: tighten thresholds by 0.1pp
5. Retest and iterate

---

## Daily Operations Checklist

### Pre-Market (Before 8am ET)

- [ ] Check drift: `curl .../performance-drift` — expect `"drifted": false`
- [ ] Review overnight trades (if running 24/7)
- [ ] Check data freshness: Last price update < 5min ago
- [ ] Check logs for errors: `tail -100 logs/trading.log`

### During Market Hours (8am - 4pm ET)

Every 15 minutes:
- [ ] Performance drift status
- [ ] Any new orders in "timeout" or "failed" state?
- [ ] Equity curve — any unexpected draws?
- [ ] Check for stuck positions (held > expected duration)

### Post-Market (After 4pm ET)

- [ ] Final equity snapshot
- [ ] Count today's trades and wins
- [ ] Review any halts (if drifted)
- [ ] Backup trade database

### Weekly (Friday evening)

- [ ] Recalculate rolling Sharpe over last 50 trades
- [ ] Review performance drift metrics trend
- [ ] Check if retraining is needed
- [ ] Update baseline if trained new model

---

## Emergency Procedures

### System Hangs (No orders executing)

```bash
# 1. Check orchestrator status
curl http://localhost:8000/health

# 2. Check logs for last error
tail -50 logs/trading.log | grep ERROR

# 3. Restart orchestrator
docker restart trade-bot-orchestrator

# 4. Resume trading (should pick up from last state)
```

### Market Crash (Large DD spike)

```bash
# 1. Check current drawdown
curl http://localhost:8000/equity-curve | tail -1

# 2. System auto-halts if DD > baseline + 10pp
# 3. Close large underwater positions manually
curl -X POST http://localhost:8000/orders/close-all \
  -d '{"filter": "underwater", "max_loss": 500}'

# 4. Wait for stabilization (1-2 hours)
# 5. Check drift — likely expanded
# 6. Do NOT resume until DD contracts
```

### Runaway Position (Position not closing)

```bash
# 1. Check order status
curl http://localhost:8000/orders/{ORDER_ID}/status

# 2. If status = "pending" or "filling"
#    → Order may be stuck on exchange
#    → Try to manually cancel on exchange

# 3. If status = "timeout" or "failed"
#    → Reconcile manually, update position DB

# 4. If order is "filled" but position not closed
#    → Check position tracking module
#    → May be database sync issue
#    → Manually mark position closed in DB
```

---

## Contacts & Escalation

**Model Issues** (Sharpe/Accuracy Drift):
- Email: model-dev@company.com
- Action: Request retraining with latest data
- Timeline: 2-3 days

**Exchange Issues** (Order Timeouts):
- Binance Support: support.binance.com
- Provide: Order ID, timestamp, symbol
- Timeline: 1-24 hours

**System/Infrastructure**:
- DevOps: devops@company.com
- Action: Check logs, restart services
- Timeline: < 30 min

---

## Reference: Drift Detector Internals

### Rolling Window

Drift detector maintains `deque(maxlen=50)` of:
- P&L history (for Sharpe)
- Win/loss flags (for win rate)
- Predictions vs actuals (for accuracy)

New trades slide into window, old trades drop off.

### Sharpe Calculation

```
Rolling Sharpe = mean(P&L) / stdev(P&L)

Example: Last 50 trades with varying P&L
  mean = 80 USD
  stdev = 120 USD
  Sharpe = 80 / 120 = 0.67
```

**Note**: Low Sharpe can mean:
- High volatility (risky trades)
- Mixed win/loss ratio (unreliable)
- Both (degraded signal)

### Accuracy Calculation

```
Rolling Accuracy = (# correct predictions) / (# total trades)

Correct = (model predicted long AND price went up)
       OR (model predicted short AND price went down)
```

### Win Rate Calculation

```
Rolling Win Rate = (# profitable trades) / (# total trades)

Profitable = Trade P&L > 0
```

**Note**: Win rate ≠ Accuracy
- Win rate depends on trade sizing + exit logic
- Accuracy depends on direction prediction only

---

## Appendix: Common Drift Patterns

### Pattern 1: Seasonal (Jan/Jul)
Sharpe drops in January (holiday positioning) and July (summer doldrums).
- Expected: 1-3 month recovery
- Action: Monitor but don't retrain aggressively
- Note: Retrain in late Aug for Sep-Dec robustness

### Pattern 2: Volatility Spike (VIX > 25)
All metrics degrade temporarily during panic.
- Expected: 2-5 day recovery
- Action: Reduce position size, wait for VIX to normalize
- Note: May indicate regime change — check regime detector

### Pattern 3: Slow Grind Down
Drift slowly accumulates (not sudden halt).
- Expected: Indicates model creep
- Action: Retrain in next 1-2 weeks
- Note: Often caused by distribution shift

### Pattern 4: False Positive (Recovers Fast)
Drift triggered but reverses within 10 trades.
- Expected: Random variance in small sample
- Action: Continue monitoring
- Note: If happens > 30% of time, loosen thresholds
