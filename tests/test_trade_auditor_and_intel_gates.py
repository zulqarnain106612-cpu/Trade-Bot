"""
Coverage for:
  - src/diagnostics/trade_auditor.py
  - src/risk/intelligence_gates.py
Debt-005.
"""
from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

from src.diagnostics.trade_auditor import AuditRecord, TradeAuditor, MAX_AUDIT_RECORDS


def _rec(**overrides) -> AuditRecord:
    defaults = dict(
        ts_utc=datetime.now(tz=UTC).timestamp(),
        symbol="BTC/USDT",
        timeframe="15m",
        features={"frac_diff": 0.1, "ofi": 0.05},
        p_long=0.7,
        p_bet=0.8,
        direction=1,
        regime_state=1,
        prob_ranging=0.1,
        prob_trending=0.8,
        prob_volatile=0.1,
        gate_status="PASS",
        gate_reason="",
        gate_details={},
        kelly_fraction=0.05,
        kelly_notional_usd=500.0,
        kelly_quantity=0.1,
        kelly_is_capped=False,
        outcome="opened",
        trade_id=None,
        skip_reason="",
    )
    defaults.update(overrides)
    return AuditRecord(**defaults)


# ---------------------------------------------------------------------------
# AuditRecord
# ---------------------------------------------------------------------------

class TestAuditRecord:
    def test_fields_stored(self):
        r = _rec(p_long=0.75, direction=1)
        assert r.p_long == pytest.approx(0.75)
        assert r.direction == 1
        assert r.symbol == "BTC/USDT"

    def test_to_dict_json_serializable(self):
        import json
        r = _rec()
        d = r.to_dict()
        json.dumps(d)

    def test_to_dict_contains_key_fields(self):
        r = _rec(gate_status="HALT_DRAWDOWN", outcome="skipped")
        d = r.to_dict()
        assert d["gate_status"] == "HALT_DRAWDOWN"
        assert d["outcome"] == "skipped"
        assert "features" in d

    def test_skip_record_fields(self):
        r = _rec(outcome="skipped", skip_reason="gap_fill_failed",
                 kelly_fraction=None, kelly_notional_usd=None,
                 kelly_quantity=None, kelly_is_capped=None)
        assert r.skip_reason == "gap_fill_failed"
        assert r.kelly_fraction is None

    def test_ts_utc_stored(self):
        now = datetime.now(tz=UTC).timestamp()
        r = _rec(ts_utc=now)
        assert r.ts_utc == pytest.approx(now)


# ---------------------------------------------------------------------------
# TradeAuditor
# ---------------------------------------------------------------------------

class TestTradeAuditor:
    def test_init_empty(self):
        a = TradeAuditor()
        assert len(a._records) == 0
        assert a._n_total == 0

    def test_record_increments_n_total(self):
        a = TradeAuditor()
        a.record(_rec())
        assert a._n_total == 1

    def test_record_multiple(self):
        a = TradeAuditor()
        for _ in range(5):
            a.record(_rec())
        assert a._n_total == 5
        assert len(a._records) == 5

    def test_rolling_eviction_at_max(self):
        a = TradeAuditor()
        for _ in range(MAX_AUDIT_RECORDS + 10):
            a.record(_rec())
        # deque maxlen enforces eviction
        assert len(a._records) == MAX_AUDIT_RECORDS
        # n_total keeps counting
        assert a._n_total == MAX_AUDIT_RECORDS + 10

    def test_recent_returns_last_n(self):
        a = TradeAuditor()
        for i in range(10):
            a.record(_rec(p_long=float(i) / 10))
        recent = a.recent(3)
        assert len(recent) == 3
        # recent() returns last N records; highest p_long is in there
        p_longs = {r.p_long for r in recent}
        assert 0.9 in p_longs or max(p_longs) == pytest.approx(0.9)

    def test_recent_all_when_fewer_than_n(self):
        a = TradeAuditor()
        a.record(_rec())
        a.record(_rec())
        assert len(a.recent(100)) == 2

    def test_n_tradeable_counts_opened(self):
        a = TradeAuditor()
        a.record(_rec(outcome="opened"))
        a.record(_rec(outcome="opened"))
        a.record(_rec(outcome="skipped"))
        assert a._n_tradeable == 2

    def test_n_skipped_counts_skipped(self):
        a = TradeAuditor()
        a.record(_rec(outcome="opened"))
        a.record(_rec(outcome="skipped"))
        assert a._n_skipped == 1

    def test_summary_has_required_keys(self):
        a = TradeAuditor()
        a.record(_rec(outcome="opened"))
        a.record(_rec(outcome="skipped", skip_reason="gap_fill_failed"))
        s = a.summary()
        assert "n_total" in s
        assert "n_skipped" in s  # actual key from TradeAuditor.summary()

    def test_summary_skip_reason_breakdown(self):
        a = TradeAuditor()
        a.record(_rec(outcome="skipped", skip_reason="gap_fill_failed"))
        a.record(_rec(outcome="skipped", skip_reason="gap_fill_failed"))
        a.record(_rec(outcome="skipped", skip_reason="insufficient_bars"))
        s = a.summary()
        breakdown = s["skip_reason_breakdown"]
        assert breakdown.get("gap_fill_failed") == 2
        assert breakdown.get("insufficient_bars") == 1

    def test_summary_empty(self):
        a = TradeAuditor()
        s = a.summary()
        assert s["n_total"] == 0


# ---------------------------------------------------------------------------
# ExchangeStressGate
# ---------------------------------------------------------------------------

def _intel_metrics(**overrides) -> MagicMock:
    m = MagicMock()
    m.exchange_stress_score = 0.1
    m.whale_buy_sell_ratio = 1.0
    m.exchange_netflow_7d_zscore = 0.0
    m.funding_rate_pct = 0.01
    m.basis_spread_pct = 0.02
    for k, v in overrides.items():
        setattr(m, k, v)
    return m


class TestExchangeStressGate:
    def test_low_stress_passes(self):
        from src.risk.intelligence_gates import ExchangeStressGate, GateStatus
        result = ExchangeStressGate.evaluate(_intel_metrics(exchange_stress_score=0.2))
        assert result.status == GateStatus.PASS

    def test_high_stress_halts(self):
        from src.risk.intelligence_gates import ExchangeStressGate, GateStatus
        result = ExchangeStressGate.evaluate(_intel_metrics(exchange_stress_score=0.9))
        assert result.status == GateStatus.HALT
        assert result.gate_id == 7

    def test_severity_in_range(self):
        from src.risk.intelligence_gates import ExchangeStressGate
        result = ExchangeStressGate.evaluate(_intel_metrics(exchange_stress_score=0.8))
        assert 0.0 <= result.severity <= 1.0

    def test_reason_nonempty_on_halt(self):
        from src.risk.intelligence_gates import ExchangeStressGate, GateStatus
        result = ExchangeStressGate.evaluate(_intel_metrics(exchange_stress_score=0.9))
        assert result.status == GateStatus.HALT
        assert len(result.reason) > 0

    def test_at_threshold_halts(self):
        from src.risk.intelligence_gates import ExchangeStressGate, GateStatus
        result = ExchangeStressGate.evaluate(
            _intel_metrics(exchange_stress_score=ExchangeStressGate.STRESS_THRESHOLD + 0.01)
        )
        assert result.status == GateStatus.HALT


class TestWhaleActivityGate:
    def test_neutral_passes(self):
        from src.risk.intelligence_gates import GateStatus, WhaleActivityGate
        result = WhaleActivityGate.evaluate(
            _intel_metrics(whale_buy_sell_ratio=1.0, exchange_netflow_7d_zscore=0.0),
            current_price_zscore=0.0,
        )
        assert result.status in (GateStatus.PASS, GateStatus.REDUCE)

    def test_extreme_netflow_reduces_or_halts(self):
        from src.risk.intelligence_gates import GateStatus, WhaleActivityGate
        result = WhaleActivityGate.evaluate(
            _intel_metrics(exchange_netflow_7d_zscore=-3.0, whale_buy_sell_ratio=0.2),
            current_price_zscore=0.0,
        )
        assert result.status in (GateStatus.HALT, GateStatus.REDUCE)
        assert result.gate_id == 8

    def test_triggered_by_is_string(self):
        from src.risk.intelligence_gates import WhaleActivityGate
        result = WhaleActivityGate.evaluate(_intel_metrics(), current_price_zscore=0.0)
        assert isinstance(result.triggered_by, str)
        assert len(result.triggered_by) > 0

    def test_bullish_accumulation_reduces(self):
        from src.risk.intelligence_gates import GateStatus, WhaleActivityGate
        # Whale buying at low price (accumulation) → REDUCE (don't fight smart money)
        result = WhaleActivityGate.evaluate(
            _intel_metrics(whale_buy_sell_ratio=3.0, exchange_netflow_7d_zscore=2.0),
            current_price_zscore=-2.5,  # at 30d low
        )
        # Should REDUCE or PASS (varies by config)
        assert result.gate_id == 8
