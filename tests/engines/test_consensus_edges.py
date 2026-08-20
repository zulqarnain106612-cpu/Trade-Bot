"""
Edge paths of the consensus layer.

`test_consensus.py` covers the happy path; these are the fallbacks that only
run when something is wrong — no manipulation signal at all, a breaker that has
cooled down, an unparseable engine id, engines that all abstain, and the audit
sink being unavailable while the breaker trips.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import patch

import numpy as np
import pytest

from src.engines.consensus import (
    CircuitBreaker,
    ConsensusLayer,
    compute_agreement_score,
    compute_consensus_price,
    kelly_weight,
)
from src.engines.schema import EngineOutput


def _out(
    engine_id: str = "E-01",
    price: float = 50_000.0,
    conf: float = 0.8,
    metadata: dict | None = None,
) -> EngineOutput:
    return EngineOutput(
        engine_id=engine_id,
        symbol="BTC/USDT",
        timestamp_utc=datetime.now(UTC),
        predicted_price=price,
        confidence=conf,
        direction=1,
        horizon_hours=4,
        metadata=metadata or {},
    )


# ---------------------------------------------------------------------------
# Circuit breaker
# ---------------------------------------------------------------------------


class TestCircuitBreaker:
    def test_absent_e16_output_leaves_the_breaker_open(self) -> None:
        assert CircuitBreaker().check(None) is False

    def test_a_clean_e16_output_does_not_trip_the_breaker(self) -> None:
        breaker = CircuitBreaker()
        assert breaker.check(_out("E-16", metadata={"manipulation_flag": False})) is False

    def test_the_breaker_resets_once_the_cooldown_elapses(self) -> None:
        breaker = CircuitBreaker()
        assert breaker.check(_out("E-16", metadata={"manipulation_flag": True})) is True

        # Still tripped inside the cooldown, released after it.
        clean = _out("E-16", metadata={"manipulation_flag": False})
        with patch("src.engines.consensus.time.monotonic", return_value=1e9):
            assert breaker.check(clean) is False
        assert breaker._tripped_at is None

    def test_an_unavailable_audit_sink_does_not_stop_the_breaker(self) -> None:
        breaker = CircuitBreaker()
        with patch(
            "src.diagnostics.audit_trail.get_audit_trail", side_effect=RuntimeError("no sink")
        ):
            assert breaker.check(_out("E-16", metadata={"manipulation_flag": True})) is True


# ---------------------------------------------------------------------------
# Weighting
# ---------------------------------------------------------------------------


class TestConsensusPrice:
    def test_an_unparseable_engine_id_falls_back_to_the_first_weight_slot(self) -> None:
        price, weights = compute_consensus_price([_out("bogus", price=50_000.0)], "Trending")
        assert price == pytest.approx(50_000.0)
        assert weights.sum() == pytest.approx(1.0)

    def test_engines_that_all_abstain_fall_back_to_equal_weights(self) -> None:
        outputs = [_out("E-01", 50_000.0, conf=0.0), _out("E-02", 60_000.0, conf=0.0)]
        price, weights = compute_consensus_price(outputs, "Trending")

        assert weights.tolist() == pytest.approx([0.5, 0.5])
        assert price == pytest.approx(55_000.0)

    def test_an_unknown_regime_uses_the_trending_weight_vector(self) -> None:
        outputs = [_out("E-01", 50_000.0), _out("E-02", 51_000.0)]
        known = compute_consensus_price(outputs, "Trending")[0]
        unknown = compute_consensus_price(outputs, "NoSuchRegime")[0]
        assert unknown == pytest.approx(known)

    def test_agreement_is_perfect_when_predictions_average_to_zero(self) -> None:
        assert compute_agreement_score([_out("E-01", 0.0), _out("E-02", 0.0)]) == 1.0

    def test_agreement_falls_as_predictions_diverge(self) -> None:
        tight = compute_agreement_score([_out("E-01", 50_000.0), _out("E-02", 50_100.0)])
        wide = compute_agreement_score([_out("E-01", 20_000.0), _out("E-02", 80_000.0)])
        assert tight > wide


# ---------------------------------------------------------------------------
# Kelly sizing
# ---------------------------------------------------------------------------


class TestKellyWeight:
    def test_an_unprofitable_edge_sizes_to_zero(self) -> None:
        assert kelly_weight(confidence=0.2, backtest_gain_ratio=1.0) == 0.0

    def test_the_fraction_is_capped_at_a_quarter_of_full_kelly(self) -> None:
        assert kelly_weight(confidence=1.0, backtest_gain_ratio=2.0) == pytest.approx(0.25)

    def test_confidence_outside_zero_to_one_is_clipped(self) -> None:
        assert kelly_weight(confidence=5.0, backtest_gain_ratio=2.0) == pytest.approx(0.25)
        assert kelly_weight(confidence=-5.0, backtest_gain_ratio=2.0) == 0.0

    def test_a_non_positive_gain_ratio_is_floored_rather_than_dividing_by_zero(self) -> None:
        assert np.isfinite(kelly_weight(confidence=0.9, backtest_gain_ratio=0.0))


# ---------------------------------------------------------------------------
# Layer
# ---------------------------------------------------------------------------


class TestConsensusLayer:
    def test_low_agreement_still_produces_a_result(self) -> None:
        layer = ConsensusLayer()
        outputs = [_out("E-01", 10_000.0), _out("E-02", 90_000.0)]

        result = layer.compute(outputs, "Trending", entropy_score=0.5)

        assert result.agreement_score < 0.5
        assert result.circuit_breaker_triggered is False
        assert result.metadata["n_engines"] == 2

    def test_a_tripped_breaker_pins_the_consensus_to_the_first_prediction(self) -> None:
        layer = ConsensusLayer()
        outputs = [
            _out("E-01", 50_000.0),
            _out("E-16", 50_000.0, metadata={"manipulation_flag": True}),
        ]

        result = layer.compute(outputs, "Trending")

        assert result.circuit_breaker_triggered is True
        assert result.spoof_penalty_applied is True
        assert result.consensus_price == pytest.approx(50_000.0)
        assert result.ci_low == result.ci_high == pytest.approx(50_000.0)
