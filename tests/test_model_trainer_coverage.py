"""
Coverage for src/models/trainer.py — Debt-005.

Targets predict_direction, predict_meta, TrainingResult, and
compute_win_loss_stats without running expensive training loops.
"""
from __future__ import annotations

from unittest.mock import patch

import numpy as np
import pandas as pd
import pytest
from xgboost import XGBClassifier

from src.models.trainer import ModelTrainer, TrainingResult
from src.risk.kelly import compute_win_loss_stats


# Exact column names from src/features/pipeline.py
FEATURES = [
    "frac_diff",
    "vwap_dev_zscore",
    "ofi",
    "realized_vol_ratio",
    "atr_momentum",
    "rolling_sharpe",
    "volume_zscore",
]
N = len(FEATURES)  # 7


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _trainer() -> ModelTrainer:
    return ModelTrainer(symbol="BTC/USDT", timeframe="15m")


def _fitted_dir(n_cols: int = N) -> XGBClassifier:
    rng = np.random.default_rng(42)
    X = rng.standard_normal((100, n_cols))
    y = (X[:, 0] > 0).astype(int)
    m = XGBClassifier(n_estimators=3, max_depth=2, verbosity=0)
    m.fit(X, y)
    return m


def _fitted_meta() -> XGBClassifier:
    """Meta expects N+2 features (p_long + confidence appended)."""
    rng = np.random.default_rng(7)
    X = rng.standard_normal((100, N + 2))
    y = (X[:, 0] > 0).astype(int)
    m = XGBClassifier(n_estimators=3, max_depth=2, verbosity=0)
    m.fit(X, y)
    return m


def _vec(seed: int = 0) -> pd.Series:
    return pd.Series(np.random.default_rng(seed).standard_normal(N), index=FEATURES)


# ---------------------------------------------------------------------------
# ModelTrainer init
# ---------------------------------------------------------------------------

class TestModelTrainerInit:
    def test_symbol_stored(self):
        t = ModelTrainer(symbol="ETH/USDT", timeframe="1h")
        assert t._symbol == "ETH/USDT"

    def test_timeframe_stored(self):
        t = ModelTrainer(symbol="BTC/USDT", timeframe="15m")
        assert t._timeframe == "15m"

    def test_default_xgb_cfg_loaded(self):
        assert _trainer()._xgb_cfg is not None

    def test_custom_xgb_cfg(self):
        from src.config import get_settings
        cfg = get_settings().xgboost
        t = ModelTrainer(symbol="BTC/USDT", timeframe="15m", xgb_cfg=cfg)
        assert t._xgb_cfg is cfg


# ---------------------------------------------------------------------------
# predict_direction
# ---------------------------------------------------------------------------

class TestPredictDirection:
    def test_returns_int_and_float(self):
        d, p = _trainer().predict_direction(_fitted_dir(), _vec())
        assert isinstance(d, int)
        assert isinstance(p, float)

    def test_direction_1_when_p_long_high(self):
        dm = _fitted_dir()
        with patch.object(dm, "predict_proba", return_value=np.array([[0.1, 0.9]])):
            d, p = _trainer().predict_direction(dm, _vec())
        assert d == 1
        assert p == pytest.approx(0.9)

    def test_direction_0_when_p_long_low(self):
        dm = _fitted_dir()
        with patch.object(dm, "predict_proba", return_value=np.array([[0.8, 0.2]])):
            d, p = _trainer().predict_direction(dm, _vec())
        assert d == 0
        assert p == pytest.approx(0.2)

    def test_p_long_in_unit_interval(self):
        dm = _fitted_dir()
        for seed in range(8):
            _, p = _trainer().predict_direction(dm, _vec(seed))
            assert 0.0 <= p <= 1.0

    def test_extra_columns_ignored(self):
        dm = _fitted_dir()
        vec = _vec()
        vec["irrelevant_col"] = 999.0
        d, _ = _trainer().predict_direction(dm, vec)
        assert d in (0, 1)

    def test_boundary_p_long_half_is_long(self):
        dm = _fitted_dir()
        with patch.object(dm, "predict_proba", return_value=np.array([[0.5, 0.5]])):
            d, p = _trainer().predict_direction(dm, _vec())
        assert d == 1  # >= 0.5 → long


# ---------------------------------------------------------------------------
# predict_meta
# ---------------------------------------------------------------------------

class TestPredictMeta:
    def test_returns_int_and_float(self):
        meta, p = _trainer().predict_meta(_fitted_meta(), _vec(), p_long=0.7)
        assert isinstance(meta, int)
        assert meta in (0, 1)
        assert 0.0 <= p <= 1.0

    def test_meta_1_when_p_bet_high(self):
        mm = _fitted_meta()
        with patch.object(mm, "predict_proba", return_value=np.array([[0.2, 0.8]])):
            meta, p = _trainer().predict_meta(mm, _vec(), p_long=0.7)
        assert meta == 1
        assert p == pytest.approx(0.8)

    def test_meta_0_when_p_bet_low(self):
        mm = _fitted_meta()
        with patch.object(mm, "predict_proba", return_value=np.array([[0.9, 0.1]])):
            meta, p = _trainer().predict_meta(mm, _vec(), p_long=0.3)
        assert meta == 0

    def test_shape_mismatch_raises(self):
        rng = np.random.default_rng(0)
        bad = XGBClassifier(n_estimators=1, verbosity=0)
        bad.fit(rng.standard_normal((50, 3)), [0, 1] * 25)
        with pytest.raises(ValueError, match="feature schema"):
            _trainer().predict_meta(bad, _vec(), p_long=0.5)

    def test_input_shape_is_n_plus_2(self):
        """Verifies [p_long, confidence] are appended to the feature vec."""
        mm = _fitted_meta()
        captured = {}

        def spy(X):
            captured["shape"] = X.shape
            return mm.predict_proba.__wrapped__(X) if hasattr(mm.predict_proba, "__wrapped__") \
                else np.array([[0.4, 0.6]])

        with patch.object(mm, "predict_proba", side_effect=spy):
            _trainer().predict_meta(mm, _vec(), p_long=0.8)

        assert captured.get("shape") == (1, N + 2)

    def test_different_p_long_values_accepted(self):
        mm = _fitted_meta()
        for p in [0.0, 0.25, 0.5, 0.75, 1.0]:
            meta, p_bet = _trainer().predict_meta(mm, _vec(), p_long=p)
            assert meta in (0, 1)
            assert 0.0 <= p_bet <= 1.0


# ---------------------------------------------------------------------------
# compute_win_loss_stats (returns win_prob, avg_win, avg_loss)
# ---------------------------------------------------------------------------

class TestComputeWinLossStats:
    def test_fewer_than_50_returns_defaults(self):
        win_prob, avg_win, avg_loss = compute_win_loss_stats([10.0, -5.0])
        assert win_prob == pytest.approx(0.5)
        assert avg_win == pytest.approx(1.0)
        assert avg_loss == pytest.approx(1.0)

    def test_returns_three_values(self):
        result = compute_win_loss_stats([1.0] * 25 + [-1.0] * 25)
        assert len(result) == 3

    def test_empty_list_returns_defaults(self):
        wp, aw, al = compute_win_loss_stats([])
        assert wp == pytest.approx(0.5)

    def test_all_wins_returns_defaults(self):
        # No losses → safe default
        wp, aw, al = compute_win_loss_stats([10.0] * 60)
        assert wp == pytest.approx(0.5)

    def test_balanced_pnl_correct_stats(self):
        wins = [10.0] * 30
        losses = [-5.0] * 30
        wp, aw, al = compute_win_loss_stats(wins + losses)
        assert wp == pytest.approx(0.5)
        assert aw == pytest.approx(10.0)
        assert al == pytest.approx(5.0)

    def test_skewed_win_rate(self):
        wins = [10.0] * 70
        losses = [-5.0] * 30
        wp, aw, al = compute_win_loss_stats(wins + losses)
        assert wp == pytest.approx(0.7)
        assert aw == pytest.approx(10.0)
        assert al == pytest.approx(5.0)


# ---------------------------------------------------------------------------
# TrainingResult dataclass
# ---------------------------------------------------------------------------

class TestTrainingResult:
    def _result(self, sharpe: float = 1.2, live_gate: bool = True) -> TrainingResult:
        return TrainingResult(
            model=_fitted_dir(),
            oos_sharpe=sharpe,
            max_drawdown=5.0,
            n_trades=100,
            accuracy=0.55,
            precision=0.56,
            recall=0.54,
            f1=0.55,
            live_gate_pass=live_gate,
        )

    def test_live_gate_pass_stored(self):
        assert self._result(live_gate=True).live_gate_pass is True
        assert self._result(live_gate=False).live_gate_pass is False

    def test_oos_sharpe_stored(self):
        assert self._result(sharpe=1.5).oos_sharpe == pytest.approx(1.5)

    def test_fold_metrics_default_empty(self):
        assert self._result().fold_metrics == []

    def test_elapsed_s_default_zero(self):
        assert self._result().elapsed_s == pytest.approx(0.0)

    def test_to_metrics_record(self):
        rec = self._result().to_metrics_record(
            model_name="direction", timeframe="15m", version="v1"
        )
        assert rec.model_name == "direction"
        assert rec.oos_sharpe == pytest.approx(1.2)
        assert rec.live_gate_pass is True
