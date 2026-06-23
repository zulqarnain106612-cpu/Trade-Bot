# SRE & Observability Patterns
**Domain**: devops | **Tags**: sre, observability, metrics, prometheus, grafana, alert, monitor, health, golden, signal

## SRE & Observability — Architecture Reference

### Google's 4 Golden Signals (apply to Trade Bot)
1. Latency:   signal_engine cycle time (target: < 500ms per 15m bar)
2. Traffic:   trades per hour, signals per hour
3. Errors:    gate rejections, API errors, HMM fit failures
4. Saturation: memory usage, SQLite write queue depth

### Current implementation (src/diagnostics/)
RuntimeMonitor: covers saturation (memory) + errors (dead tasks, tick stall)
TradeAuditor: covers traffic (trade decisions logged)
SignalDebugger: covers errors (model drift)
Missing: latency tracking, Prometheus export

### Recommended metrics to add (TASK-007)
# In orchestrator.py signal loop:
signal_latency_ms = (time.time() - bar_receive_time) * 1000
regime_state_gauge = current_regime  # 0/1/2
kelly_fraction_gauge = computed_kelly
gate_pass_rate = gates_passed / gates_evaluated (rolling 100)

### Alert thresholds
signal_latency > 2000ms: WARN (missing bar window)
memory > 512MB: WARN (RuntimeMonitor already handles)
gate_pass_rate < 0.05: WARN (model degraded or over-filtered)
gate_pass_rate > 0.8: WARN (gates too loose — risk of overtrading)
