"""
Tests for the whale gate's advisory/veto posture and GateResult.size_scalar.

The defect these pin: check_whale_activity is documented — in its own
docstring, in the GAP-015 section header, and in RiskGateContext.whale_scalar
— as reducing position size by 50% rather than blocking. It returned
GateResult.fail(), evaluate_all_gates() returns on the first failure, and
whale_scalar was never written, read or emitted by any line in the
repository. The documented reduction had no implementation at all, and the
gate vetoed instead.
"""

from __future__ import annotations

import pytest

from src.risk.gates import (
    GateResult,
    GateStatus,
    check_whale_activity,
)

# ------------------------------------------------------------ GateResult


def test_pass_gate_carries_no_reduction() -> None:
    assert GateResult.pass_gate().size_scalar == 1.0


def test_fail_carries_no_reduction() -> None:
    # A veto is not a size reduction; conflating them is the original bug.
    result = GateResult.fail(GateStatus.REDUCE_WHALE_ACTIVITY, reason="x")
    assert result.passed is False
    assert result.size_scalar == 1.0


def test_reduce_passes_but_shrinks() -> None:
    result = GateResult.reduce(GateStatus.REDUCE_WHALE_ACTIVITY, 0.5, reason="x")
    # passed is True: an advisory gate is not a veto. Reporting it as a
    # failure is exactly the confusion this constructor exists to end.
    assert result.passed is True
    assert result.size_scalar == pytest.approx(0.5)
    assert result.status is GateStatus.REDUCE_WHALE_ACTIVITY
    assert result.details["size_scalar"] == pytest.approx(0.5)


@pytest.mark.parametrize("scalar", [0.0, -0.1, 1.5])
def test_reduce_rejects_a_scalar_outside_the_unit_interval(scalar: float) -> None:
    # A scalar > 1.0 would grow the position; 0.0 would be a veto wearing a
    # scalar's clothes. Both are caller bugs, not values to clamp.
    with pytest.raises(ValueError):
        GateResult.reduce(GateStatus.REDUCE_WHALE_ACTIVITY, scalar, reason="x")


# ------------------------------------------------------------ whale gate


def test_no_data_fails_open() -> None:
    result = check_whale_activity(None)
    assert result.passed is True
    assert result.size_scalar == 1.0


def test_healthy_ratio_passes_without_reduction() -> None:
    result = check_whale_activity(1.2)
    assert result.passed is True
    assert result.size_scalar == 1.0


def test_sell_pressure_blocks_by_default() -> None:
    # Default preserves the behaviour this gate has actually had for its
    # whole life. Switching a live risk control from block to half-size is a
    # trading-policy decision, not something to slip inside a bug fix.
    result = check_whale_activity(0.5)
    assert result.passed is False
    assert result.status is GateStatus.REDUCE_WHALE_ACTIVITY
    assert result.details["whale_action"] == "block"


def test_sell_pressure_reduces_when_advisory_is_enabled() -> None:
    result = check_whale_activity(0.5, advisory=True, advisory_scalar=0.5)
    assert result.passed is True
    assert result.size_scalar == pytest.approx(0.5)
    assert result.details["whale_action"] == "reduce_to_50%"


def test_advisory_scalar_is_configurable() -> None:
    result = check_whale_activity(0.5, advisory=True, advisory_scalar=0.25)
    assert result.size_scalar == pytest.approx(0.25)


def test_non_finite_ratio_is_treated_as_a_trigger_not_as_neutral() -> None:
    # Corrupt taker flow must not read as "no whale pressure".
    blocked = check_whale_activity(float("nan"))
    assert blocked.passed is False

    reduced = check_whale_activity(float("nan"), advisory=True)
    assert reduced.passed is True
    assert reduced.size_scalar < 1.0


def test_threshold_boundary_does_not_trigger() -> None:
    assert check_whale_activity(0.85, sell_threshold=0.85).passed is True
