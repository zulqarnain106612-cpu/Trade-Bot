"""Regression tests for the single risk-gated horizon order path.

Decides:
  - REG-0030 — horizon decisions cannot bypass the single risk-gated order path
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import structlog

from src.config import Timeframe
from src.engine.orchestrator import Orchestrator
from src.engine.signal_engine import SignalResult


def _orchestrator(action: str | None, *, observed_at: float | None = None) -> Orchestrator:
    orchestrator = Orchestrator.__new__(Orchestrator)
    orchestrator._primary_tf = Timeframe.INTRADAY
    orchestrator._intelligence_task = None
    orchestrator._intelligence_adapter = SimpleNamespace(
        _intel=SimpleNamespace(
            _cfg={"inference_timeout_seconds": 1.0},
            last_decision=(
                {"action": action, "abstain_reason": "no validated horizon expert"}
                if action is not None
                else None
            ),
            last_horizon_ts=time.time() if observed_at is None else observed_at,
        )
    )
    orchestrator._log = structlog.get_logger("test.horizon_order_gate")
    return orchestrator


def _result(direction: int = 1) -> SignalResult:
    return SignalResult(
        tradeable=True,
        direction=direction,
        p_long=0.8,
        p_bet=0.8,
        kelly_result=SimpleNamespace(adjusted_fraction=0.01),
        regime=None,
        gate_result=None,
        skip_reason="",
    )


def test_horizon_abstention_vetoes_an_existing_tradeable_signal(monkeypatch) -> None:
    monkeypatch.setenv("INTEL_ENABLED", "true")
    result = asyncio.run(
        _orchestrator("ABSTAIN")._apply_intelligence_order_gate(_result(), Timeframe.INTRADAY)
    )
    assert not result.tradeable
    assert result.kelly_result is None
    assert "no validated horizon expert" in result.skip_reason


def test_only_a_fresh_direction_aligned_decision_can_clear_the_extra_veto(monkeypatch) -> None:
    monkeypatch.setenv("INTEL_ENABLED", "true")
    orchestrator = _orchestrator("BUY")
    assert asyncio.run(
        orchestrator._apply_intelligence_order_gate(_result(1), Timeframe.INTRADAY)
    ).tradeable
    mismatch = asyncio.run(
        orchestrator._apply_intelligence_order_gate(_result(0), Timeframe.INTRADAY)
    )
    assert not mismatch.tradeable
    assert mismatch.skip_reason == "horizon_horizon_direction_disagrees_with_signal_engine"


def test_stale_and_non_primary_horizon_decisions_cannot_authorize_orders(monkeypatch) -> None:
    monkeypatch.setenv("INTEL_ENABLED", "true")
    stale = asyncio.run(
        _orchestrator("BUY", observed_at=time.time() - 5)._apply_intelligence_order_gate(
            _result(), Timeframe.INTRADAY
        )
    )
    secondary = asyncio.run(
        _orchestrator("BUY")._apply_intelligence_order_gate(_result(), Timeframe.SWING)
    )
    assert not stale.tradeable
    assert stale.skip_reason == "horizon_horizon_decision_stale"
    assert not secondary.tradeable
    assert secondary.skip_reason == "horizon_primary_timeframe_only"
