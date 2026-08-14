"""Tests for the v7 macro regime classifier."""

from __future__ import annotations

from src.intelligence.macro_regime import MacroIndicators, MacroRegime, classify_macro_regime


def test_strong_risk_on_indicators_classify_risk_on() -> None:
    indicators = MacroIndicators(
        funding_rate_zscore_avg=-1.0,
        stablecoin_supply_growth_pct=10.0,
        net_exchange_inflow_zscore=-1.0,
    )
    result = classify_macro_regime(indicators)
    assert result.regime == MacroRegime.RISK_ON
    assert result.risk_appetite > 0.2


def test_strong_risk_off_indicators_classify_risk_off() -> None:
    indicators = MacroIndicators(
        funding_rate_zscore_avg=3.0,
        stablecoin_supply_growth_pct=-10.0,
        net_exchange_inflow_zscore=3.0,
    )
    result = classify_macro_regime(indicators)
    assert result.regime == MacroRegime.RISK_OFF
    assert result.risk_appetite < -0.2


def test_neutral_indicators_classify_neutral() -> None:
    indicators = MacroIndicators(
        funding_rate_zscore_avg=0.0,
        stablecoin_supply_growth_pct=0.0,
        net_exchange_inflow_zscore=0.0,
    )
    result = classify_macro_regime(indicators)
    assert result.regime == MacroRegime.NEUTRAL


def test_risk_appetite_bounded() -> None:
    indicators = MacroIndicators(
        funding_rate_zscore_avg=100.0,
        stablecoin_supply_growth_pct=-100.0,
        net_exchange_inflow_zscore=100.0,
    )
    result = classify_macro_regime(indicators)
    assert -1.0 <= result.risk_appetite <= 1.0
