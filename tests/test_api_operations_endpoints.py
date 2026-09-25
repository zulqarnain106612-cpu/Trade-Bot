"""
Coverage for the operator-control endpoints added for the dashboard:
/models/retrain, /backfill and /capital-floor/re-authorize.

Tests the request models and the shared timeframe validator directly —
the pieces reachable without standing up the full app lifespan, matching
the approach in test_api_main_edges.py.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

from src.api.main import (
    BackfillRequest,
    CapitalFloorReAuthorizeRequest,
    RetrainRequest,
    _parse_timeframe_or_400,
)
from src.config import Timeframe

# ---------------------------------------------------------------------------
# Timeframe validation — shared by all three endpoints
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("tf", [tf.value for tf in Timeframe])
def test_parse_timeframe_accepts_every_configured_value(tf):
    assert _parse_timeframe_or_400(tf) == Timeframe(tf)


def test_parse_timeframe_rejects_unknown_with_400():
    with pytest.raises(HTTPException) as exc:
        _parse_timeframe_or_400("7h")
    assert exc.value.status_code == 400
    # The message must name the valid set — an operator hitting this by
    # hand has no other way to discover it.
    assert "7h" in str(exc.value.detail)


def test_parse_timeframe_rejects_empty_string():
    with pytest.raises(HTTPException) as exc:
        _parse_timeframe_or_400("")
    assert exc.value.status_code == 400


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------


def test_retrain_request_validates_operator():
    req = RetrainRequest(timeframe="15m", operator="alice", operator_secret="s")
    assert req.operator == "alice"
    assert req.timeframe == "15m"


def test_retrain_request_rejects_invalid_operator():
    with pytest.raises(ValueError):
        RetrainRequest(timeframe="15m", operator="bad operator!!", operator_secret="s")


def test_backfill_request_defaults_to_180_days():
    req = BackfillRequest(timeframe="15m")
    assert req.lookback_days == 180


@pytest.mark.parametrize("days", [0, -1, 1826])
def test_backfill_request_rejects_out_of_range_lookback(days):
    with pytest.raises(ValueError):
        BackfillRequest(timeframe="15m", lookback_days=days)


def test_backfill_request_takes_no_operator_secret():
    """
    /backfill is gated by API-key role, not the operator secret. If this
    model ever grows those fields the frontend must stop using postJson
    and go back through useOperatorAction — the two have to agree.
    """
    assert "operator_secret" not in BackfillRequest.model_fields


def test_capital_floor_reauth_requires_a_substantive_reason():
    # The reason is the audit-trail record of why trading resumed, so a
    # placeholder must not satisfy it.
    with pytest.raises(ValueError):
        CapitalFloorReAuthorizeRequest(
            timeframe="15m", reason="ok", operator="alice", operator_secret="s"
        )


def test_capital_floor_reauth_accepts_a_real_reason():
    req = CapitalFloorReAuthorizeRequest(
        timeframe="15m",
        reason="root cause understood, positions reconciled",
        operator="alice",
        operator_secret="s",
    )
    assert req.timeframe == "15m"


def test_capital_floor_reauth_rejects_invalid_operator():
    with pytest.raises(ValueError):
        CapitalFloorReAuthorizeRequest(
            timeframe="15m",
            reason="root cause understood, positions reconciled",
            operator="!!!",
            operator_secret="s",
        )


# ---------------------------------------------------------------------------
# Orchestrator control methods
# ---------------------------------------------------------------------------


def _orchestrator_stub():
    """A bare Orchestrator-shaped object for the pure-logic methods."""
    from src.engine.orchestrator import Orchestrator

    orch = object.__new__(Orchestrator)
    orch._engines = {}
    orch._retrain_tasks = {}
    orch._last_retrain_error = {}
    orch._timeframes = list(Timeframe)
    orch._primary_tf = Timeframe.INTRADAY
    orch._log = MagicMock()
    return orch


def test_retrain_status_reports_every_active_timeframe():
    orch = _orchestrator_stub()
    status = orch.retrain_status()
    assert set(status) == {tf.value for tf in Timeframe}
    assert all(s["running"] is False for s in status.values())
    assert status[Timeframe.INTRADAY.value]["is_primary"] is True


def test_retrain_status_surfaces_last_error():
    orch = _orchestrator_stub()
    orch._last_retrain_error["15m"] = "boom"
    assert orch.retrain_status()["15m"]["last_error"] == "boom"


def test_request_retrain_refuses_while_one_is_in_flight():
    """
    Two trainers writing the same artifact path would race, so a manual
    request during an in-flight retrain is refused rather than queued.
    """
    orch = _orchestrator_stub()
    running = MagicMock()
    running.done.return_value = False
    orch._retrain_tasks["15m"] = running

    assert orch.request_retrain(Timeframe.INTRADAY) == "already_running"


def test_capital_floor_status_skips_engines_without_a_floor():
    orch = _orchestrator_stub()
    engine_without = MagicMock(spec=[])  # no _capital_floor attribute
    orch._engines = {"15m": engine_without}
    assert orch.capital_floor_status() == {}


def test_capital_floor_status_reports_halt_state():
    orch = _orchestrator_stub()
    floor = MagicMock()
    floor.is_halted = True
    floor.halt_reason = "drawdown 0.35 >= floor 0.300"
    floor.last_reauthorization = None
    engine = MagicMock()
    engine._capital_floor = floor
    orch._engines = {"15m": engine}

    status = orch.capital_floor_status()
    assert status["15m"]["halted"] is True
    assert "drawdown" in status["15m"]["halt_reason"]
    assert status["15m"]["last_reauthorization"] is None


def test_re_authorize_returns_false_for_unknown_timeframe():
    """
    False, not an exception — the route turns it into a 404 so an
    operator is never told a halt was cleared that never existed.
    """
    orch = _orchestrator_stub()
    assert orch.re_authorize_capital_floor("15m", "alice", "reason here", 0) is False


def test_re_authorize_calls_through_to_the_floor():
    orch = _orchestrator_stub()
    floor = MagicMock()
    engine = MagicMock()
    engine._capital_floor = floor
    orch._engines = {"15m": engine}

    assert orch.re_authorize_capital_floor("15m", "alice", "reason here", 123) is True
    floor.re_authorize.assert_called_once_with(
        authorized_by="alice", reason="reason here", at_ms=123
    )
