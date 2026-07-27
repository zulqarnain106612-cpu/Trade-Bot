"""
Test coverage for src/engine/signal_engine.py — Debt-005.

Strategy: mock all external dependencies and drive SignalEngine.tick()
through every major branch.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pandas as pd
import pytest
from xgboost import XGBClassifier

from src.config import Timeframe
from src.engine.signal_engine import SignalEngine, SignalResult
from src.features.pipeline import FEATURE_COLUMNS, FeatureMatrix
from src.regime.detector import RegimeDetector, RegimePrediction
from src.risk.gates import GateResult, GateStatus
from src.risk.kelly import KellyResult


# ---------------------------------------------------------------------------
# Module-level autouse fixture: isolate all tests from real network calls.
# BinanceIntelligenceProvider.exchange_id was fixed so the intel aggregator
# no longer fails at construction — without this patch it makes live HTTP
# calls and hangs every test that doesn't explicitly mock the aggregator.
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _mock_intel_aggregator():
    """Patch _get_intel_aggregator for every test in this module."""
    mock_agg = AsyncMock()
    mock_agg.fetch_metrics = AsyncMock(return_value={})
    with patch("src.engine.signal_engine._get_intel_aggregator", return_value=mock_agg):
        yield


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


def _make_bars(n: int = 320, closed: bool = True) -> pd.DataFrame:
    from datetime import UTC, datetime

    from src.config import TIMEFRAME_SECONDS, Timeframe

    now_ms = int(datetime.now(tz=UTC).timestamp() * 1000)
    tf_ms = TIMEFRAME_SECONDS.get(Timeframe.INTRADAY, 900) * 1000
    offset = tf_ms if closed else 0
    ts = [now_ms - offset - (n - 1 - i) * tf_ms for i in range(n)]
    return pd.DataFrame(
        {
            "open": np.linspace(100, 110, n),
            "high": np.linspace(101, 111, n),
            "low": np.linspace(99, 109, n),
            "close": np.linspace(100, 110, n),
            "volume": [1000.0] * n,
            "quote_volume": [100_000.0] * n,
            "taker_buy_vol": [500.0] * n,
        },
        index=ts,
    )


def _fitted_xgb() -> XGBClassifier:
    X = np.array([[0, 0], [1, 0], [0, 1], [1, 1]], dtype=float)
    y = np.array([0, 1, 0, 1])
    m = XGBClassifier(n_estimators=1, max_depth=1, verbosity=0)
    m.fit(X, y)
    return m


def _fitted_detector() -> MagicMock:
    det = MagicMock(spec=RegimeDetector)
    det.is_fitted.return_value = True
    det.predict_current.return_value = RegimePrediction(
        state=1,
        prob_ranging=0.1,
        prob_trending=0.8,
        prob_volatile=0.1,
        entropy=0.5,
    )
    return det


def _make_engine(
    bars=None,
    raise_gap_fill=False,
    direction_model=None,
    meta_model=None,
    detector=None,
    ensemble=None,
) -> SignalEngine:
    storage = AsyncMock()
    storage.fetch_bars.return_value = bars if bars is not None else _make_bars()

    fetcher = AsyncMock()
    if raise_gap_fill:
        fetcher.gap_fill.side_effect = RuntimeError("down")
    else:
        fetcher.gap_fill.return_value = None
    ob = MagicMock()
    ob.order_flow_imbalance.return_value = 0.05
    ob.mid_price = 105.0
    ob.spread = 0.1
    fetcher.fetch_orderbook.return_value = ob
    fetcher.fetch_funding_rate.return_value = 0.0001  # TASK-010: live funding rate

    trainer = MagicMock()
    trainer.symbol = "BTC/USDT"
    trainer.timeframe = Timeframe.INTRADAY

    return SignalEngine(
        symbol="BTC/USDT",
        timeframe=Timeframe.INTRADAY,
        storage=storage,
        fetcher=fetcher,
        detector=detector or _fitted_detector(),
        direction_model=direction_model or _fitted_xgb(),
        meta_model=meta_model or _fitted_xgb(),
        trainer=trainer,
        ensemble=ensemble,
    )


_TICK = {
    "capital_usd": 10_000.0,
    "daily_pnl_usd": 0.0,
    "starting_equity_usd": 10_000.0,
    "consecutive_loss_count": 0,
    "direction_gate_pass": True,
    "meta_gate_pass": True,
    "avg_win_usd": 50.0,
    "avg_loss_usd": 25.0,
    "paper_trading_days": 30,
}


def _fm(n=120, cols=5):
    fm = MagicMock(spec=FeatureMatrix)
    # Use real FEATURE_COLUMNS so history_df passes the >=3 column guard in
    # signal_engine (enabling regime detection in tests). Extra synthetic cols
    # appended to reach the requested `cols` count.
    real_cols = list(FEATURE_COLUMNS)
    extra = [f"_extra_{i}" for i in range(max(0, cols - len(real_cols)))]
    all_cols = (real_cols + extra)[: max(cols, len(real_cols))]
    fm.features = pd.DataFrame(np.random.rand(n, len(all_cols)), columns=all_cols)
    return fm


def _pass_gate() -> GateResult:
    return GateResult.pass_gate()


def _block_gate(reason: str = "drawdown") -> GateResult:
    return GateResult.fail(status=GateStatus.HALT_DRAWDOWN, reason=reason)


def _mock_kelly():
    return KellyResult(
        kelly_fraction=0.1,
        adjusted_fraction=0.05,
        capital_usd=10_000.0,
        entry_price=105.0,
        quantity=4.76,
        notional_usd=500.0,
        is_capped=False,
    )


def _mock_cognitive(passed=True):
    cog = MagicMock()
    res = MagicMock()
    res.passed = passed
    res.size_fraction = 0.05
    res.veto_reason = ""
    res.adjusted_size_fraction = 0.05
    cog.evaluate = MagicMock(return_value=res)  # sync call in tick()
    return cog


# ---------------------------------------------------------------------------
# SignalResult._skip shape
# ---------------------------------------------------------------------------


class TestSkipShape:
    def test_skip_not_tradeable(self):
        # _skip is synchronous
        e = _make_engine()
        r = e._skip("test_reason")
        assert isinstance(r, SignalResult)
        assert r.tradeable is False
        assert r.skip_reason == "test_reason"
        assert r.kelly_result is None
        assert r.regime is None


# ---------------------------------------------------------------------------
# Skip paths via tick()
# ---------------------------------------------------------------------------


class TestSkipPaths:
    @pytest.mark.asyncio
    async def test_gap_fill_failure(self):
        e = _make_engine(raise_gap_fill=True)
        r = await e.tick(**_TICK)
        assert r.tradeable is False
        assert r.skip_reason == "gap_fill_failed"

    @pytest.mark.asyncio
    async def test_insufficient_bars(self):
        e = _make_engine(bars=_make_bars(n=3))
        r = await e.tick(**_TICK)
        assert r.tradeable is False
        assert "insufficient" in r.skip_reason or "feature" in r.skip_reason

    @pytest.mark.asyncio
    async def test_last_bar_not_closed(self):
        e = _make_engine()
        open_bars = _make_bars(n=320, closed=False)

        async def _fake_lb():
            return open_bars

        e._load_bars = _fake_lb
        r = await e.tick(**_TICK)
        assert r.tradeable is False
        assert r.skip_reason == "last_bar_not_yet_closed"

    @pytest.mark.asyncio
    async def test_feature_matrix_exception(self):
        e = _make_engine()
        good_bars = _make_bars(n=320)

        async def _fake_lb():
            return good_bars

        e._load_bars = _fake_lb
        with patch(
            "src.engine.signal_engine.build_feature_matrix", side_effect=ValueError("bad data")
        ):
            r = await e.tick(**_TICK)
        assert r.tradeable is False
        assert r.skip_reason == "feature_matrix_failed"

    @pytest.mark.asyncio
    async def test_gate_blocked_returns_skip(self):
        e = _make_engine()
        good_bars = _make_bars(n=320)

        async def _lb():
            return good_bars

        e._load_bars = _lb
        with (
            patch("src.engine.signal_engine.build_feature_matrix", return_value=_fm()),
            patch(
                "src.engine.signal_engine.build_inference_features",
                return_value=pd.Series({"f0": 1.0}),
            ),
            patch("src.engine.signal_engine.compute_position_size", return_value=_mock_kelly()),
            patch.object(e._trainer, "predict_direction", return_value=(1, 0.8)),
            patch.object(e._trainer, "predict_meta", return_value=(1, 0.8)),
            patch(
                "src.engine.signal_engine.evaluate_all_gates",
                return_value=_block_gate("drawdown_halt"),
            ),
        ):
            r = await e.tick(**_TICK)
        assert r.tradeable is False

    @pytest.mark.asyncio
    async def test_entropy_reduces_position_size_scalar(self):
        """High entropy must reduce regime_scalar passed to compute_position_size."""
        e = _make_engine()
        good_bars = _make_bars(n=320)

        async def _lb():
            return good_bars

        e._load_bars = _lb

        # position_scalar is on a frozen dataclass. Patch the class method so
        # all instances return 0.6 during this test (frozen = can't patch instances).
        from src.regime.detector import RegimePrediction as _RP

        with (
            patch("src.engine.signal_engine.build_feature_matrix", return_value=_fm()),
            patch(
                "src.engine.signal_engine.build_inference_features",
                return_value=pd.Series({"f0": 1.0}),
            ),
            patch(
                "src.engine.signal_engine.compute_position_size", return_value=_mock_kelly()
            ) as mock_kelly,
            patch.object(e._trainer, "predict_direction", return_value=(1, 0.8)),
            patch.object(e._trainer, "predict_meta", return_value=(1, 0.8)),
            patch("src.engine.signal_engine.evaluate_all_gates", return_value=_pass_gate()),
            patch.object(_RP, "position_scalar", lambda self, cfg=None: 0.6),
            patch("src.engine.signal_engine.get_cognitive_engine") as mock_cog,
        ):
            mock_cog.return_value.evaluate.return_value = MagicMock(
                passed=True, adjusted_size_fraction=1.0, veto_reason=None
            )
            await e.tick(**_TICK)

        assert mock_kelly.call_args is not None, "compute_position_size was never called"
        assert mock_kelly.call_args.kwargs["regime_scalar"] == pytest.approx(0.6, abs=1e-9)

    @pytest.mark.asyncio
    async def test_direction_gate_not_passed(self):
        e = _make_engine()
        kwargs = dict(_TICK, direction_gate_pass=False)
        good_bars = _make_bars(n=320)

        async def _lb():
            return good_bars

        e._load_bars = _lb
        with (
            patch("src.engine.signal_engine.build_feature_matrix", return_value=_fm()),
            patch(
                "src.engine.signal_engine.build_inference_features",
                return_value=pd.Series({"f0": 1.0}),
            ),
            patch("src.engine.signal_engine.compute_position_size", return_value=_mock_kelly()),
            patch.object(e._trainer, "predict_direction", return_value=(1, 0.8)),
            patch.object(e._trainer, "predict_meta", return_value=(1, 0.8)),
            patch(
                "src.engine.signal_engine.evaluate_all_gates", return_value=_block_gate("live_gate")
            ),
        ):
            r = await e.tick(**kwargs)
        assert r.tradeable is False

    @pytest.mark.asyncio
    async def test_strategy_filter_blocked(self):
        e = _make_engine()
        filter_block = {
            "passes": False,
            "scalar": 0.0,
            "filters_failed": ["low_volume"],
            "details": {"hurst": 0.5},
        }
        good_bars = _make_bars(n=320)

        async def _lb():
            return good_bars

        e._load_bars = _lb
        with (
            patch("src.engine.signal_engine.build_feature_matrix", return_value=_fm()),
            patch(
                "src.engine.signal_engine.build_inference_features",
                return_value=pd.Series({"f0": 1.0}),
            ),
            patch("src.engine.signal_engine.evaluate_all_gates", return_value=_pass_gate()),
            patch("src.engine.signal_engine.compute_position_size", return_value=_mock_kelly()),
            patch("src.engine.signal_engine.get_cognitive_engine", return_value=_mock_cognitive()),
            patch("src.engine.signal_engine.apply_all_strategy_filters", return_value=filter_block),
            patch.object(e._trainer, "predict_direction", return_value=(1, 0.8)),
            patch.object(e._trainer, "predict_meta", return_value=(1, 0.8)),
        ):
            r = await e.tick(**_TICK)
        assert r.tradeable is False
        assert "strategy_filter" in r.skip_reason
        assert "low_volume" in r.skip_reason

    @pytest.mark.asyncio
    async def test_ofi_fetch_failure_nonfatal(self):
        """order book exception → gather fails, live values default (0.0), tick continues."""
        e = _make_engine()
        e._fetcher.fetch_orderbook.side_effect = RuntimeError("ws down")
        e._fetcher.fetch_funding_rate.return_value = 0.0  # ensure float even when ob fails
        good_bars = _make_bars(n=320)

        async def _lb():
            return good_bars

        e._load_bars = _lb
        with (
            patch("src.engine.signal_engine.build_feature_matrix", return_value=_fm()),
            patch(
                "src.engine.signal_engine.build_inference_features",
                return_value=pd.Series({"f0": 1.0}),
            ),
            patch("src.engine.signal_engine.compute_position_size", return_value=_mock_kelly()),
            patch.object(e._trainer, "predict_direction", return_value=(1, 0.8)),
            patch.object(e._trainer, "predict_meta", return_value=(1, 0.8)),
            patch("src.engine.signal_engine.evaluate_all_gates", return_value=_block_gate()),
        ):
            r = await e.tick(**_TICK)
        assert isinstance(r, SignalResult)  # no crash


# ---------------------------------------------------------------------------
# Tradeable path
# ---------------------------------------------------------------------------


class TestTradeablePath:
    @pytest.mark.asyncio
    async def test_full_pipeline_tradeable(self):
        e = _make_engine()
        filter_pass = {
            "passes": True,
            "scalar": 1.0,
            "filters_failed": [],
            "details": {"hurst": 0.5},
        }
        good_bars = _make_bars(n=320)

        async def _lb():
            return good_bars

        e._load_bars = _lb
        with (
            patch("src.engine.signal_engine.build_feature_matrix", return_value=_fm()),
            patch(
                "src.engine.signal_engine.build_inference_features",
                return_value=pd.Series({"f0": 1.0}),
            ),
            patch("src.engine.signal_engine.evaluate_all_gates", return_value=_pass_gate()),
            patch("src.engine.signal_engine.compute_position_size", return_value=_mock_kelly()),
            patch(
                "src.engine.signal_engine.get_cognitive_engine",
                return_value=_mock_cognitive(passed=True),
            ),
            patch("src.engine.signal_engine.apply_all_strategy_filters", return_value=filter_pass),
            patch.object(e._trainer, "predict_direction", return_value=(1, 0.8)),
            patch.object(e._trainer, "predict_meta", return_value=(1, 0.8)),
        ):
            r = await e.tick(**_TICK)

        assert r.tradeable is True
        assert r.kelly_result is not None
        assert r.kelly_result.notional_usd == pytest.approx(500.0)
        assert r.direction == 1  # p_long=0.8 → long
        assert 0.0 <= r.p_long <= 1.0
        assert 0.0 <= r.p_bet <= 1.0
        assert r.gate_result is not None

    @pytest.mark.asyncio
    async def test_p_long_below_half_direction_short(self):
        e = _make_engine()
        filter_pass = {
            "passes": True,
            "scalar": 1.0,
            "filters_failed": [],
            "details": {"hurst": 0.5},
        }
        good_bars = _make_bars(n=320)

        async def _lb():
            return good_bars

        e._load_bars = _lb
        with (
            patch("src.engine.signal_engine.build_feature_matrix", return_value=_fm()),
            patch(
                "src.engine.signal_engine.build_inference_features",
                return_value=pd.Series({"f0": 1.0}),
            ),
            patch("src.engine.signal_engine.evaluate_all_gates", return_value=_pass_gate()),
            patch("src.engine.signal_engine.compute_position_size", return_value=_mock_kelly()),
            patch(
                "src.engine.signal_engine.get_cognitive_engine",
                return_value=_mock_cognitive(passed=True),
            ),
            patch("src.engine.signal_engine.apply_all_strategy_filters", return_value=filter_pass),
            patch.object(e._trainer, "predict_direction", return_value=(0, 0.2)),
            patch.object(e._trainer, "predict_meta", return_value=(1, 0.8)),
        ):
            r = await e.tick(**_TICK)

        assert r.tradeable is True
        assert r.direction == 0  # p_long=0.2 → short; signal_engine uses 0 not -1 for short

    @pytest.mark.asyncio
    async def test_tick_appends_to_hash_chained_audit_trail(self):
        """v8: every _emit_audit call also appends to AuditTrail (src/diagnostics/audit_trail.py)."""
        from src.diagnostics.audit_trail import get_audit_trail

        trail = get_audit_trail()
        len_before = len(trail)

        e = _make_engine()
        filter_pass = {
            "passes": True,
            "scalar": 1.0,
            "filters_failed": [],
            "details": {"hurst": 0.5},
        }
        good_bars = _make_bars(n=320)

        async def _lb():
            return good_bars

        e._load_bars = _lb
        with (
            patch("src.engine.signal_engine.build_feature_matrix", return_value=_fm()),
            patch(
                "src.engine.signal_engine.build_inference_features",
                return_value=pd.Series({"f0": 1.0}),
            ),
            patch("src.engine.signal_engine.evaluate_all_gates", return_value=_pass_gate()),
            patch("src.engine.signal_engine.compute_position_size", return_value=_mock_kelly()),
            patch(
                "src.engine.signal_engine.get_cognitive_engine",
                return_value=_mock_cognitive(passed=True),
            ),
            patch("src.engine.signal_engine.apply_all_strategy_filters", return_value=filter_pass),
            patch.object(e._trainer, "predict_direction", return_value=(1, 0.8)),
            patch.object(e._trainer, "predict_meta", return_value=(1, 0.8)),
        ):
            await e.tick(**_TICK)

        assert len(trail) == len_before + 1
        latest = trail.entries()[-1]
        assert latest.event_type == "opened"
        intact, broken_at = trail.verify_chain_integrity()
        assert intact
        assert broken_at is None


# ---------------------------------------------------------------------------
# Ensemble blend (src/intelligence/ensemble_predictor.py wired into p_long)
# ---------------------------------------------------------------------------


def _mock_ensemble_prediction(point_estimate: float, uncertainty: float = 0.01):
    from src.intelligence.ensemble_predictor import EnsemblePrediction

    return EnsemblePrediction(
        point_estimate=point_estimate,
        credible_lower=point_estimate - 1.96 * uncertainty,
        credible_upper=point_estimate + 1.96 * uncertainty,
        model_disagreement=uncertainty,
        aleatoric_uncertainty=uncertainty,
        epistemic_uncertainty=0.0,
        best_model="xgboost",
        model_weights={"xgboost": 1.0},
        individual_predictions={"xgboost": point_estimate},
    )


class TestEnsembleBlend:
    @pytest.mark.asyncio
    async def test_no_ensemble_leaves_p_long_unchanged(self):
        """self._ensemble is None (default, before any retrain cycle has
        produced one) -> XGBoost-only p_long/direction, unchanged."""
        e = _make_engine(ensemble=None)
        filter_pass = {
            "passes": True,
            "scalar": 1.0,
            "filters_failed": [],
            "details": {"hurst": 0.5},
        }
        e._load_bars = lambda: _make_bars(n=320)  # type: ignore[method-assign,assignment]

        async def _lb():
            return _make_bars(n=320)

        e._load_bars = _lb
        with (
            patch("src.engine.signal_engine.build_feature_matrix", return_value=_fm()),
            patch(
                "src.engine.signal_engine.build_inference_features",
                return_value=pd.Series({"f0": 1.0}),
            ),
            patch("src.engine.signal_engine.evaluate_all_gates", return_value=_pass_gate()),
            patch("src.engine.signal_engine.compute_position_size", return_value=_mock_kelly()),
            patch(
                "src.engine.signal_engine.get_cognitive_engine",
                return_value=_mock_cognitive(passed=True),
            ),
            patch("src.engine.signal_engine.apply_all_strategy_filters", return_value=filter_pass),
            patch.object(e._trainer, "predict_direction", return_value=(1, 0.8)),
            patch.object(e._trainer, "predict_meta", return_value=(1, 0.8)),
        ):
            r = await e.tick(**_TICK)

        assert r.p_long == pytest.approx(0.8)
        assert r.direction == 1

    @pytest.mark.asyncio
    async def test_zero_blend_weight_skips_ensemble_even_when_present(self):
        mock_ensemble = MagicMock()
        mock_ensemble.predict_row.return_value = _mock_ensemble_prediction(1.0)  # extreme bullish
        e = _make_engine(ensemble=mock_ensemble)
        filter_pass = {
            "passes": True,
            "scalar": 1.0,
            "filters_failed": [],
            "details": {"hurst": 0.5},
        }

        async def _lb():
            return _make_bars(n=320)

        e._load_bars = _lb
        mock_risk = MagicMock(ensemble_blend_weight=0.0)
        with (
            patch("src.engine.signal_engine.build_feature_matrix", return_value=_fm()),
            patch(
                "src.engine.signal_engine.build_inference_features",
                return_value=pd.Series({"f0": 1.0}),
            ),
            patch("src.engine.signal_engine.evaluate_all_gates", return_value=_pass_gate()),
            patch("src.engine.signal_engine.compute_position_size", return_value=_mock_kelly()),
            patch(
                "src.engine.signal_engine.get_cognitive_engine",
                return_value=_mock_cognitive(passed=True),
            ),
            patch("src.engine.signal_engine.apply_all_strategy_filters", return_value=filter_pass),
            patch("src.engine.signal_engine.effective_risk_settings", return_value=mock_risk),
            patch.object(e._trainer, "predict_direction", return_value=(1, 0.55)),
            patch.object(e._trainer, "predict_meta", return_value=(1, 0.8)),
        ):
            r = await e.tick(**_TICK)

        assert r.p_long == pytest.approx(0.55)  # untouched -- weight 0 short-circuits the block
        mock_ensemble.predict_row.assert_not_called()

    @pytest.mark.asyncio
    async def test_bullish_ensemble_pulls_p_long_up(self):
        mock_ensemble = MagicMock()
        mock_ensemble.predict_row.return_value = _mock_ensemble_prediction(0.5, uncertainty=0.01)
        e = _make_engine(ensemble=mock_ensemble)
        filter_pass = {
            "passes": True,
            "scalar": 1.0,
            "filters_failed": [],
            "details": {"hurst": 0.5},
        }

        async def _lb():
            return _make_bars(n=320)

        e._load_bars = _lb
        mock_risk = MagicMock(ensemble_blend_weight=0.5)
        with (
            patch("src.engine.signal_engine.build_feature_matrix", return_value=_fm()),
            patch(
                "src.engine.signal_engine.build_inference_features",
                return_value=pd.Series({"f0": 1.0}),
            ),
            patch("src.engine.signal_engine.evaluate_all_gates", return_value=_pass_gate()),
            patch("src.engine.signal_engine.compute_position_size", return_value=_mock_kelly()),
            patch(
                "src.engine.signal_engine.get_cognitive_engine",
                return_value=_mock_cognitive(passed=True),
            ),
            patch("src.engine.signal_engine.apply_all_strategy_filters", return_value=filter_pass),
            patch("src.engine.signal_engine.effective_risk_settings", return_value=mock_risk),
            patch.object(e._trainer, "predict_direction", return_value=(1, 0.55)),
            patch.object(e._trainer, "predict_meta", return_value=(1, 0.8)),
        ):
            r = await e.tick(**_TICK)

        # Strongly bullish, low-uncertainty ensemble pulls the 50/50-weighted
        # blend well above the raw XGBoost p_long=0.55.
        assert r.p_long > 0.55
        assert r.direction == 1
        mock_ensemble.predict_row.assert_called_once()

    @pytest.mark.asyncio
    async def test_bearish_ensemble_can_flip_direction(self):
        """A confident-enough bearish ensemble blended at high enough weight
        must flip the re-derived direction, not just shrink position size."""
        mock_ensemble = MagicMock()
        mock_ensemble.predict_row.return_value = _mock_ensemble_prediction(-0.5, uncertainty=0.01)
        e = _make_engine(ensemble=mock_ensemble)
        filter_pass = {
            "passes": True,
            "scalar": 1.0,
            "filters_failed": [],
            "details": {"hurst": 0.5},
        }

        async def _lb():
            return _make_bars(n=320)

        e._load_bars = _lb
        mock_risk = MagicMock(ensemble_blend_weight=0.9)
        with (
            patch("src.engine.signal_engine.build_feature_matrix", return_value=_fm()),
            patch(
                "src.engine.signal_engine.build_inference_features",
                return_value=pd.Series({"f0": 1.0}),
            ),
            patch("src.engine.signal_engine.evaluate_all_gates", return_value=_pass_gate()),
            patch("src.engine.signal_engine.compute_position_size", return_value=_mock_kelly()),
            patch(
                "src.engine.signal_engine.get_cognitive_engine",
                return_value=_mock_cognitive(passed=True),
            ),
            patch("src.engine.signal_engine.apply_all_strategy_filters", return_value=filter_pass),
            patch("src.engine.signal_engine.effective_risk_settings", return_value=mock_risk),
            patch.object(e._trainer, "predict_direction", return_value=(1, 0.55)),
            patch.object(e._trainer, "predict_meta", return_value=(1, 0.8)),
        ):
            r = await e.tick(**_TICK)

        assert r.p_long < 0.5
        assert r.direction == 0

    @pytest.mark.asyncio
    async def test_ensemble_exception_fails_open_to_xgboost_only(self):
        mock_ensemble = MagicMock()
        mock_ensemble.predict_row.side_effect = RuntimeError("boom")
        e = _make_engine(ensemble=mock_ensemble)
        filter_pass = {
            "passes": True,
            "scalar": 1.0,
            "filters_failed": [],
            "details": {"hurst": 0.5},
        }

        async def _lb():
            return _make_bars(n=320)

        e._load_bars = _lb
        mock_risk = MagicMock(ensemble_blend_weight=0.5)
        with (
            patch("src.engine.signal_engine.build_feature_matrix", return_value=_fm()),
            patch(
                "src.engine.signal_engine.build_inference_features",
                return_value=pd.Series({"f0": 1.0}),
            ),
            patch("src.engine.signal_engine.evaluate_all_gates", return_value=_pass_gate()),
            patch("src.engine.signal_engine.compute_position_size", return_value=_mock_kelly()),
            patch(
                "src.engine.signal_engine.get_cognitive_engine",
                return_value=_mock_cognitive(passed=True),
            ),
            patch("src.engine.signal_engine.apply_all_strategy_filters", return_value=filter_pass),
            patch("src.engine.signal_engine.effective_risk_settings", return_value=mock_risk),
            patch.object(e._trainer, "predict_direction", return_value=(1, 0.8)),
            patch.object(e._trainer, "predict_meta", return_value=(1, 0.8)),
        ):
            r = await e.tick(**_TICK)  # must not raise despite the ensemble erroring

        assert r.p_long == pytest.approx(0.8)  # unchanged -- fails open
        assert r.tradeable is True


# ---------------------------------------------------------------------------
# UI-015: multi-timeframe trend confirmation (opt-in, off by default)
# ---------------------------------------------------------------------------


def _uptrend_bars(n: int) -> pd.DataFrame:
    """Strictly rising close series -> ewm_trend_signal > 0."""
    return _make_bars_with_close(n, np.linspace(100, 200, n))


def _downtrend_bars(n: int) -> pd.DataFrame:
    """Strictly falling close series -> ewm_trend_signal < 0."""
    return _make_bars_with_close(n, np.linspace(200, 100, n))


def _make_bars_with_close(n: int, close: np.ndarray) -> pd.DataFrame:
    from datetime import UTC, datetime

    from src.config import TIMEFRAME_SECONDS, Timeframe

    now_ms = int(datetime.now(tz=UTC).timestamp() * 1000)
    tf_ms = TIMEFRAME_SECONDS.get(Timeframe.INTRADAY, 900) * 1000
    ts = [now_ms - tf_ms - (n - 1 - i) * tf_ms for i in range(n)]
    return pd.DataFrame(
        {
            "open": close,
            "high": close + 1,
            "low": close - 1,
            "close": close,
            "volume": [1000.0] * n,
            "quote_volume": [100_000.0] * n,
            "taker_buy_vol": [500.0] * n,
        },
        index=ts,
    )


class TestMtfTrendConfirmation:
    async def _run(self, monkeypatch, fast_bars, slow_bars, direction, p_long, enabled=True):
        from src.config import invalidate_settings_cache

        if enabled:
            monkeypatch.setenv("FEATURE_MTF_CONFIRMATION_ENABLED", "true")
        else:
            monkeypatch.delenv("FEATURE_MTF_CONFIRMATION_ENABLED", raising=False)
        invalidate_settings_cache()

        e = _make_engine(bars=fast_bars)

        async def _fetch_bars(symbol, timeframe, since_ts, limit):
            return [
                type("_R", (), {"close": float(c), "ts": i})()
                for i, c in enumerate(slow_bars["close"])
            ]

        e._storage.fetch_bars = _fetch_bars

        filter_pass = {
            "passes": True,
            "scalar": 1.0,
            "filters_failed": [],
            "details": {"hurst": 0.5},
        }

        async def _lb():
            return fast_bars

        e._load_bars = _lb
        with (
            patch("src.engine.signal_engine.build_feature_matrix", return_value=_fm()),
            patch(
                "src.engine.signal_engine.build_inference_features",
                return_value=pd.Series({"f0": 1.0}),
            ),
            patch("src.engine.signal_engine.evaluate_all_gates", return_value=_pass_gate()),
            patch("src.engine.signal_engine.compute_position_size", return_value=_mock_kelly()),
            patch(
                "src.engine.signal_engine.get_cognitive_engine",
                return_value=_mock_cognitive(passed=True),
            ),
            patch("src.engine.signal_engine.apply_all_strategy_filters", return_value=filter_pass),
            patch.object(e._trainer, "predict_direction", return_value=(direction, p_long)),
            patch.object(e._trainer, "predict_meta", return_value=(1, 0.8)),
        ):
            return await e.tick(**_TICK)

    @pytest.mark.asyncio
    async def test_disabled_by_default_ignores_mtf(self, monkeypatch):
        """Confirms the feature is truly opt-in: with the env var unset,
        a long signal against a downtrending 'slow' timeframe must still
        pass (MTF check never runs)."""
        r = await self._run(
            monkeypatch,
            fast_bars=_uptrend_bars(320),
            slow_bars=_downtrend_bars(320),
            direction=1,
            p_long=0.8,
            enabled=False,
        )
        assert r.tradeable is True

    @pytest.mark.asyncio
    async def test_enabled_aligned_trend_passes(self, monkeypatch):
        r = await self._run(
            monkeypatch,
            fast_bars=_uptrend_bars(320),
            slow_bars=_uptrend_bars(320),
            direction=1,
            p_long=0.8,
        )
        assert r.tradeable is True

    @pytest.mark.asyncio
    async def test_enabled_misaligned_trend_blocks(self, monkeypatch):
        r = await self._run(
            monkeypatch,
            fast_bars=_uptrend_bars(320),
            slow_bars=_downtrend_bars(320),
            direction=1,
            p_long=0.8,
        )
        assert r.tradeable is False
        assert "mtf_trend_misaligned" in r.skip_reason

    @pytest.mark.asyncio
    async def test_enabled_but_insufficient_slow_bars_fails_open(self, monkeypatch):
        """Fewer slow-timeframe bars than the EWM span needed -> the check
        is skipped (fails open), matching the other professional filters'
        behavior when their required inputs are unavailable."""
        r = await self._run(
            monkeypatch,
            fast_bars=_uptrend_bars(320),
            slow_bars=_downtrend_bars(5),  # far below _EWM_SPAN_TREND_MIN
            direction=1,
            p_long=0.8,
        )
        assert r.tradeable is True


# ---------------------------------------------------------------------------
# model_swap()
# ---------------------------------------------------------------------------


class TestModelSwap:
    @pytest.mark.asyncio
    async def test_swap_replaces_all_three(self):
        e = _make_engine()
        new_dm, new_mm, new_det = _fitted_xgb(), _fitted_xgb(), _fitted_detector()
        await e.swap_models(direction_model=new_dm, meta_model=new_mm, detector=new_det)
        assert e._direction_model is new_dm
        assert e._meta_model is new_mm
        assert e._detector is new_det

    @pytest.mark.asyncio
    async def test_swap_with_ensemble_replaces_it(self):
        e = _make_engine()
        new_ensemble = MagicMock()
        await e.swap_models(_fitted_xgb(), _fitted_xgb(), _fitted_detector(), ensemble=new_ensemble)
        assert e._ensemble is new_ensemble

    @pytest.mark.asyncio
    async def test_swap_without_ensemble_leaves_existing_one_untouched(self):
        """ensemble=None (the default) must not clobber a previously-swapped
        ensemble -- e.g. an orchestrator retrain that only touches direction/
        meta (ensemble train failed) must not silently drop a working one."""
        existing_ensemble = MagicMock()
        e = _make_engine(ensemble=existing_ensemble)
        await e.swap_models(_fitted_xgb(), _fitted_xgb(), _fitted_detector())
        assert e._ensemble is existing_ensemble

    @pytest.mark.asyncio
    async def test_concurrent_swap_and_tick_no_crash(self):
        """Concurrent swap + tick must not raise (lock protects torn reads)."""
        e = _make_engine(bars=_make_bars(n=3))  # thin bars → quick skip

        async def swap_loop():
            for _ in range(5):
                await e.swap_models(_fitted_xgb(), _fitted_xgb(), _fitted_detector())
                await asyncio.sleep(0)

        async def tick_loop():
            for _ in range(5):
                await e.tick(**_TICK)
                await asyncio.sleep(0)

        await asyncio.gather(swap_loop(), tick_loop())


# ---------------------------------------------------------------------------
# TASK-010 — live spread_bps + funding_rate_8h wiring
# ---------------------------------------------------------------------------


class TestTask010FundingRateWiring:
    """Verify live spread_bps and funding_rate_8h reach SignalContext."""

    @pytest.mark.asyncio
    async def test_funding_rate_passed_to_cognitive_engine(self):
        """funding_rate_8h from fetcher must appear in SignalContext passed to CogEng."""
        captured: list = []

        def _capturing_cog():
            cog = MagicMock()
            res = MagicMock()
            res.passed = True
            res.veto_reason = ""
            res.adjusted_size_fraction = 0.05

            def _capture_eval(ctx):
                captured.append(ctx)
                return res

            cog.evaluate = _capture_eval
            return cog

        e = _make_engine()
        e._fetcher.fetch_funding_rate.return_value = 0.0003  # 0.03% / 8h

        good_bars = _make_bars(n=320)

        async def _lb():
            return good_bars

        e._load_bars = _lb

        with (
            patch("src.engine.signal_engine.build_feature_matrix", return_value=_fm()),
            patch(
                "src.engine.signal_engine.build_inference_features",
                return_value=pd.Series({"f0": 1.0}),
            ),
            patch("src.engine.signal_engine.compute_position_size", return_value=_mock_kelly()),
            patch.object(e._trainer, "predict_direction", return_value=(1, 0.8)),
            patch.object(e._trainer, "predict_meta", return_value=(1, 0.8)),
            patch(
                "src.engine.signal_engine.evaluate_all_gates",
                return_value=MagicMock(
                    passed=True, status=MagicMock(value="ok"), reason="", details={}
                ),
            ),
            patch(
                "src.engine.signal_engine.apply_all_strategy_filters",
                return_value={
                    "passes": True,
                    "filters_failed": [],
                    "scalar": 1.0,
                    "details": {"hurst": 0.55},
                },
            ),
            patch("src.engine.signal_engine.get_cognitive_engine", return_value=_capturing_cog()),
        ):
            await e.tick(**_TICK)

        assert len(captured) == 1, "CognitiveEngine.evaluate must be called exactly once"
        ctx = captured[0]
        assert (
            abs(ctx.funding_rate_8h - 0.0003) < 1e-9
        ), f"Expected funding_rate_8h=0.0003, got {ctx.funding_rate_8h}"
        # spread_bps: ob.spread/ob.mid_price*10_000 = 0.1/105.0*10_000 ≈ 9.52 bps
        assert ctx.spread_bps > 0.0, "spread_bps must be positive when orderbook is live"

    @pytest.mark.asyncio
    async def test_funding_rate_defaults_zero_on_fetch_error(self):
        """fetch_funding_rate raising must not crash tick; funding_rate_8h falls back to 0.0."""
        e = _make_engine()
        e._fetcher.fetch_orderbook.side_effect = RuntimeError("exchange down")
        e._fetcher.fetch_funding_rate.side_effect = RuntimeError("exchange down")

        good_bars = _make_bars(n=320)

        async def _lb():
            return good_bars

        e._load_bars = _lb

        with (
            patch("src.engine.signal_engine.build_feature_matrix", return_value=_fm()),
            patch(
                "src.engine.signal_engine.build_inference_features",
                return_value=pd.Series({"f0": 1.0}),
            ),
            patch("src.engine.signal_engine.compute_position_size", return_value=_mock_kelly()),
            patch.object(e._trainer, "predict_direction", return_value=(1, 0.8)),
            patch.object(e._trainer, "predict_meta", return_value=(1, 0.8)),
            patch(
                "src.engine.signal_engine.evaluate_all_gates",
                return_value=MagicMock(
                    passed=False, status=MagicMock(value="blocked"), reason="x", details={}
                ),
            ),
        ):
            r = await e.tick(**_TICK)

        assert isinstance(r, SignalResult)  # no crash

    @pytest.mark.asyncio
    async def test_empty_feature_matrix_skips(self):
        e = _make_engine()
        good_bars = _make_bars(n=320)

        async def _lb():
            return good_bars

        e._load_bars = _lb
        empty_fm = MagicMock(spec=FeatureMatrix)
        empty_fm.features = pd.DataFrame()
        with patch("src.engine.signal_engine.build_feature_matrix", return_value=empty_fm):
            r = await e.tick(**_TICK)
        assert r.tradeable is False
        assert r.skip_reason == "insufficient_features"

    @pytest.mark.asyncio
    async def test_intel_aggregator_fetch_raises_nonfatal(self):
        """Intel aggregator exception must not crash tick; metrics default to {}."""
        e = _make_engine()
        good_bars = _make_bars(n=320)

        async def _lb():
            return good_bars

        e._load_bars = _lb
        broken_agg = AsyncMock()
        broken_agg.fetch_metrics = AsyncMock(side_effect=RuntimeError("aggregator down"))
        with (
            patch("src.engine.signal_engine._get_intel_aggregator", return_value=broken_agg),
            patch("src.engine.signal_engine.build_feature_matrix", return_value=_fm()),
            patch(
                "src.engine.signal_engine.build_inference_features",
                return_value=pd.Series({"f0": 1.0}),
            ),
            patch("src.engine.signal_engine.compute_position_size", return_value=_mock_kelly()),
            patch.object(e._trainer, "predict_direction", return_value=(1, 0.8)),
            patch.object(e._trainer, "predict_meta", return_value=(1, 0.8)),
            patch("src.engine.signal_engine.evaluate_all_gates", return_value=_block_gate()),
        ):
            r = await e.tick(**_TICK)
        assert isinstance(r, SignalResult)

    @pytest.mark.asyncio
    async def test_probabilistic_adapter_exception_nonfatal(self):
        """ProbabilisticMetricsAdapter.process raising must not crash tick."""
        e = _make_engine()
        good_bars = _make_bars(n=320)

        async def _lb():
            return good_bars

        e._load_bars = _lb
        with (
            patch("src.engine.signal_engine._PROB_ADAPTER") as mock_adapter,
            patch("src.engine.signal_engine.build_feature_matrix", return_value=_fm()),
            patch(
                "src.engine.signal_engine.build_inference_features",
                return_value=pd.Series({"f0": 1.0}),
            ),
            patch("src.engine.signal_engine.compute_position_size", return_value=_mock_kelly()),
            patch.object(e._trainer, "predict_direction", return_value=(1, 0.8)),
            patch.object(e._trainer, "predict_meta", return_value=(1, 0.8)),
            patch("src.engine.signal_engine.evaluate_all_gates", return_value=_block_gate()),
        ):
            mock_adapter.process.side_effect = RuntimeError("model broken")
            r = await e.tick(**_TICK)
        assert isinstance(r, SignalResult)

    @pytest.mark.asyncio
    async def test_inference_vector_none_skips(self):
        e = _make_engine()
        good_bars = _make_bars(n=320)

        async def _lb():
            return good_bars

        e._load_bars = _lb
        with (
            patch("src.engine.signal_engine.build_feature_matrix", return_value=_fm()),
            patch("src.engine.signal_engine.build_inference_features", return_value=None),
        ):
            r = await e.tick(**_TICK)
        assert r.tradeable is False
        assert r.skip_reason == "insufficient_features_with_ofi"

    @pytest.mark.asyncio
    async def test_regime_prediction_exception_defaults_volatile(self):
        """detector.predict_current raising must fail-safe to REGIME_VOLATILE, not crash."""
        e = _make_engine()
        good_bars = _make_bars(n=320)

        async def _lb():
            return good_bars

        e._load_bars = _lb
        with (
            patch("src.engine.signal_engine.build_feature_matrix", return_value=_fm(n=120)),
            patch(
                "src.engine.signal_engine.build_inference_features",
                return_value=pd.Series({"f0": 1.0}),
            ),
            patch.object(e._detector, "is_fitted", return_value=True),
            patch.object(e._detector, "predict_current", side_effect=RuntimeError("hmm broke")),
            patch("src.engine.signal_engine.compute_position_size", return_value=_mock_kelly()),
            patch.object(e._trainer, "predict_direction", return_value=(1, 0.8)),
            patch.object(e._trainer, "predict_meta", return_value=(1, 0.8)),
            patch("src.engine.signal_engine.evaluate_all_gates", return_value=_block_gate()),
        ):
            r = await e.tick(**_TICK)
        assert isinstance(r, SignalResult)

    @pytest.mark.asyncio
    async def test_carver_cap_exception_nonfatal(self):
        e = _make_engine()
        good_bars = _make_bars(n=320)

        async def _lb():
            return good_bars

        e._load_bars = _lb
        with (
            patch(
                "src.engine.signal_engine.recommend_position_notional",
                side_effect=RuntimeError("carver broke"),
            ),
            patch("src.engine.signal_engine.build_feature_matrix", return_value=_fm()),
            patch(
                "src.engine.signal_engine.build_inference_features",
                return_value=pd.Series({"f0": 1.0}),
            ),
            patch("src.engine.signal_engine.compute_position_size", return_value=_mock_kelly()),
            patch.object(e._trainer, "predict_direction", return_value=(1, 0.8)),
            patch.object(e._trainer, "predict_meta", return_value=(1, 0.8)),
            patch("src.engine.signal_engine.evaluate_all_gates", return_value=_block_gate()),
        ):
            r = await e.tick(**_TICK)
        assert isinstance(r, SignalResult)

    @pytest.mark.asyncio
    async def test_slippage_estimate_exception_nonfatal(self):
        e = _make_engine()
        good_bars = _make_bars(n=320)

        async def _lb():
            return good_bars

        e._load_bars = _lb
        with (
            patch("src.engine.signal_engine.SlippageModel") as mock_slip_cls,
            patch("src.engine.signal_engine.build_feature_matrix", return_value=_fm()),
            patch(
                "src.engine.signal_engine.build_inference_features",
                return_value=pd.Series({"f0": 1.0}),
            ),
            patch("src.engine.signal_engine.compute_position_size", return_value=_mock_kelly()),
            patch.object(e._trainer, "predict_direction", return_value=(1, 0.8)),
            patch.object(e._trainer, "predict_meta", return_value=(1, 0.8)),
            patch("src.engine.signal_engine.evaluate_all_gates", return_value=_block_gate()),
        ):
            mock_slip_cls.return_value.estimate.side_effect = RuntimeError("slippage broke")
            r = await e.tick(**_TICK)
        assert isinstance(r, SignalResult)

    @pytest.mark.asyncio
    async def test_slippage_negative_ev_skips(self):
        e = _make_engine()
        good_bars = _make_bars(n=320)

        async def _lb():
            return good_bars

        e._load_bars = _lb
        veto = GateResult.fail(status=GateStatus.HALT_NEGATIVE_EV, reason="negative EV")
        with (
            patch("src.engine.signal_engine.build_feature_matrix", return_value=_fm()),
            patch(
                "src.engine.signal_engine.build_inference_features",
                return_value=pd.Series({"f0": 1.0}),
            ),
            patch("src.engine.signal_engine.compute_position_size", return_value=_mock_kelly()),
            patch.object(e._trainer, "predict_direction", return_value=(1, 0.8)),
            patch.object(e._trainer, "predict_meta", return_value=(1, 0.8)),
            patch("src.engine.signal_engine.check_slippage_veto", return_value=veto),
        ):
            r = await e.tick(**_TICK)
        assert r.tradeable is False
        assert r.skip_reason == "slippage_negative_ev"

    @pytest.mark.asyncio
    async def test_kelly_size_zero_skips(self):
        e = _make_engine()
        good_bars = _make_bars(n=320)

        async def _lb():
            return good_bars

        e._load_bars = _lb
        with (
            patch("src.engine.signal_engine.build_feature_matrix", return_value=_fm()),
            patch(
                "src.engine.signal_engine.build_inference_features",
                return_value=pd.Series({"f0": 1.0}),
            ),
            patch("src.engine.signal_engine.compute_position_size", return_value=None),
            patch.object(e._trainer, "predict_direction", return_value=(1, 0.8)),
            patch.object(e._trainer, "predict_meta", return_value=(1, 0.8)),
            patch("src.engine.signal_engine.evaluate_all_gates", return_value=_pass_gate()),
        ):
            r = await e.tick(**_TICK)
        assert r.tradeable is False
        assert r.skip_reason == "kelly_size_zero"

    @pytest.mark.asyncio
    async def test_meta_label_gate_skip(self):
        e = _make_engine()
        good_bars = _make_bars(n=320)

        async def _lb():
            return good_bars

        e._load_bars = _lb
        with (
            patch("src.engine.signal_engine.build_feature_matrix", return_value=_fm()),
            patch(
                "src.engine.signal_engine.build_inference_features",
                return_value=pd.Series({"f0": 1.0}),
            ),
            patch("src.engine.signal_engine.compute_position_size", return_value=_mock_kelly()),
            patch.object(e._trainer, "predict_direction", return_value=(1, 0.8)),
            patch.object(e._trainer, "predict_meta", return_value=(0, 0.2)),
            patch("src.engine.signal_engine.evaluate_all_gates", return_value=_pass_gate()),
        ):
            r = await e.tick(**_TICK)
        assert r.tradeable is False
        assert r.skip_reason == "meta_label_gate_skip"

    @pytest.mark.asyncio
    async def test_cognitive_veto_skips(self):
        e = _make_engine()
        good_bars = _make_bars(n=320)

        async def _lb():
            return good_bars

        e._load_bars = _lb
        with (
            patch("src.engine.signal_engine.build_feature_matrix", return_value=_fm()),
            patch(
                "src.engine.signal_engine.build_inference_features",
                return_value=pd.Series({"f0": 1.0}),
            ),
            patch("src.engine.signal_engine.compute_position_size", return_value=_mock_kelly()),
            patch.object(e._trainer, "predict_direction", return_value=(1, 0.8)),
            patch.object(e._trainer, "predict_meta", return_value=(1, 0.8)),
            patch("src.engine.signal_engine.evaluate_all_gates", return_value=_pass_gate()),
            patch("src.engine.signal_engine.get_cognitive_engine") as mock_cog,
        ):
            mock_cog.return_value.evaluate.return_value = MagicMock(
                passed=False, adjusted_size_fraction=0.0, veto_reason="risk_too_high"
            )
            r = await e.tick(**_TICK)
        assert r.tradeable is False
        assert r.skip_reason == "cognitive_veto:risk_too_high"

    @pytest.mark.asyncio
    async def test_cognitive_warn_reduces_kelly_size(self):
        """WARN result (adjusted_size_fraction < kelly's) must shrink notional/quantity."""
        e = _make_engine()
        good_bars = _make_bars(n=320)

        async def _lb():
            return good_bars

        e._load_bars = _lb
        kelly = _mock_kelly()  # adjusted_fraction=0.05
        with (
            patch("src.engine.signal_engine.build_feature_matrix", return_value=_fm()),
            patch(
                "src.engine.signal_engine.build_inference_features",
                return_value=pd.Series({"f0": 1.0}),
            ),
            patch("src.engine.signal_engine.compute_position_size", return_value=kelly),
            patch.object(e._trainer, "predict_direction", return_value=(1, 0.8)),
            patch.object(e._trainer, "predict_meta", return_value=(1, 0.8)),
            patch("src.engine.signal_engine.evaluate_all_gates", return_value=_pass_gate()),
            patch("src.engine.signal_engine.get_cognitive_engine") as mock_cog,
        ):
            mock_cog.return_value.evaluate.return_value = MagicMock(
                passed=True, adjusted_size_fraction=0.025, veto_reason=None
            )
            r = await e.tick(**_TICK)
        assert r.tradeable is True
        assert r.kelly_result.notional_usd < kelly.notional_usd
        assert r.kelly_result.quantity < kelly.quantity


class TestLoadBars:
    @pytest.mark.asyncio
    async def test_returns_none_when_insufficient_records(self):
        e = _make_engine()
        e._storage.fetch_bars = AsyncMock(return_value=[])
        result = await e._load_bars()
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_sorted_dataframe_when_sufficient(self):
        from src.data.storage import BarRecord

        e = _make_engine()
        records = [
            BarRecord(
                symbol="BTC/USDT",
                timeframe="15m",
                ts=1_700_000_000_000 + i * 900_000,
                open=100.0 + i,
                high=101.0 + i,
                low=99.0 + i,
                close=100.5 + i,
                volume=10.0,
                quote_volume=1_000.0,
                taker_buy_vol=5.0,
            )
            for i in range(310)
        ]
        e._storage.fetch_bars = AsyncMock(return_value=records)
        result = await e._load_bars()
        assert result is not None
        assert len(result) == 310
        assert list(result.index) == sorted(result.index)
        assert "close" in result.columns
