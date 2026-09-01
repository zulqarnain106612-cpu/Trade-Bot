"""Endpoint bodies in src/api/main.py that no test reached.

The ledger, recovery, attribution, re-enable, portfolio-evaluation and
size-check routes are driven through TestClient with the module singletons
they read replaced by real objects (a real UnifiedLedger, a real
AttributionTracker) or small fakes where the real thing needs an exchange.
The lifespan tests here cover the crypto-intel-v6 startup block and the
shutdown arms that only run when it is active or when the orchestrator task
overruns its stop timeout.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from src.diagnostics.disaster_recovery import Discrepancy, DiscrepancyType
from src.execution.unified_ledger import VenuePosition


_API_KEY = "x" * 32
_OP_SECRET = "y" * 32


def _make_state():
    from src.api.main import AppState

    s = AppState()
    s.storage = AsyncMock()
    s.storage.insert_audit_event = AsyncMock()
    s.ready = True
    orch = MagicMock()
    orch._executor = MagicMock()
    s.orchestrator = orch
    return s


@pytest.fixture
def state():
    st = _make_state()
    with (
        patch("src.api.main._state", st),
        patch.dict(os.environ, {"API_SECRET_KEY": _API_KEY, "OPERATOR_SECRET": _OP_SECRET}),
        patch("src.api.auth._get_configured_key", return_value=_API_KEY),
    ):
        yield st


@pytest.fixture
def client():
    from src.api.main import app

    return TestClient(app, raise_server_exceptions=False)


def _auth() -> dict[str, str]:
    return {"x-api-key": _API_KEY}


# ---------------------------------------------------------------------------
# /ledger
# ---------------------------------------------------------------------------


def test_ledger_reports_positions_grouped_by_symbol(state, client):
    from src.execution.unified_ledger import UnifiedLedger

    ledger = UnifiedLedger()
    ledger.record_position(VenuePosition("binance", "BTC/USDT", 0.5, 50_000.0, 5_000.0))
    ledger.record_position(VenuePosition("paper", "BTC/USDT", -0.2, 50_100.0, 2_000.0))

    with patch("src.api.main.get_unified_ledger", return_value=ledger):
        resp = client.get("/ledger", headers=_auth())

    assert resp.status_code == 200
    body = resp.json()
    assert len(body["venues"]) == 2
    assert body["by_symbol"]["BTC/USDT"]["net_exposure"] == pytest.approx(0.3)
    assert body["total_margin_used_usd"] == pytest.approx(7_000.0)


# ---------------------------------------------------------------------------
# /recovery
# ---------------------------------------------------------------------------


def _discrepancy() -> Discrepancy:
    return Discrepancy(
        symbol="BTC/USDT",
        discrepancy_type=DiscrepancyType.QUANTITY_MISMATCH,
        local_quantity=0.5,
        reference_quantity=0.4,
    )


class _FakeLiveExecutor:
    def __init__(self, discrepancies=None) -> None:
        self.recovery_discrepancies = list(discrepancies or [])
        self.acknowledged_by: str | None = None

    def acknowledge_recovery(self, operator: str) -> int:
        self.acknowledged_by = operator
        cleared = len(self.recovery_discrepancies)
        self.recovery_discrepancies = []
        return cleared


def test_recovery_status_is_not_applicable_without_a_live_executor(state, client):
    resp = client.get("/recovery/status", headers=_auth())

    assert resp.status_code == 200
    assert resp.json() == {"applicable": False, "blocked": False, "discrepancies": []}


def test_recovery_status_reports_the_block_and_its_discrepancies(state, client):
    executor = _FakeLiveExecutor([_discrepancy()])
    with patch("src.api.main._live_executor_or_none", return_value=executor):
        resp = client.get("/recovery/status", headers=_auth())

    body = resp.json()
    assert body["applicable"] is True
    assert body["blocked"] is True
    assert body["discrepancies"][0] == {
        "symbol": "BTC/USDT",
        "type": "quantity_mismatch",
        "local_quantity": 0.5,
        "exchange_quantity": 0.4,
    }


def test_live_executor_or_none_is_none_when_the_orchestrator_is_absent(state):
    from src.api.main import _live_executor_or_none

    state.orchestrator = None
    assert _live_executor_or_none() is None


def test_live_executor_or_none_ignores_a_paper_executor(state):
    from src.api.main import _live_executor_or_none

    # the orchestrator fixture holds a MagicMock, not a LiveExecutor
    assert _live_executor_or_none() is None


def test_live_executor_or_none_returns_a_real_live_executor(state):
    from src.api.main import _live_executor_or_none
    from src.execution.live import LiveExecutor

    executor = LiveExecutor.__new__(LiveExecutor)
    state.orchestrator._executor = executor
    assert _live_executor_or_none() is executor


def test_acknowledging_recovery_clears_the_block_and_audits_it(state, client):
    executor = _FakeLiveExecutor([_discrepancy()])
    with patch("src.api.main._live_executor_or_none", return_value=executor):
        resp = client.post(
            "/recovery/acknowledge",
            json={"operator": "alice", "operator_secret": _OP_SECRET},
            headers=_auth(),
        )

    assert resp.status_code == 200
    assert resp.json() == {"cleared": 1, "blocked": False, "operator": "alice"}
    assert executor.acknowledged_by == "alice"
    kwargs = state.storage.insert_audit_event.await_args.kwargs
    assert kwargs["event_type"] == "recovery_acknowledged"
    assert kwargs["details"]["discrepancies"][0]["symbol"] == "BTC/USDT"


def test_acknowledging_recovery_without_a_live_executor_is_a_conflict(state, client):
    resp = client.post(
        "/recovery/acknowledge",
        json={"operator": "alice", "operator_secret": _OP_SECRET},
        headers=_auth(),
    )

    assert resp.status_code == 409


# ---------------------------------------------------------------------------
# /strategies/attribution
# ---------------------------------------------------------------------------


def test_attribution_reports_per_strategy_stats(state, client):
    tracker = MagicMock()
    attribution = MagicMock()
    attribution.to_dict.return_value = {"strategy_id": "s1", "trade_count": 3}
    tracker.snapshot.return_value = {"s1": attribution}
    tracker.fill_count.return_value = 3

    with patch("src.api.main.get_attribution_tracker", return_value=tracker):
        resp = client.get("/strategies/attribution", headers=_auth())

    assert resp.status_code == 200
    assert resp.json() == {
        "strategies": {"s1": {"strategy_id": "s1", "trade_count": 3}},
        "fill_count": 3,
    }


# ---------------------------------------------------------------------------
# /strategies/{id}/re-enable
# ---------------------------------------------------------------------------


def test_re_enable_unknown_strategy_is_a_404(state, client):
    manager = MagicMock()
    manager.is_registered.return_value = False

    with patch("src.api.main.get_strategy_kill_switch_manager", return_value=manager):
        resp = client.post(
            "/strategies/nope/re-enable",
            json={"operator": "alice", "operator_secret": _OP_SECRET},
            headers=_auth(),
        )

    assert resp.status_code == 404


def test_re_enable_reports_the_failed_gauntlet_criteria(state, client):
    from src.risk.strategy_kill_switch import GauntletNotPassedError

    manager = MagicMock()
    manager.is_registered.return_value = True
    manager.re_enable.side_effect = GauntletNotPassedError("s1", ("sharpe", "trade_count"))

    with patch("src.api.main.get_strategy_kill_switch_manager", return_value=manager):
        resp = client.post(
            "/strategies/s1/re-enable",
            json={"operator": "alice", "operator_secret": _OP_SECRET},
            headers=_auth(),
        )

    assert resp.status_code == 409
    assert resp.json()["detail"]["failed_criteria"] == ["sharpe", "trade_count"]


def test_re_enable_succeeds_and_records_the_override(state, client):
    manager = MagicMock()
    manager.is_registered.return_value = True

    with patch("src.api.main.get_strategy_kill_switch_manager", return_value=manager):
        resp = client.post(
            "/strategies/s1/re-enable",
            json={"operator": "alice", "operator_secret": _OP_SECRET, "force": True},
            headers=_auth(),
        )

    assert resp.status_code == 200
    assert resp.json() == {"strategy_id": "s1", "enabled": True, "forced": True}
    manager.re_enable.assert_called_once_with("s1", force=True)
    kwargs = state.storage.insert_audit_event.await_args.kwargs
    assert kwargs["details"] == {"strategy_id": "s1", "forced": True}


# ---------------------------------------------------------------------------
# /risk/size-check
# ---------------------------------------------------------------------------


def _size_check_body(**overrides) -> dict:
    base = {
        "symbol": "BTC/USDT",
        "group": "crypto_large_cap",
        "capital_usd": 100_000.0,
        "current_equity": 100_000.0,
        "hwm": 100_000.0,
        "realized_vol_pct": 80.0,
        "win_rate": 0.55,
        "avg_win_usd": 300.0,
        "avg_loss_usd": 200.0,
    }
    return base | overrides


def test_size_check_returns_a_vol_targeted_notional(state, client):
    resp = client.post("/risk/size-check", json=_size_check_body(), headers=_auth())

    assert resp.status_code == 200
    body = resp.json()
    assert body["final_notional_usd"] >= 0.0
    assert isinstance(body["allowed"], bool)
    assert set(body["vol_target"]) >= {"notional_usd", "vol_target_notional"}
    assert "budget_check" in body


def test_size_check_rejects_when_the_sizer_returns_nothing(state, client):
    # a deep drawdown against the high-water mark takes the notional to zero,
    # which the endpoint must surface as allowed=False with a reason.
    resp = client.post(
        "/risk/size-check",
        json=_size_check_body(current_equity=10_000.0, hwm=100_000.0),
        headers=_auth(),
    )

    body = resp.json()
    assert body["allowed"] is False
    assert body["reject_reason"]


# ---------------------------------------------------------------------------
# lifespan: crypto-intel-v6 startup and the shutdown arms
# ---------------------------------------------------------------------------


class _FetcherCtx:
    def __init__(self, fetcher):
        self._fetcher = fetcher

    async def __aenter__(self):
        return self._fetcher

    async def __aexit__(self, *exc):
        return False


def _lifespan_patches(orch):
    fetcher = AsyncMock()
    fetcher.close = AsyncMock()
    return [
        patch.dict(
            os.environ,
            {"API_SECRET_KEY": _API_KEY, "OPERATOR_SECRET": _OP_SECRET, "INTEL_ENABLED": "true"},
            clear=True,
        ),
        patch("src.api.main.create_storage_backend", return_value=AsyncMock()),
        patch("src.api.main.open_fetcher", return_value=_FetcherCtx(fetcher)),
        patch("src.api.main.Orchestrator", return_value=orch),
    ]


def _orchestrator() -> MagicMock:
    orch = MagicMock()
    orch.startup = AsyncMock()
    orch.run = AsyncMock(return_value=None)
    orch.stop = MagicMock()
    orch.shutdown = AsyncMock()
    return orch


def test_lifespan_starts_and_closes_crypto_intel_when_enabled():
    from src.api import main as main_mod

    intel = MagicMock()
    adapter = MagicMock()
    adapter._intel = intel
    orch = _orchestrator()

    async def _run():
        with contextlib.ExitStack() as stack:
            for ctx in _lifespan_patches(orch):
                stack.enter_context(ctx)
            stack.enter_context(patch("src.intel.CryptoIntelligence", return_value=intel))
            stack.enter_context(
                patch(
                    "src.intelligence.intelligence_adapter.IntelligenceAdapter",
                    return_value=adapter,
                )
            )
            async with main_mod.lifespan(main_mod.app):
                assert main_mod._state.intel_adapter is adapter

    asyncio.run(_run())
    intel.start.assert_called_once()
    intel.close.assert_called_once()


def test_lifespan_survives_a_crypto_intel_that_fails_to_start():
    from src.api import main as main_mod

    orch = _orchestrator()
    main_mod._state.intel_adapter = None

    async def _run():
        with contextlib.ExitStack() as stack:
            for ctx in _lifespan_patches(orch):
                stack.enter_context(ctx)
            stack.enter_context(
                patch("src.intel.CryptoIntelligence", side_effect=RuntimeError("no config"))
            )
            async with main_mod.lifespan(main_mod.app):
                assert main_mod._state.intel_adapter is None

    asyncio.run(_run())
    orch.shutdown.assert_awaited_once()


def test_lifespan_survives_a_crypto_intel_that_fails_to_close():
    from src.api import main as main_mod

    intel = MagicMock()
    intel.close.side_effect = RuntimeError("already gone")
    adapter = MagicMock()
    adapter._intel = intel
    orch = _orchestrator()

    async def _run():
        with contextlib.ExitStack() as stack:
            for ctx in _lifespan_patches(orch):
                stack.enter_context(ctx)
            stack.enter_context(patch("src.intel.CryptoIntelligence", return_value=intel))
            stack.enter_context(
                patch(
                    "src.intelligence.intelligence_adapter.IntelligenceAdapter",
                    return_value=adapter,
                )
            )
            async with main_mod.lifespan(main_mod.app):
                pass

    asyncio.run(_run())
    orch.shutdown.assert_awaited_once()


def test_lifespan_cancels_an_orchestrator_task_that_overruns_its_stop_timeout():
    from src.api import main as main_mod

    orch = _orchestrator()
    cancelled: dict[str, bool] = {}

    async def _never_returns():
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled["yes"] = True
            raise

    orch.run = _never_returns

    async def _run():
        with contextlib.ExitStack() as stack:
            for ctx in _lifespan_patches(orch):
                stack.enter_context(ctx)
            stack.enter_context(
                patch("src.intel.CryptoIntelligence", side_effect=RuntimeError("off"))
            )
            stack.enter_context(patch("asyncio.wait_for", AsyncMock(side_effect=TimeoutError)))
            async with main_mod.lifespan(main_mod.app):
                await asyncio.sleep(0)  # let the orchestrator task actually start
        # let the cancellation the shutdown requested reach the task
        await asyncio.sleep(0.05)

    asyncio.run(_run())
    assert cancelled == {"yes": True}, "the overrunning orchestrator task must be cancelled"


# ---------------------------------------------------------------------------
# /strategies/portfolio
# ---------------------------------------------------------------------------


def test_strategy_portfolio_reports_when_the_orchestrator_has_not_started(state, client):
    state.orchestrator = None
    resp = client.get("/strategies/portfolio", headers=_auth())

    assert resp.status_code == 200
    assert resp.json() == {"evaluations": {}, "reason": "orchestrator_not_started"}


def test_strategy_portfolio_returns_every_timeframe_by_default(state, client):
    state.orchestrator.portfolio_evaluation.return_value = {"15m": {"signal_engine_v1": "ok"}}

    resp = client.get("/strategies/portfolio", headers=_auth())

    assert resp.json() == {"evaluations": {"15m": {"signal_engine_v1": "ok"}}}
    state.orchestrator.portfolio_evaluation.assert_called_once_with(None)


def test_strategy_portfolio_scopes_to_one_timeframe_when_asked(state, client):
    state.orchestrator.portfolio_evaluation.return_value = {"signal_engine_v1": "ok"}

    resp = client.get("/strategies/portfolio?timeframe=15m", headers=_auth())

    assert resp.json() == {"timeframe": "15m", "evaluation": {"signal_engine_v1": "ok"}}
    state.orchestrator.portfolio_evaluation.assert_called_once_with("15m")


def test_lifespan_shutdown_tolerates_an_adapter_without_an_intel_object():
    from src.api import main as main_mod

    adapter = MagicMock(spec=[])  # no _intel attribute at all
    orch = _orchestrator()

    async def _run():
        with contextlib.ExitStack() as stack:
            for ctx in _lifespan_patches(orch):
                stack.enter_context(ctx)
            stack.enter_context(patch("src.intel.CryptoIntelligence", return_value=MagicMock()))
            stack.enter_context(
                patch(
                    "src.intelligence.intelligence_adapter.IntelligenceAdapter",
                    return_value=adapter,
                )
            )
            async with main_mod.lifespan(main_mod.app):
                pass

    asyncio.run(_run())
    orch.shutdown.assert_awaited_once()
