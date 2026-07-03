"""Tests for src/api/metrics.py — TASK-007 Prometheus endpoint."""
from __future__ import annotations

import pytest
from src.api.metrics import (
    update_metrics,
    metrics_output,
    signal_score,
    regime_state,
    kelly_fraction,
    equity_usd,
    open_positions,
    model_accuracy_rolling,
)


def _parse_metrics(text: str) -> dict[str, float]:
    """Parse Prometheus text format into {metric_name_with_labels: value}."""
    result = {}
    for line in text.splitlines():
        if line.startswith("#") or not line.strip():
            continue
        parts = line.rsplit(" ", 1)
        if len(parts) == 2:
            result[parts[0]] = float(parts[1])
    return result


class TestUpdateMetrics:
    def test_signal_score_updates(self):
        update_metrics({"signal_score": 0.75})
        assert signal_score._value.get() == pytest.approx(0.75)

    def test_regime_state_updates(self):
        update_metrics({"regime_state": 2})
        assert regime_state._value.get() == pytest.approx(2.0)

    def test_kelly_fraction_updates(self):
        update_metrics({"kelly_fraction": 0.12})
        assert kelly_fraction._value.get() == pytest.approx(0.12)

    def test_equity_updates(self):
        update_metrics({"equity_usd": 12345.67})
        assert equity_usd._value.get() == pytest.approx(12345.67)

    def test_open_positions_updates(self):
        update_metrics({"open_positions": 3})
        assert open_positions._value.get() == pytest.approx(3.0)

    def test_model_accuracy_updates(self):
        update_metrics({"model_accuracy": 0.63})
        assert model_accuracy_rolling._value.get() == pytest.approx(0.63)

    def test_partial_snapshot_no_error(self):
        """Missing keys must be silently skipped."""
        update_metrics({"signal_score": 0.1})  # only one key — should not raise

    def test_empty_snapshot_no_error(self):
        update_metrics({})

    def test_bad_value_no_propagation(self):
        """Non-numeric value must not raise — swallowed, metric unchanged."""
        update_metrics({"signal_score": "bad"})  # type: ignore[arg-type]

    def test_regime_probs_labels(self):
        update_metrics({
            "prob_ranging": 0.6,
            "prob_trending": 0.3,
            "prob_volatile": 0.1,
        })
        text, _ = metrics_output()
        parsed = _parse_metrics(text)
        assert parsed.get('tradebot_regime_prob{state="ranging"}') == pytest.approx(0.6)
        assert parsed.get('tradebot_regime_prob{state="trending"}') == pytest.approx(0.3)
        assert parsed.get('tradebot_regime_prob{state="volatile"}') == pytest.approx(0.1)

    def test_gate_counters_increment(self):
        from src.api.metrics import gate_pass_total, gate_block_total
        before_pass = gate_pass_total.labels(gate_name="drawdown")._value.get()
        before_block = gate_block_total.labels(gate_name="regime")._value.get()
        update_metrics({
            "gate_results": {"drawdown": True, "regime": False}
        })
        assert gate_pass_total.labels(gate_name="drawdown")._value.get() == before_pass + 1
        assert gate_block_total.labels(gate_name="regime")._value.get() == before_block + 1

    def test_tick_duration_observed(self):
        from src.api.metrics import tick_duration_seconds
        before = tick_duration_seconds._sum.get()
        update_metrics({"tick_duration_seconds": 0.42})
        assert tick_duration_seconds._sum.get() == pytest.approx(before + 0.42)


class TestMetricsOutput:
    def test_returns_bytes_and_content_type(self):
        body, ct = metrics_output()
        assert isinstance(body, bytes)
        assert "text/plain" in ct

    def test_output_contains_tradebot_metrics(self):
        update_metrics({"signal_score": 0.5, "regime_state": 1})
        body, _ = metrics_output()
        text = body.decode()
        assert "tradebot_signal_score" in text
        assert "tradebot_regime_state" in text

    def test_output_is_valid_prometheus_format(self):
        """Every non-comment line must be parseable as 'name value'."""
        body, _ = metrics_output()
        for line in body.decode().splitlines():
            if line.startswith("#") or not line.strip():
                continue
            parts = line.rsplit(" ", 1)
            assert len(parts) == 2, f"Unparseable line: {line!r}"
            float(parts[1])  # must be numeric
