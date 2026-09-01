"""The last unexercised arms of Orchestrator.

Each one is a guard or a fail-open handler that the existing suites walk past:
the blend-audit happy path, the online-trainer construction failure, the two
supervisor loops' generic error arms, the intelligence-join early returns, the
kill-switch drift branch, and two empty-data branches in the portfolio input
assembly.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pandas as pd
import pytest
from test_orchestrator_coverage import _make_orch, _make_storage, _orch_patches

from src.config import Timeframe
from src.engine.orchestrator import _blend_audit
from src.engine.signal_engine import SignalResult
from src.engine.strategy_portfolio import InputNeed


def _result(**overrides) -> SignalResult:
    base = dict(
        tradeable=True,
        direction=1,
        p_long=0.7,
        p_bet=0.6,
        kelly_result=None,
        regime=None,
        gate_result=None,
        skip_reason=None,
    )
    return SignalResult(**(base | overrides))


# ---------------------------------------------------------------------------
# _blend_audit
# ---------------------------------------------------------------------------


def test_a_complete_blend_is_recorded():
    audit = _blend_audit(
        _result(pre_blend_p_long=0.61, ensemble_p_long=0.80, ensemble_blend_weight=0.25)
    )
    assert audit is not None
    assert (audit.pre_blend_p_long, audit.ensemble_p_long, audit.blend_weight) == (
        0.61,
        0.80,
        0.25,
    )


@pytest.mark.parametrize(
    "missing",
    ["pre_blend_p_long", "ensemble_p_long", "ensemble_blend_weight"],
)
def test_a_half_formed_blend_is_treated_as_no_blend(missing):
    fields = {
        "pre_blend_p_long": 0.61,
        "ensemble_p_long": 0.80,
        "ensemble_blend_weight": 0.25,
    }
    fields[missing] = None
    assert _blend_audit(_result(**fields)) is None


# ---------------------------------------------------------------------------
# Startup: the online trainer is optional
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_online_trainer_that_cannot_be_built_leaves_the_engine_running():
    """A cold online model is acceptable; an engine that never comes up is not."""
    orch = _make_orch()
    orch._log = MagicMock()

    with (
        _orch_patches(),
        patch(
            "src.engine.orchestrator.OnlineTrainer",
            side_effect=RuntimeError("model dir unwritable"),
        ),
    ):
        await orch.startup()

    assert orch._engines, "the engine must still be built without an online trainer"
    events = [c[0][0] for c in orch._log.warning.call_args_list]
    assert "orchestrator.online_trainer_init_failed" in events


# ---------------------------------------------------------------------------
# Supervisor loops: a fault must not end the loop silently
# ---------------------------------------------------------------------------


def _sleep_then_stop(orch):
    """First sleep returns; the second (the error backoff) ends the loop."""
    calls = {"n": 0}

    async def _sleep(_seconds):
        calls["n"] += 1
        if calls["n"] >= 2:
            orch._running = False
        return None

    return _sleep


@pytest.mark.asyncio
async def test_the_midnight_reset_loop_survives_a_fault():
    orch = _make_orch()
    orch._log = MagicMock()
    orch._running = True
    orch._all_executors = MagicMock(side_effect=RuntimeError("executor list exploded"))

    with patch("src.engine.orchestrator.asyncio.sleep", side_effect=_sleep_then_stop(orch)):
        await orch._midnight_reset_loop()

    assert orch._log.error.call_args[0][0] == "orchestrator.midnight_reset_error"


@pytest.mark.asyncio
async def test_the_position_monitor_loop_survives_a_fault():
    orch = _make_orch()
    orch._log = MagicMock()
    orch._running = True
    orch._all_executors = MagicMock(side_effect=RuntimeError("executor list exploded"))

    with patch("src.engine.orchestrator.asyncio.sleep", side_effect=_sleep_then_stop(orch)):
        await orch._position_monitor_loop()

    assert orch._log.error.call_args[0][0] == "orchestrator.position_monitor_loop_error"


# ---------------------------------------------------------------------------
# _attach_intelligence_features: the join is best-effort
# ---------------------------------------------------------------------------


def _intel_frame() -> pd.DataFrame:
    return pd.DataFrame({"intelligence_x": [1.0, 2.0]}, index=[1, 2])


@pytest.mark.asyncio
async def test_an_empty_training_matrix_is_left_alone():
    orch = _make_orch()
    orch._storage = _make_storage()
    orch._storage.intelligence_feature_coverage = AsyncMock(return_value={"coverage": 0.9})
    orch._storage.fetch_intelligence_features = AsyncMock(return_value=_intel_frame())

    fm = MagicMock()
    fm.features = pd.DataFrame()

    await orch._attach_intelligence_features(fm, Timeframe.INTRADAY)

    # nothing joined, and no coverage claimed for a matrix with no rows
    assert fm.features.empty
    assert not isinstance(getattr(fm, "intelligence_coverage", None), float)


@pytest.mark.asyncio
async def test_a_matrix_without_features_is_left_alone():
    orch = _make_orch()
    orch._storage = _make_storage()
    orch._storage.intelligence_feature_coverage = AsyncMock(return_value={"coverage": 0.9})
    orch._storage.fetch_intelligence_features = AsyncMock(return_value=_intel_frame())

    class _Bare:
        features = None

    fm = _Bare()
    await orch._attach_intelligence_features(fm, Timeframe.INTRADAY)
    assert fm.features is None


# ---------------------------------------------------------------------------
# _record_kill_switch_outcome
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_drifted_strategy_is_logged_as_kill_switched():
    orch = _make_orch()
    orch._log = MagicMock()

    manager = MagicMock()
    manager.is_registered.return_value = True
    manager.evaluate.return_value = MagicMock(drifted=True, reason="sharpe_collapse", metric=-1.2)

    with patch("src.engine.orchestrator.get_strategy_kill_switch_manager", return_value=manager):
        orch._record_kill_switch_outcome(
            strategy_id="mean_reversion",
            pnl_usd=-50.0,
            actual_direction=-1,
            current_equity=900.0,
            now_ms=1_700_000_000_000,
        )

    event = orch._log.warning.call_args[0][0]
    assert event == "orchestrator.strategy_kill_switched"


# ---------------------------------------------------------------------------
# _build_portfolio_inputs: empty feeds
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_all_nan_funding_column_yields_no_funding_history():
    orch = _make_orch()
    orch._storage = _make_storage()
    orch._storage.fetch_intelligence_features = AsyncMock(
        return_value=pd.DataFrame({"intelligence_binance_funding_rate_pct": [None, None]})
    )

    inputs = await orch._build_portfolio_inputs(Timeframe.INTRADAY, frozenset({InputNeed.FUNDING}))

    assert not inputs.funding_history_pct
    assert inputs.funding_rate_pct is None


@pytest.mark.asyncio
async def test_a_universe_refresh_fault_leaves_the_returns_empty():
    orch = _make_orch()
    orch._log = MagicMock()
    orch._universe_returns = MagicMock()
    orch._universe_returns.trailing_returns = AsyncMock(side_effect=RuntimeError("venue down"))

    inputs = await orch._build_portfolio_inputs(Timeframe.INTRADAY, frozenset({InputNeed.UNIVERSE}))

    assert inputs.universe_returns == {}
    assert orch._log.warning.called


# ---------------------------------------------------------------------------
# Cancellation: both supervisor loops exit rather than swallow the cancel
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("loop_name", ["_midnight_reset_loop", "_position_monitor_loop"])
async def test_a_supervisor_loop_exits_on_cancellation(loop_name):
    orch = _make_orch()
    orch._log = MagicMock()
    orch._running = True

    with patch("src.engine.orchestrator.asyncio.sleep", side_effect=asyncio.CancelledError):
        await getattr(orch, loop_name)()

    # broke out of the loop instead of retrying, and did not log it as an error
    assert orch._running is True
    assert not orch._log.error.called


# ---------------------------------------------------------------------------
# _build_portfolio_inputs: the universe snapshot is refreshed for PAIR too
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_pairs_only_book_refreshes_the_snapshot_without_taking_the_returns():
    """PAIR reads the close series from the same fetch, so the refresh must run."""
    orch = _make_orch()
    orch._universe_returns = MagicMock()
    orch._universe_returns.trailing_returns = AsyncMock(return_value={"ETH/USDT": 0.02})
    orch._pair_series = MagicMock(return_value=(None, None, None))

    inputs = await orch._build_portfolio_inputs(Timeframe.INTRADAY, frozenset({InputNeed.PAIR}))

    orch._universe_returns.trailing_returns.assert_awaited_once()
    assert inputs.universe_returns == {}


# ---------------------------------------------------------------------------
# Startup in LIVE mode with a non-primary timeframe
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_live_mode_gives_non_primary_timeframes_their_own_paper_executor():
    """Scalping and swing never trade real money, even when the mode is LIVE."""
    from src.config import TradingMode

    storage = _make_storage()
    orch = _make_orch(storage)
    orch._cfg.trading_mode = TradingMode.LIVE
    orch._timeframes = [Timeframe.INTRADAY, Timeframe.SCALPING]
    orch._primary_tf = Timeframe.INTRADAY

    with _orch_patches():
        await orch.startup()

    assert orch._non_primary_executor is not None


# ---------------------------------------------------------------------------
# run(): the crypto-box provider loops
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_spawns_the_crypto_box_provider_loops_when_enabled():
    orch = _make_orch()
    orch._engines = {}
    orch._timeframes = []
    orch._crypto_box = MagicMock()
    orch._crypto_box.enabled = True

    async def _idle() -> None:
        await asyncio.sleep(3600)

    provider_task = asyncio.create_task(_idle(), name="crypto_box_provider")
    orch._crypto_box_provider_tasks = MagicMock(return_value=[provider_task])
    orch._midnight_reset_loop = AsyncMock(return_value=None)
    orch._position_monitor_loop = AsyncMock(return_value=None)
    orch._allocation_rebalance_loop = AsyncMock(return_value=None)
    orch._stop_event.set()

    await orch.run()

    orch._crypto_box_provider_tasks.assert_called_once()
    assert provider_task.cancelled() or provider_task.done()


@pytest.mark.asyncio
async def test_a_cross_sectional_book_takes_the_refreshed_returns():
    orch = _make_orch()
    orch._universe_returns = MagicMock()
    orch._universe_returns.trailing_returns = AsyncMock(return_value={"ETH/USDT": 0.02})

    inputs = await orch._build_portfolio_inputs(Timeframe.INTRADAY, frozenset({InputNeed.UNIVERSE}))

    assert inputs.universe_returns == {"ETH/USDT": 0.02}
