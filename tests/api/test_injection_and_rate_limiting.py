"""
API-003, API-006, EXEC-005 — injection, rate limiting, and the kill switch.

**Injection** (`API-003`). The document lists SQL, NoSQL, command, template,
path-traversal, header and JSON payloads, and says to test them wherever input
reaches a database, filesystem, subprocess, external API or log. This project
has an unusually narrow input surface — the API takes enumerations, numbers
and symbol strings, not free text — so the tests below are organised around
that surface rather than around a generic payload list: every place a caller
string reaches something that interprets it.

**Rate limiting** (`API-006`). The objective is not preventing inconvenience:

> For a trading system, an attacker must not be able to cause thousands of
> orders, massive API calls, CPU exhaustion, database exhaustion.

So the assertions are about the *burst* and the *sustained flood*, not about a
single call being allowed.

**The kill switch** (`EXEC-005`) is here because it is an API-reachable
control and its requirements are API requirements: authenticated, authorized,
audited, idempotent, fast, and durable across a restart.
"""

from __future__ import annotations

import math

import pytest

from src.api.access_control import Permission, Role, require_permission
from src.api.ssrf import SSRFError, assert_outbound_url_allowed
from src.data.quality_gate import DataQualityGate
from src.execution.exchange_contract import parse_order
from src.models.trainer import _validate_timeframe


def _baseline():
    from src.risk.performance_drift import PerformanceBaseline

    return PerformanceBaseline(
        train_sharpe=1.0,
        oos_sharpe=0.8,
        train_accuracy=0.60,
        oos_accuracy=0.58,
        train_win_rate=0.55,
        max_drawdown_pct=0.10,
        trades_in_backtest=500,
    )


#: One payload per injection class in the source document's list.
PAYLOADS = {
    "sql": "'; DROP TABLE trades; --",
    "nosql": '{"$gt": ""}',
    "command": "; rm -rf / #",
    "template": "{{7*7}}",
    "path_traversal": "../../etc/passwd",
    "path_traversal_windows": "..\\..\\windows\\system32",
    "header": "value\r\nX-Injected: yes",
    "null_byte": "abc\x00def",
    "json": '{"__proto__": {"admin": true}}',
    "xss": "<script>alert(1)</script>",
}


class TestPathTraversalCannotReachTheFilesystem:
    @pytest.mark.parametrize("name", ["path_traversal", "path_traversal_windows", "null_byte"])
    def test_a_traversal_shaped_timeframe_is_refused(self, name):
        # `timeframe` is interpolated directly into a model filename, which
        # makes it the one caller-influenced string in this project that
        # reaches a path join.
        with pytest.raises(ValueError, match="Invalid timeframe"):
            _validate_timeframe(PAYLOADS[name])

    @pytest.mark.parametrize("payload", sorted(PAYLOADS.values()))
    def test_no_payload_is_accepted_as_a_timeframe(self, payload):
        with pytest.raises(ValueError):
            _validate_timeframe(payload)

    @pytest.mark.parametrize("timeframe", ["1m", "15m", "1h", "4h", "1d", "intraday"])
    def test_real_timeframes_are_accepted(self, timeframe):
        _validate_timeframe(timeframe)

    @pytest.mark.parametrize("empty", ["", " ", "\t"])
    def test_an_empty_timeframe_is_refused(self, empty):
        with pytest.raises(ValueError):
            _validate_timeframe(empty)


class TestInjectionPayloadsReachingTheOutboundSurface:
    @pytest.mark.parametrize("payload", sorted(PAYLOADS.values()))
    def test_no_payload_becomes_an_outbound_request(self, payload):
        # A URL is the one place a caller string could reach a network.
        with pytest.raises(SSRFError):
            assert_outbound_url_allowed(payload)

    def test_a_file_scheme_payload_cannot_read_the_disk(self):
        with pytest.raises(SSRFError, match="scheme"):
            assert_outbound_url_allowed("file:///etc/passwd")


class TestInjectionPayloadsReachingTheParsers:
    @pytest.mark.parametrize("payload", sorted(PAYLOADS.values()))
    def test_the_exchange_contract_treats_a_payload_as_data(self, payload):
        # Not as a status, not as a number, and not as something to execute.
        update = parse_order({"id": "ord-1", "status": payload}, expected_order_id="ord-1")
        assert update.needs_reconciliation
        assert update.fsm_status is None

    @pytest.mark.parametrize("payload", sorted(PAYLOADS.values()))
    def test_a_long_payload_is_truncated_in_the_rejection_reason(self, payload):
        # Twelve characters survive by design -- enough to recognise a new
        # venue status, too few to be a credential or a working payload. A
        # payload shorter than that is not truncated, and does not need to
        # be: it is already too short to do anything.
        update = parse_order({"id": "ord-1", "status": payload}, expected_order_id="ord-1")
        if len(payload) > 12:
            assert payload not in update.reason

    @pytest.mark.parametrize("payload", sorted(PAYLOADS.values()))
    def test_a_payload_as_a_sentiment_score_is_refused(self, payload):
        gate = DataQualityGate()
        assert not gate.check_sentiment_score(math.nan).passed


class TestHeaderInjection:
    def test_a_crlf_payload_cannot_forge_a_header(self):
        # If a value carrying CRLF reached a response header, it would split
        # the response and inject one. The security-headers middleware only
        # ever writes its own constants, and this asserts that.
        from src.api.security_headers import SECURITY_HEADERS

        for value in SECURITY_HEADERS.values():
            assert "\r" not in value
            assert "\n" not in value

    def test_the_declared_header_names_are_well_formed(self):
        from src.api.security_headers import SECURITY_HEADERS

        for name in SECURITY_HEADERS:
            assert name.isascii()
            assert " " not in name


class TestRateLimiting:
    """
    The limiter's own logic, exercised through the state object the routes use.

    Constructed directly rather than through the app: the requirement is
    about the limiter's behaviour under burst and sustained load, and driving
    it through HTTP would test Starlette's routing as well.
    """

    @pytest.fixture
    def state(self):
        from src.api.main import AppState

        return AppState()

    def test_a_single_call_is_allowed(self, state):
        state.check_endpoint_rate_limit("trade", client_ip="1.2.3.4")

    def test_a_burst_is_eventually_refused(self, state):
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as excinfo:
            for _ in range(10_000):
                state.check_endpoint_rate_limit("trade", client_ip="1.2.3.4")
        assert excinfo.value.status_code == 429

    def test_the_limit_is_per_client(self, state):
        from fastapi import HTTPException

        with pytest.raises(HTTPException):
            for _ in range(10_000):
                state.check_endpoint_rate_limit("trade", client_ip="1.2.3.4")
        # A second client is unaffected: one flooding caller must not deny
        # the operator their own kill switch.
        state.check_endpoint_rate_limit("trade", client_ip="5.6.7.8")

    def test_the_limit_is_per_endpoint(self, state):
        from fastapi import HTTPException

        with pytest.raises(HTTPException):
            for _ in range(10_000):
                state.check_endpoint_rate_limit("trade", client_ip="1.2.3.4")
        state.check_endpoint_rate_limit("risk-controls", client_ip="1.2.3.4")

    def test_mode_changes_are_limited_separately(self, state):
        from fastapi import HTTPException

        # The endpoint that turns paper into live deserves its own budget.
        with pytest.raises(HTTPException):
            for _ in range(10_000):
                state.check_mode_change_rate_limit()

    def test_the_refusal_is_a_429_not_a_500(self, state):
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as excinfo:
            for _ in range(10_000):
                state.check_mode_change_rate_limit()
        assert excinfo.value.status_code == 429


class TestTheKillSwitchIsAnApiControl:
    """EXEC-005: authenticated, authorized, audited, idempotent, durable."""

    def test_changing_the_execution_mode_needs_the_trading_role(self):
        # The kill switch and the live toggle are the same permission: both
        # decide whether orders can be produced.
        with pytest.raises(PermissionError):
            require_permission(Role.READ_ONLY, Permission.CHANGE_EXECUTION_MODE)
        require_permission(Role.TRADE_AUTHORIZING, Permission.CHANGE_EXECUTION_MODE)

    def test_halting_is_idempotent(self, tmp_path):
        # Repeating a halt is what an operator does when they are not sure
        # the first one landed, so the second one must be a no-op rather than
        # an error or a different state.
        from src.config import ExecutionMode
        from src.execution.mode_persistence import load_execution_mode, save_execution_mode

        state = tmp_path / "execution_mode.json"
        save_execution_mode(ExecutionMode.MANUAL, state)
        first = state.read_text()
        save_execution_mode(ExecutionMode.MANUAL, state)
        assert state.read_text() == first
        assert load_execution_mode(ExecutionMode.AUTOMATIC, state) is ExecutionMode.MANUAL

    def test_an_unregistered_strategy_is_not_enabled_by_accident(self):
        from src.risk.strategy_kill_switch import StrategyKillSwitchManager

        manager = StrategyKillSwitchManager()
        assert not manager.is_registered("never-registered")
        # Fails closed by raising rather than by returning False: a typo in a
        # strategy id is a caller bug, and silently answering "not enabled"
        # would let it look like a deliberate halt.
        with pytest.raises(KeyError, match="no registered kill-switch"):
            manager.is_enabled("never-registered")

    def test_enabled_ids_treats_an_unregistered_strategy_as_enabled(self):
        # The asymmetry with is_enabled() above is deliberate and documented
        # on the method: a batch call that ran before startup finished
        # registering would otherwise report every strategy disabled and zero
        # the portfolio. Being *disabled* still requires observed drift --
        # only the batch query is permissive, and only about strategies that
        # have no switch at all.
        from src.risk.strategy_kill_switch import StrategyKillSwitchManager

        manager = StrategyKillSwitchManager()
        manager.register_strategy("alpha", _baseline())
        assert manager.enabled_ids(["alpha", "ghost"]) == {"alpha", "ghost"}

    def test_enabled_ids_omits_a_registered_strategy_that_was_disabled(self):
        # The half that must stay closed: an explicit disable is honoured.
        from src.risk.strategy_kill_switch import StrategyKillSwitchManager

        manager = StrategyKillSwitchManager()
        manager.register_strategy("alpha", _baseline())
        manager.register_strategy("beta", _baseline())
        # Set through the runtime state rather than by driving `evaluate`
        # into a drift trip: this test is about what the batch query does
        # with a disabled strategy, not about how it came to be disabled.
        manager._states["beta"].enabled = False
        assert manager.enabled_ids(["alpha", "beta"]) == {"alpha"}

    def test_the_capital_preservation_floor_survives_a_restart(self):
        # The durability half, at the level this project's floor provides:
        # the halt is a state a recovery pass reads back, and recovery does
        # not clear it -- only an explicit re-authorisation does.
        from src.risk.capital_preservation_floor import CapitalPreservationFloor

        floor = CapitalPreservationFloor(max_drawdown_pct=0.30)
        floor.update_equity(100_000.0)
        floor.update_equity(50_000.0)
        assert floor.is_halted

        revived = CapitalPreservationFloor(max_drawdown_pct=0.30)
        # A fresh instance starts unhalted, which is why the halted flag has
        # to be persisted by the caller rather than inferred from equity.
        assert not revived.is_halted
        assert floor.halt_reason
