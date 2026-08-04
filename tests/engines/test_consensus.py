"""Tests for consensus layer — regime weights, circuit breaker, TTL."""

from datetime import UTC, datetime

from src.engines.consensus import (
    REGIME_WEIGHTS,
    CircuitBreaker,
    ConsensusLayer,
    TtlManager,
    bootstrap_ci,
    chauvenet_outliers,
    compute_agreement_score,
    compute_consensus_price,
)
from src.engines.schema import EngineOutput


def _make_output(
    engine_id: str = "E-01",
    price: float = 50_000.0,
    conf: float = 0.8,
    direction: int = 1,
) -> EngineOutput:
    return EngineOutput(
        engine_id=engine_id,
        symbol="BTC/USDT",
        timestamp_utc=datetime.now(UTC),
        predicted_price=price,
        confidence=conf,
        direction=direction,
        horizon_hours=4,
    )


# -----------------------------------------------------------------------
# Gap G-11: each regime weight row must sum to 1.0 ±0.001
# -----------------------------------------------------------------------


def test_regime_weights_row_sums():
    for regime, weights in REGIME_WEIGHTS.items():
        total = sum(weights)
        assert abs(total - 1.0) < 1e-3, f"{regime} weights sum to {total:.6f}"


def test_regime_weights_all_nonnegative():
    for regime, weights in REGIME_WEIGHTS.items():
        assert all(w >= 0 for w in weights), f"{regime} has negative weight"


def test_regime_weights_length():
    for regime, weights in REGIME_WEIGHTS.items():
        assert len(weights) == 18, f"{regime} has {len(weights)} weights, expected 18"


# -----------------------------------------------------------------------
# Consensus price
# -----------------------------------------------------------------------


def test_consensus_price_basic():
    outputs = [_make_output(f"E-{i + 1:02d}", price=50_000.0) for i in range(9)]
    price, weights = compute_consensus_price(outputs, "Trending")
    assert abs(price - 50_000.0) < 1.0
    assert abs(weights.sum() - 1.0) < 1e-6


def test_spoof_penalty_reduces_e02_weight():
    # E-02 (index 1) weight should be reduced by penalty
    outputs = [_make_output(f"E-{i + 1:02d}") for i in range(2)]
    _, w_no_penalty = compute_consensus_price(outputs, "Trending", spoof_penalty=0.0)
    _, w_penalty = compute_consensus_price(outputs, "Trending", spoof_penalty=0.5)
    assert w_penalty[1] < w_no_penalty[1]


def test_consensus_price_sparse_engines_use_correct_weights():
    """When engines fail (sparse outputs), each engine must use its own regime weight.

    Bug: old code used weights[:len(outputs)], assigning E-01's weight to E-03
    when E-02 failed. Fixed: use engine_id to index into full_weights.
    """
    from src.engines.consensus import REGIME_WEIGHTS

    # Only E-01 (idx 0) and E-18 (idx 17) survive
    outputs_sparse = [_make_output("E-01", price=50_000.0), _make_output("E-18", price=50_000.0)]
    _, w_sparse = compute_consensus_price(outputs_sparse, "Trending")
    # Weights should reflect REGIME_WEIGHTS["Trending"][0] and [17], not [0] and [1]
    full = REGIME_WEIGHTS["Trending"]
    expected_ratio = full[0] / full[17]  # E-01 weight / E-18 weight
    assert abs(w_sparse.sum() - 1.0) < 1e-6
    # The ratio of normalized weights should reflect the underlying regime weights
    if w_sparse[1] > 0:
        actual_ratio = w_sparse[0] / w_sparse[1]
        assert abs(actual_ratio - expected_ratio) < 0.01


# -----------------------------------------------------------------------
# Bootstrap CI
# -----------------------------------------------------------------------


def test_bootstrap_ci_ordering():
    outputs = [_make_output(f"E-{i + 1:02d}", price=50_000.0 + i * 100) for i in range(5)]
    _, weights = compute_consensus_price(outputs, "Trending")
    lo, hi = bootstrap_ci(outputs, weights, n=200)
    assert lo < hi
    assert 49_000 < lo < 52_000
    assert 49_000 < hi < 52_000


# -----------------------------------------------------------------------
# Chauvenet outlier detection
# -----------------------------------------------------------------------


def test_chauvenet_flags_extreme_outlier():
    outputs = [_make_output(f"E-{i + 1:02d}", price=50_000.0) for i in range(8)]
    outputs.append(_make_output("E-09", price=200_000.0))  # extreme outlier
    outliers = chauvenet_outliers(outputs)
    assert "E-09" in outliers


def test_chauvenet_no_outliers_when_uniform():
    outputs = [_make_output(f"E-{i + 1:02d}", price=50_000.0) for i in range(9)]
    outliers = chauvenet_outliers(outputs)
    assert outliers == []


# -----------------------------------------------------------------------
# Agreement score
# -----------------------------------------------------------------------


def test_agreement_score_perfect():
    outputs = [_make_output(f"E-{i + 1:02d}", price=50_000.0) for i in range(5)]
    assert compute_agreement_score(outputs) == 1.0


def test_agreement_score_low_on_divergence():
    outputs = [
        _make_output("E-01", price=10_000.0),
        _make_output("E-02", price=100_000.0),
    ]
    score = compute_agreement_score(outputs)
    assert score < 0.5


def test_agreement_score_never_negative():
    """Extreme divergence must not produce negative agreement score."""
    outputs = [
        _make_output("E-01", price=1.0),
        _make_output("E-02", price=1_000_000.0),
    ]
    score = compute_agreement_score(outputs)
    assert score >= 0.0


# -----------------------------------------------------------------------
# Circuit breaker (Gap G-14)
# -----------------------------------------------------------------------


def test_circuit_breaker_triggers_on_manipulation():
    cb = CircuitBreaker()
    e16 = _make_output("E-16")
    e16.metadata["manipulation_flag"] = True
    assert cb.check(e16) is True


def test_circuit_breaker_no_trigger_without_flag():
    cb = CircuitBreaker()
    e16 = _make_output("E-16")
    e16.metadata["manipulation_flag"] = False
    assert cb.check(e16) is False


def test_circuit_breaker_triggers_direction_zero():
    layer = ConsensusLayer()
    # E-01 through E-15 only — E-16 is added separately below with manipulation flag
    outputs = [_make_output(f"E-{i + 1:02d}") for i in range(15)]
    # Build E-16 with manipulation flag
    e16 = EngineOutput(
        engine_id="E-16",
        symbol="BTC/USDT",
        timestamp_utc=datetime.now(UTC),
        predicted_price=50_000.0,
        confidence=0.9,
        direction=0,
        horizon_hours=1,
        metadata={"manipulation_flag": True},
    )
    outputs.append(e16)
    result = layer.compute(outputs, "Trending")
    assert result.circuit_breaker_triggered is True


# -----------------------------------------------------------------------
# TTL hysteresis (Gap G-04)
# -----------------------------------------------------------------------


def test_ttl_no_oscillation_in_dead_band():
    mgr = TtlManager()
    # Within the dead-band between 0.3 and 0.7
    ttls = [mgr.compute(0.35) for _ in range(10)]
    # Should not oscillate — all same
    assert len(set(ttls)) == 1


def test_ttl_transitions_to_high_above_threshold():
    mgr = TtlManager()
    ttl = mgr.compute(0.8)  # 0.8 > 0.3 + 0.05
    assert ttl == 1  # "high" entropy → short TTL


def test_ttl_stays_low_in_hysteresis():
    mgr = TtlManager()
    # Start in low, go to value just above low threshold but below high threshold
    mgr.compute(0.1)  # ensure low state
    ttl = mgr.compute(0.32)  # 0.32 < 0.3 + 0.05, stays low
    assert ttl == 24
