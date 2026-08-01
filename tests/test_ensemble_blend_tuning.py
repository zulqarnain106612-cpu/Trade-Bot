"""
Wiring tests for the risk.ensemble_blend_weight tuning loop.

The parameter was registered but had no evaluate_fn, so the scheduler listed
it as tunable and could never move it. Closing that needed the two blend
inputs on the trade record — the blended result alone is not invertible,
because the online-trainer blend perturbs it again straight afterwards.

These cover the two ends that were missing: that the engine records both
inputs (and records neither when no blend happened), and that the harness
turns them into a champion-vs-challenger comparison.
"""

from __future__ import annotations

import pytest

from src.data.storage import BlendAudit, TradeRecord
from src.tuning.backtest_harness import (
    EnsembleBlendSample,
    _blended_p_long,
    ensemble_blend_samples_from_trades,
    run_ensemble_blend_backtest,
)


def _trade(
    trade_id: str = "t1",
    *,
    entry_price: float = 100.0,
    exit_price: float | None = 110.0,
    direction: int = 1,
    pre_blend_p_long: float | None = 0.6,
    ensemble_p_long: float | None = 0.8,
    ensemble_blend_weight: float | None = 0.15,
) -> TradeRecord:
    return TradeRecord(
        id=trade_id,
        symbol="BTC/USDT",
        timeframe="15m",
        trading_mode="paper",
        execution_mode="automatic",
        direction=direction,
        entry_price=entry_price,
        exit_price=exit_price,
        quantity=1.0,
        notional_usd=entry_price,
        entry_ts=1_700_000_000_000,
        exit_ts=1_700_000_900_000,
        pnl_usd=None,
        pnl_pct=None,
        fee_usd=0.0,
        kelly_fraction=0.1,
        regime_at_entry=0,
        meta_label_prob=0.6,
        exit_reason="time_exit",
        approved_by="auto",
        raw_signal=0.63,
        pre_blend_p_long=pre_blend_p_long,
        ensemble_p_long=ensemble_p_long,
        ensemble_blend_weight=ensemble_blend_weight,
    )


# ---------------------------------------------------------------------------
# TradeRecord / BlendAudit
# ---------------------------------------------------------------------------


class TestTradeRecordBlendFields:
    def test_blend_fields_default_to_none(self) -> None:
        """Every caller that predates the blend audit keeps working."""
        record = _trade(pre_blend_p_long=None, ensemble_p_long=None, ensemble_blend_weight=None)
        assert record.pre_blend_p_long is None
        assert record.ensemble_p_long is None
        assert record.ensemble_blend_weight is None

    def test_blend_audit_carries_the_three_values_together(self) -> None:
        audit = BlendAudit(pre_blend_p_long=0.6, ensemble_p_long=0.8, blend_weight=0.15)
        assert audit.pre_blend_p_long == 0.6
        assert audit.ensemble_p_long == 0.8
        assert audit.blend_weight == 0.15

    def test_blend_audit_is_immutable(self) -> None:
        """An audit record that can be edited after the fact is not an audit."""
        audit = BlendAudit(pre_blend_p_long=0.6, ensemble_p_long=0.8, blend_weight=0.15)
        with pytest.raises(AttributeError):
            audit.blend_weight = 0.9  # type: ignore[misc]


# ---------------------------------------------------------------------------
# ensemble_blend_samples_from_trades
# ---------------------------------------------------------------------------


class TestSampleBuilding:
    def test_closed_blended_trade_becomes_a_sample(self) -> None:
        samples = ensemble_blend_samples_from_trades([_trade()])
        assert len(samples) == 1
        assert samples[0].raw_p_long == 0.6
        assert samples[0].ensemble_p_long == 0.8

    def test_open_trade_is_skipped(self) -> None:
        """No exit price means no realized return to score against."""
        assert ensemble_blend_samples_from_trades([_trade(exit_price=None)]) == []

    def test_trade_without_blend_columns_is_skipped(self) -> None:
        """Pre-v7 rows, and any trade taken with no ensemble available."""
        assert (
            ensemble_blend_samples_from_trades(
                [_trade(pre_blend_p_long=None, ensemble_p_long=None)]
            )
            == []
        )

    def test_zero_weight_trade_is_still_a_sample(self) -> None:
        """
        A blend at weight 0.0 happened and is re-scorable; NULL did not.
        Conflating the two would silently discard the samples that matter
        most when the tuner is pushing the weight toward its floor.
        """
        samples = ensemble_blend_samples_from_trades([_trade(ensemble_blend_weight=0.0)])
        assert len(samples) == 1

    def test_long_return_is_signed_to_the_direction_traded(self) -> None:
        samples = ensemble_blend_samples_from_trades(
            [_trade(direction=1, entry_price=100.0, exit_price=110.0)]
        )
        assert samples[0].raw_return == pytest.approx(0.10)

    def test_winning_short_has_a_positive_return(self) -> None:
        samples = ensemble_blend_samples_from_trades(
            [_trade(direction=0, entry_price=100.0, exit_price=90.0)]
        )
        assert samples[0].raw_return == pytest.approx(0.10)

    def test_samples_come_back_oldest_first(self) -> None:
        """fetch_trades returns newest-first; the folds assume chronological."""
        newest = _trade("t2", exit_price=120.0)
        oldest = _trade("t1", exit_price=110.0)
        samples = ensemble_blend_samples_from_trades([newest, oldest])
        assert samples[0].raw_return < samples[1].raw_return


# ---------------------------------------------------------------------------
# run_ensemble_blend_backtest
# ---------------------------------------------------------------------------


def _samples(n: int, *, ensemble_is_right: bool) -> list[EnsembleBlendSample]:
    """
    n alternating-outcome samples where the ensemble either agrees with the
    realized outcome or contradicts it, and the XGBoost side is uninformative.

    Callers pass 800: the default CPCV split is 10 folds with a 60-bar purge
    gap, so anything at or below 600 makes _make_folds raise rather than
    exercise the harness.
    """
    out: list[EnsembleBlendSample] = []
    for i in range(n):
        won = i % 2 == 0
        ensemble_p = (0.95 if won else 0.05) if ensemble_is_right else (0.05 if won else 0.95)
        out.append(
            EnsembleBlendSample(
                raw_p_long=0.5,
                ensemble_p_long=ensemble_p,
                direction=1,
                raw_return=0.02 if won else -0.02,
            )
        )
    return out


class TestBlendBacktest:
    def test_blend_weight_arithmetic(self) -> None:
        sample = EnsembleBlendSample(
            raw_p_long=0.4, ensemble_p_long=0.9, direction=1, raw_return=0.01
        )
        assert _blended_p_long(sample, 0.0) == pytest.approx(0.4)
        assert _blended_p_long(sample, 1.0) == pytest.approx(0.9)
        assert _blended_p_long(sample, 0.5) == pytest.approx(0.65)

    def test_reports_both_metrics(self) -> None:
        comparisons = run_ensemble_blend_backtest(
            _samples(800, ensemble_is_right=True), champion_weight=0.15, challenger_weight=0.5
        )
        assert {c.metric_name for c in comparisons} == {"ensemble_calibration", "oos_sharpe"}

    def test_a_skilful_ensemble_favours_the_heavier_weight(self) -> None:
        comparisons = run_ensemble_blend_backtest(
            _samples(800, ensemble_is_right=True), champion_weight=0.05, challenger_weight=0.9
        )
        calibration = next(c for c in comparisons if c.metric_name == "ensemble_calibration")
        assert calibration.challenger_mean > calibration.champion_mean

    def test_a_misleading_ensemble_favours_the_lighter_weight(self) -> None:
        """
        The direction that matters: leaning on an ensemble that is wrong must
        score worse, or the tuner would ratchet the weight up on noise.
        """
        comparisons = run_ensemble_blend_backtest(
            _samples(800, ensemble_is_right=False), champion_weight=0.05, challenger_weight=0.9
        )
        calibration = next(c for c in comparisons if c.metric_name == "ensemble_calibration")
        assert calibration.challenger_mean < calibration.champion_mean

    def test_identical_weights_compare_equal(self) -> None:
        comparisons = run_ensemble_blend_backtest(
            _samples(800, ensemble_is_right=True), champion_weight=0.3, challenger_weight=0.3
        )
        for comparison in comparisons:
            assert comparison.champion_mean == pytest.approx(comparison.challenger_mean)
