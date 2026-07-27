"""
Prometheus metrics for Trade Bot — TASK-007.

Exposes GET /metrics in Prometheus text format.
Metrics are updated by the orchestrator on each signal tick
via update_metrics() below.

Design:
  - Uses prometheus_client CollectorRegistry (not the default global
    registry) to avoid test pollution and allow multiple instances.
  - All metric updates go through update_metrics() — a single call
    site, easy to audit and test.
  - /metrics endpoint is auth-protected via the same API key gate
    as all other endpoints.
  - No push; Prometheus scrapes this endpoint on its configured interval.

Metrics exposed:
  tradebot_signal_score          Gauge   latest composite signal score [-1, 1]
  tradebot_regime_state          Gauge   0=ranging 1=trending 2=volatile
  tradebot_regime_prob{state}    Gauge   HMM posterior probabilities
  tradebot_kelly_fraction        Gauge   adjusted Kelly fraction [0, 0.25]
  tradebot_gate_pass_total       Counter gates passed (label: gate_name)
  tradebot_gate_block_total      Counter gates blocked (label: gate_name)
  tradebot_model_accuracy_rolling Gauge  rolling win-rate from last N trades
  tradebot_equity_usd            Gauge   current portfolio equity in USD
  tradebot_open_positions        Gauge   number of currently open positions
  tradebot_tick_duration_seconds Histogram orchestrator tick wall-clock time
"""

from __future__ import annotations

from typing import Any

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)


# ── Singleton registry (not the default global — avoids test cross-talk) ──────
_REGISTRY = CollectorRegistry(auto_describe=True)

# ── Gauges ────────────────────────────────────────────────────────────────────
signal_score = Gauge(
    "tradebot_signal_score",
    "Latest composite signal score in [-1, 1]",
    registry=_REGISTRY,
)

regime_state = Gauge(
    "tradebot_regime_state",
    "Current HMM regime: 0=ranging 1=trending 2=volatile",
    registry=_REGISTRY,
)

regime_prob = Gauge(
    "tradebot_regime_prob",
    "HMM posterior probability per regime state",
    labelnames=["state"],
    registry=_REGISTRY,
)

kelly_fraction = Gauge(
    "tradebot_kelly_fraction",
    "Adjusted Kelly fraction passed to position sizing [0, 0.25]",
    registry=_REGISTRY,
)

model_accuracy_rolling = Gauge(
    "tradebot_model_accuracy_rolling",
    "Rolling win-rate over the last N closed trades [0, 1]",
    registry=_REGISTRY,
)

equity_usd = Gauge(
    "tradebot_equity_usd",
    "Current portfolio equity in USD",
    registry=_REGISTRY,
)

open_positions = Gauge(
    "tradebot_open_positions",
    "Number of currently open positions",
    registry=_REGISTRY,
)

# ── Counters ──────────────────────────────────────────────────────────────────
gate_pass_total = Counter(
    "tradebot_gate_pass_total",
    "Number of times a risk gate was passed",
    labelnames=["gate_name"],
    registry=_REGISTRY,
)

gate_block_total = Counter(
    "tradebot_gate_block_total",
    "Number of times a risk gate blocked a trade",
    labelnames=["gate_name"],
    registry=_REGISTRY,
)

regime_ensemble_failure_total = Counter(
    "tradebot_regime_ensemble_failure_total",
    "Number of times the v4 regime ensemble (BOCPD + HMM combine) raised "
    "during a tick — instrumentation-only failures, never blocks trading, "
    "but a rising rate indicates the observability signal has gone dark",
    registry=_REGISTRY,
)

# ── Histogram ─────────────────────────────────────────────────────────────────
tick_duration_seconds = Histogram(
    "tradebot_tick_duration_seconds",
    "Wall-clock time for a full orchestrator signal tick",
    buckets=(0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0),
    registry=_REGISTRY,
)


# ── Public update API ─────────────────────────────────────────────────────────


def update_metrics(snapshot: dict[str, Any]) -> None:
    """
    Update all gauges/counters from an orchestrator tick snapshot.

    Called by orchestrator after each signal evaluation. Never raises —
    metric failures must not affect trade execution.

    Expected keys (all optional — missing keys are silently skipped):
        signal_score          float  [-1, 1]
        regime_state          int    0|1|2
        prob_ranging          float  [0, 1]
        prob_trending         float  [0, 1]
        prob_volatile         float  [0, 1]
        kelly_fraction        float  [0, 0.25]
        model_accuracy        float  [0, 1]
        equity_usd            float
        open_positions        int
        gate_results          dict[str, bool]  gate_name → passed
        tick_duration_seconds float
    """
    try:
        if (v := snapshot.get("signal_score")) is not None:
            signal_score.set(float(v))

        if (v := snapshot.get("regime_state")) is not None:
            regime_state.set(int(v))

        for label, key in (
            ("ranging", "prob_ranging"),
            ("trending", "prob_trending"),
            ("volatile", "prob_volatile"),
        ):
            if (v := snapshot.get(key)) is not None:
                regime_prob.labels(state=label).set(float(v))

        if (v := snapshot.get("kelly_fraction")) is not None:
            kelly_fraction.set(float(v))

        if (v := snapshot.get("model_accuracy")) is not None:
            model_accuracy_rolling.set(float(v))

        if (v := snapshot.get("equity_usd")) is not None:
            equity_usd.set(float(v))

        if (v := snapshot.get("open_positions")) is not None:
            open_positions.set(int(v))

        for gate_name, passed in (snapshot.get("gate_results") or {}).items():
            if passed:
                gate_pass_total.labels(gate_name=gate_name).inc()
            else:
                gate_block_total.labels(gate_name=gate_name).inc()

        if (v := snapshot.get("tick_duration_seconds")) is not None:
            tick_duration_seconds.observe(float(v))

    except Exception:
        # Metric update failure must never propagate to trade path
        import structlog

        structlog.get_logger(__name__).warning(
            "metrics.update_failed", snapshot_keys=list(snapshot.keys())
        )


def metrics_output() -> tuple[bytes, str]:
    """
    Generate Prometheus text output.

    Returns (body_bytes, content_type) ready for a FastAPI Response.
    """
    return generate_latest(_REGISTRY), CONTENT_TYPE_LATEST
