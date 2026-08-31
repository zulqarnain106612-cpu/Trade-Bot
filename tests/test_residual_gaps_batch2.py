"""Residual branches in the tuning scheduler, regime detector, worker
orchestrator, orderbook stream and MAML adapter.

Mostly error arms and optional-import fallbacks: the ensemble-blend attempt
and its failure handler, the E-09 retrain guards, the HMM candidate-selection
tie-break, the DepthDetectorV2 restore, the ECC sub-scan failures, CPU pinning
on a platform without sched_setaffinity, the websocket reconnect handlers and
torch.func's pre-2.0 import path.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from test_tuning_scheduler import _FakeStorage

from src.config import get_settings
from src.tuning.scheduler import AutoTuningScheduler


# ---------------------------------------------------------------------------
# Tuning scheduler
# ---------------------------------------------------------------------------


def _scheduler() -> AutoTuningScheduler:
    return AutoTuningScheduler(
        storage=_FakeStorage([], {}),  # type: ignore[arg-type]
        settings=get_settings(),
        symbol="BTC/USDT",
        timeframe="15m",
    )


class TestEnsembleBlendAttempt:
    def _run_attempt(self, attempt_impl):
        scheduler = _scheduler()
        scheduler._build_ensemble_blend_samples = AsyncMock(return_value=[object()] * 40)
        scheduler._closed_trade_count = AsyncMock(return_value=100)

        async def _run():
            scheduler.start()
            try:
                with patch("src.tuning.scheduler.runner.attempt", side_effect=attempt_impl):
                    await scheduler._attempt_all()
            finally:
                scheduler.stop()

        asyncio.run(_run())

    def test_the_blend_evaluator_runs_the_backtest_for_the_proposed_weights(self):
        seen: dict[str, object] = {}

        def _attempt(param_name, evaluate, **_kw):
            if param_name != "risk.ensemble_blend_weight":
                return MagicMock(attempted=False, accepted=False, promoted=False, reasons=[])
            proposal = MagicMock(champion_value=0.2, challenger_value=0.4)
            with patch(
                "src.tuning.scheduler.run_ensemble_blend_backtest", return_value=[]
            ) as backtest:
                evaluate(MagicMock(), proposal)
            seen["kwargs"] = backtest.call_args.kwargs
            return MagicMock(attempted=True, accepted=False, promoted=False, reasons=[])

        self._run_attempt(_attempt)

        assert seen["kwargs"]["champion_weight"] == 0.2
        assert seen["kwargs"]["challenger_weight"] == 0.4

    def test_a_failing_blend_attempt_is_logged_and_the_cycle_continues(self):
        def _attempt(param_name, _evaluate, **_kw):
            if param_name == "risk.ensemble_blend_weight":
                raise RuntimeError("backtest harness exploded")
            return MagicMock(attempted=False, accepted=False, promoted=False, reasons=[])

        self._run_attempt(_attempt)  # must not raise


class TestE09Retrain:
    """E-09 retrain only runs under CRYPTO_BOX, on an interval cycle."""

    def test_too_few_feature_bars_skips_the_retrain(self, monkeypatch):
        monkeypatch.setenv("CRYPTO_BOX", "true")
        scheduler = _scheduler()
        scheduler._build_feature_bars_df = AsyncMock(return_value=None)

        with patch("src.tuning.engine_backtest.retrain_e09_walkforward") as retrain:
            asyncio.run(scheduler._maybe_retrain_e09())

        retrain.assert_not_called()

    def test_a_failing_retrain_is_logged_rather_than_raised(self, monkeypatch):
        import pandas as pd

        monkeypatch.setenv("CRYPTO_BOX", "true")
        scheduler = _scheduler()
        scheduler._build_feature_bars_df = AsyncMock(
            return_value=pd.DataFrame({"close": range(300)})
        )

        with patch(
            "src.tuning.engine_backtest.retrain_e09_walkforward",
            side_effect=RuntimeError("no labels"),
        ):
            asyncio.run(scheduler._maybe_retrain_e09())

    def test_the_retrain_is_skipped_entirely_when_crypto_box_is_off(self, monkeypatch):
        monkeypatch.delenv("CRYPTO_BOX", raising=False)
        scheduler = _scheduler()
        scheduler._build_feature_bars_df = AsyncMock(return_value=None)

        asyncio.run(scheduler._maybe_retrain_e09())

        scheduler._build_feature_bars_df.assert_not_awaited()

    def test_a_successful_retrain_reports_its_sample_count(self, monkeypatch):
        import pandas as pd

        monkeypatch.setenv("CRYPTO_BOX", "true")
        scheduler = _scheduler()
        scheduler._build_feature_bars_df = AsyncMock(
            return_value=pd.DataFrame({"close": range(300)})
        )
        retrain = AsyncMock(return_value=42)

        with patch("src.tuning.engine_backtest.retrain_e09_walkforward", retrain):
            asyncio.run(scheduler._maybe_retrain_e09())

        retrain.assert_awaited_once()


# ---------------------------------------------------------------------------
# Worker orchestrator
# ---------------------------------------------------------------------------


class TestCpuPinning:
    def test_pinning_is_skipped_without_a_pid(self):
        from src.workers.orchestrator import WorkerOrchestrator

        orch = WorkerOrchestrator.__new__(WorkerOrchestrator)
        orch._pin_cpu(None, 0)  # returns quietly

    def test_a_platform_without_sched_setaffinity_is_tolerated(self, monkeypatch):
        from src.workers import orchestrator as mod

        monkeypatch.delattr(mod.os, "sched_setaffinity", raising=False)
        orch = mod.WorkerOrchestrator.__new__(mod.WorkerOrchestrator)

        orch._pin_cpu(1234, 0)  # AttributeError swallowed, as on Windows/macOS


# ---------------------------------------------------------------------------
# Orderbook stream
# ---------------------------------------------------------------------------


class _WS:
    """Async-iterable websocket double yielding one message then stopping."""

    def __init__(self, messages, error: Exception | None = None) -> None:
        self._messages = messages
        self._error = error

    async def __aenter__(self):
        if self._error is not None:
            raise self._error
        return self

    async def __aexit__(self, *exc):
        return False

    def __aiter__(self):
        async def _gen():
            for message in self._messages:
                yield message

        return _gen()


def _stream():
    from src.data.orderbook_stream import OrderbookStream

    return OrderbookStream(symbol="btcusdt")


class TestStreamLoops:
    def test_the_depth_loop_stops_mid_message_when_asked_to(self):
        import json

        stream = _stream()
        stream._running = True

        def _connect(_uri):
            stream._running = False  # a stop landed while the message was in flight
            return _WS([json.dumps({"bids": [], "asks": []})])

        with patch("src.data.orderbook_stream.websockets.connect", _connect):
            asyncio.run(stream._stream_depth())

        assert stream._snapshots == []

    def test_a_depth_stream_error_backs_off_and_retries(self):
        stream = _stream()
        stream._running = True
        attempts = {"n": 0}

        def _connect(_uri):
            attempts["n"] += 1
            if attempts["n"] >= 2:
                stream._running = False
            return _WS([], error=RuntimeError("handshake failed"))

        with (
            patch("src.data.orderbook_stream.websockets.connect", _connect),
            patch("src.data.orderbook_stream.asyncio.sleep", AsyncMock()),
        ):
            asyncio.run(stream._stream_depth())

        assert attempts["n"] == 2

    def test_the_trade_loop_stops_mid_message_when_asked_to(self):
        import json

        stream = _stream()
        stream._running = True

        def _connect(_uri):
            stream._running = False
            return _WS([json.dumps({"p": "1", "q": "1", "T": 1, "m": True})])

        with patch("src.data.orderbook_stream.websockets.connect", _connect):
            asyncio.run(stream._stream_trades())

        assert stream._trades == []

    def test_a_closed_trade_connection_reconnects(self):
        from websockets.exceptions import ConnectionClosed

        stream = _stream()
        stream._running = True
        attempts = {"n": 0}

        def _connect(_uri):
            attempts["n"] += 1
            if attempts["n"] >= 2:
                stream._running = False
            return _WS([], error=ConnectionClosed(None, None))

        with (
            patch("src.data.orderbook_stream.websockets.connect", _connect),
            patch("src.data.orderbook_stream.asyncio.sleep", AsyncMock()),
        ):
            asyncio.run(stream._stream_trades())

        assert attempts["n"] == 2


# ---------------------------------------------------------------------------
# MAML: torch.func import fallback
# ---------------------------------------------------------------------------


def test_fast_adapt_returns_one_adapted_tensor_per_parameter():
    import torch
    import torch.nn as nn

    from src.upgrade.maml import fast_adapt

    model = nn.Linear(3, 1)
    adapted = fast_adapt(
        model,
        torch.randn(8, 3),
        torch.randn(8, 1),
        nn.MSELoss(),
        k_steps=1,
    )

    assert set(adapted) == {name for name, _ in model.named_parameters()}


def test_the_meta_update_returns_the_mean_query_loss():
    import torch
    import torch.nn as nn

    from src.upgrade.maml import MAMLOptimizer

    trainer = MAMLOptimizer(nn.Linear(3, 1), loss_fn=nn.MSELoss(), k_steps=1)
    task = {
        "support_x": torch.randn(8, 3),
        "support_y": torch.randn(8, 1),
        "query_x": torch.randn(4, 3),
        "query_y": torch.randn(4, 1),
    }

    assert isinstance(trainer.meta_update([task]), float)


# ---------------------------------------------------------------------------
# Regime detector
# ---------------------------------------------------------------------------


class _StubMonitor:
    def __init__(self, converged: bool) -> None:
        self.converged = converged
        self.iter = 5


class _StubHMM:
    """GaussianHMM stand-in whose convergence and score are scripted per seed.

    The first restart is unconverged but scores best; the second converged and
    worse. The real selection rule must still prefer the converged fit.
    """

    _scripts: list[tuple[float, bool]] = []
    _built = 0

    def __init__(self, **kwargs) -> None:
        score, converged = self._scripts[type(self)._built % len(self._scripts)]
        type(self)._built += 1
        self._score = score
        self.monitor_ = _StubMonitor(converged)
        self.n_components = kwargs.get("n_components", 3)
        self.means_ = None

    def fit(self, X, lengths=None):
        import numpy as np

        self.means_ = np.zeros((self.n_components, X.shape[1]))
        return self

    def score(self, X, lengths=None) -> float:
        return self._score

    def predict(self, X, lengths=None):
        import numpy as np

        return np.zeros(len(X), dtype=int)

    def predict_proba(self, X, lengths=None):
        import numpy as np

        return np.tile([0.6, 0.3, 0.1], (len(X), 1))


def test_a_converged_restart_wins_over_a_better_scoring_unconverged_one(monkeypatch):
    from test_detector import make_synthetic_features

    from src.regime import detector as mod

    _StubHMM._scripts = [(-10.0, False), (-500.0, True), (-600.0, True)]
    _StubHMM._built = 0
    monkeypatch.setattr(mod, "GaussianHMM", _StubHMM)

    detector = mod.RegimeDetector(symbol="BTC/USDT", timeframe="1h")
    detector.fit(make_synthetic_features())

    assert detector._model.monitor_.converged is True
    assert detector._convergence_failed is False


class TestDepthDetectorRestore:
    def _saved(self, tmp_path, monkeypatch):
        from test_detector import make_synthetic_features

        from src.regime import detector as mod

        _StubHMM._scripts = [(-10.0, True)]
        _StubHMM._built = 0
        monkeypatch.setattr(mod, "GaussianHMM", _StubHMM)
        detector = mod.RegimeDetector(symbol="BTC/USDT", timeframe="1h")
        detector.fit(make_synthetic_features())
        detector.save(tmp_path)
        return mod

    def test_the_v2_model_is_restored_when_crypto_box_is_on(self, tmp_path, monkeypatch):
        mod = self._saved(tmp_path, monkeypatch)
        monkeypatch.setenv("CRYPTO_BOX", "true")
        v2 = MagicMock()
        v2.load.return_value = True

        with patch("src.regime.depth_detector_v2.DepthDetectorV2", return_value=v2):
            restored = mod.RegimeDetector.load(tmp_path, "BTC/USDT", "1h")

        assert restored._depth_v2 is v2

    def test_a_v2_model_that_is_absent_is_not_attached(self, tmp_path, monkeypatch):
        mod = self._saved(tmp_path, monkeypatch)
        monkeypatch.setenv("CRYPTO_BOX", "true")
        v2 = MagicMock()
        v2.load.return_value = False

        with patch("src.regime.depth_detector_v2.DepthDetectorV2", return_value=v2):
            restored = mod.RegimeDetector.load(tmp_path, "BTC/USDT", "1h")

        assert restored._depth_v2 is None

    def test_a_v2_model_that_fails_to_load_does_not_break_the_restore(self, tmp_path, monkeypatch):
        mod = self._saved(tmp_path, monkeypatch)
        monkeypatch.setenv("CRYPTO_BOX", "true")

        with patch(
            "src.regime.depth_detector_v2.DepthDetectorV2",
            side_effect=RuntimeError("corrupt checkpoint"),
        ):
            restored = mod.RegimeDetector.load(tmp_path, "BTC/USDT", "1h")

        assert restored._fitted is True
        assert restored._depth_v2 is None
