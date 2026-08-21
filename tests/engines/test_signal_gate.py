"""Tests for consensus-to-signal gate (Gap G-09, G-10)."""

from src.engines.signal_gate import TradeSignal, consensus_to_signal


def _gate(**kwargs) -> TradeSignal:
    defaults = {
        "consensus": 50_500.0,
        "spot": 50_000.0,
        "uncertainty_label": "high_confidence",
        "agreement": 0.8,
        "tail_risk": 0.1,
        "e16_flag": False,
        "regime": "Trending",
        "ttl_hours": 4,
        "symbol": "BTC/USDT",
        "raw_confidence": 0.7,
    }
    defaults.update(kwargs)
    return consensus_to_signal(**defaults)


def test_long_on_upside_consensus():
    sig = _gate(consensus=50_300.0, spot=50_000.0)
    assert sig.direction == 1


def test_short_on_downside_consensus():
    sig = _gate(consensus=49_700.0, spot=50_000.0)
    assert sig.direction == -1


def test_neutral_on_small_move():
    sig = _gate(consensus=50_100.0, spot=50_000.0)
    assert sig.direction == 0


def test_suppress_on_high_uncertainty():
    sig = _gate(uncertainty_label="suppress", consensus=51_000.0, spot=50_000.0)
    assert sig.direction == 0
    assert "uncertainty_suppress" in sig.warnings
    assert sig.confidence == 0.0
    assert sig.kelly_multiplier == 0.0


def test_e16_flag_suppresses_direction():
    sig = _gate(e16_flag=True, consensus=51_000.0, spot=50_000.0)
    assert sig.direction == 0
    assert "manipulation_circuit_breaker" in sig.warnings


def test_tail_risk_warning():
    sig = _gate(tail_risk=0.5)
    assert "tail_risk_active" in sig.warnings


def test_kelly_multiplier_positive_when_clean():
    sig = _gate(agreement=0.9, tail_risk=0.05, uncertainty_label="high_confidence")
    assert sig.kelly_multiplier > 0


def test_kelly_multiplier_zero_on_suppress():
    sig = _gate(uncertainty_label="suppress")
    assert sig.kelly_multiplier == 0.0


def test_e16_flag_also_zeros_kelly_multiplier():
    """Manipulation circuit breaker must zero kelly_multiplier, not just direction."""
    sig = _gate(e16_flag=True, agreement=0.9, tail_risk=0.05, consensus=51_000.0, spot=50_000.0)
    assert sig.direction == 0
    assert sig.kelly_multiplier == 0.0
    assert "manipulation_circuit_breaker" in sig.warnings
