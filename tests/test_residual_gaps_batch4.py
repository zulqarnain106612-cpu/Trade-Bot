"""One-line residual branches across small modules.

Each of these is a guard or fallback that the existing suites walk past: a
window too short to score, a value that cannot be normalised, an optional
dependency, a rejected configuration.
"""

from __future__ import annotations

import asyncio
import sys
import types
from unittest.mock import MagicMock

import numpy as np
import pandas as pd
import pytest

# ---------------------------------------------------------------------------
# Intelligence metrics / macro indicators
# ---------------------------------------------------------------------------


def test_a_z_score_needs_two_observations():
    from src.intelligence.metrics import IntelligenceAnalyzer

    analyzer = IntelligenceAnalyzer()
    analyzer.historical_data = pd.DataFrame({"funding_rate": [0.01]})

    assert analyzer._compute_zscore(0.02, "funding_rate") == 0.0


def test_a_non_finite_growth_is_reported_as_unavailable():
    from src.intelligence.macro_indicators import _window_growth_pct

    features = pd.DataFrame({"m2": [5e-324] * 3 + [1e308] * 3})

    growth, ok = _window_growth_pct(features, "m2", min_observations=2)

    assert (growth, ok) == (0.0, False)


# ---------------------------------------------------------------------------
# Order throttler
# ---------------------------------------------------------------------------


def test_changing_the_rate_rebuilds_the_existing_buckets():
    from src.execution.order_throttler import OrderThrottler

    throttler = OrderThrottler(rate=1.0, burst=1)
    throttler.acquire("binance")  # create a bucket to rebuild

    throttler.set_rate(5.0, burst=10)

    assert throttler.rate == 5.0
    assert throttler.tokens_remaining("binance") == pytest.approx(10.0)


# ---------------------------------------------------------------------------
# Strategy portfolio helper
# ---------------------------------------------------------------------------


def test_a_single_observation_has_no_standard_deviation():
    from src.engine.strategy_portfolio import _stdev

    assert _stdev([1.0], mu=1.0) == 0.0


# ---------------------------------------------------------------------------
# ECC helpers
# ---------------------------------------------------------------------------


def test_a_worthless_utxo_set_has_no_hodler_signal():
    from src.ecc.utxo_curve import compute_hodler_index

    result = compute_hodler_index([{"value_btc": 0.0, "age_days": 400.0}])

    assert result.hodler_index == 0.0
    assert result.supply_shock_risk is False


def test_privacy_routing_is_zero_without_any_cosigner_clusters():
    from src.ecc.schnorr_taproot import estimate_privacy_routing

    assert estimate_privacy_routing([]) == 0.0


# ---------------------------------------------------------------------------
# Storage health check allowlist
# ---------------------------------------------------------------------------


def test_health_check_refuses_a_table_name_it_cannot_vouch_for(tmp_path, monkeypatch):
    from src.data import storage as mod

    monkeypatch.setattr(mod, "_ALLOWED_TABLES", ("bars; DROP TABLE trades",))

    async def _run():
        backend = mod.StorageBackend(db_path=str(tmp_path / "t.db"))
        await backend.initialize()
        try:
            with pytest.raises(RuntimeError, match="unsafe characters"):
                await backend.health_check()
        finally:
            await backend.close()

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# Sentiment provider: the vaderSentiment path
# ---------------------------------------------------------------------------


def test_the_vader_analyzer_is_used_when_it_is_installed(monkeypatch):
    from src.data import sentiment_provider as mod

    analyzer = object()
    module = types.ModuleType("vaderSentiment.vaderSentiment")
    module.SentimentIntensityAnalyzer = lambda: analyzer
    package = types.ModuleType("vaderSentiment")
    package.vaderSentiment = module
    monkeypatch.setitem(sys.modules, "vaderSentiment", package)
    monkeypatch.setitem(sys.modules, "vaderSentiment.vaderSentiment", module)

    assert mod.SentimentProvider._get_vader() is analyzer


# ---------------------------------------------------------------------------
# Granger: a pair whose overlap is too short after alignment
# ---------------------------------------------------------------------------


def test_granger_skips_a_pair_left_too_short_after_dropping_nans():
    from src.causal.granger import GrangerCausalityDetector

    detector = GrangerCausalityDetector()
    btc = pd.Series(np.random.default_rng(0).normal(0, 0.01, 200))
    alt = [100.0] * 5 + [float("nan")] * 195

    assert "ETH/USDT" not in detector.update(btc, {"ETH/USDT": alt})


# ---------------------------------------------------------------------------
# Config: mean-reversion pair validation
# ---------------------------------------------------------------------------


def test_a_mean_reversion_pair_needs_exactly_two_symbols():
    from src.config import StrategyPortfolioSettings

    with pytest.raises(ValueError, match="exactly two symbols"):
        StrategyPortfolioSettings(mean_reversion_pair=["BTC/USDT"])


def test_a_mean_reversion_pair_needs_two_different_symbols():
    from src.config import StrategyPortfolioSettings

    with pytest.raises(ValueError, match="two different symbols"):
        StrategyPortfolioSettings(mean_reversion_pair=["BTC/USDT", "BTC/USDT"])


# ---------------------------------------------------------------------------
# Tuning audit log
# ---------------------------------------------------------------------------


def test_blank_lines_in_the_audit_log_are_skipped(tmp_path):
    from src.tuning.audit import TuningAuditLog, TuningEventType

    path = tmp_path / "audit.jsonl"
    log = TuningAuditLog(path)
    log.record(param_name="risk.x", event_type=next(iter(TuningEventType)))
    with path.open("a", encoding="utf-8") as fh:
        fh.write("\n\n")

    assert len(log.read_all()) == 1


# ---------------------------------------------------------------------------
# Position sizing / vol targeting
# ---------------------------------------------------------------------------


def test_an_instrument_with_no_measurable_risk_gets_no_position():
    from src.strategies.position_sizing import carver_forecast_position

    assert (
        carver_forecast_position(
            capital_usd=100_000.0, forecast=10.0, daily_vol_pct=0.0, price=50_000.0
        )
        == 0.0
    )


def test_a_notional_that_rounds_away_is_reported_as_zero():
    from src.risk.vol_target_sizer import vol_target_size

    result = vol_target_size(
        capital_usd=1.0,
        current_equity=1.0,
        hwm=1.0,
        realized_vol_pct=100_000.0,  # enormous vol -> a vanishing notional
        target_vol_pct=1e-9,
    )

    assert result.notional_usd == 0.0
    assert result.reject_reason


# ---------------------------------------------------------------------------
# Credential vault
# ---------------------------------------------------------------------------


def test_a_soft_child_index_is_forced_hardened():
    from src.security.credential_vault import _HARDENED_OFFSET, _derive_child

    soft = _derive_child(b"\x01" * 32, b"\x02" * 32, 0)
    hardened = _derive_child(b"\x01" * 32, b"\x02" * 32, _HARDENED_OFFSET)

    assert soft == hardened


# ---------------------------------------------------------------------------
# Universe returns backoff
# ---------------------------------------------------------------------------


def test_there_is_no_backoff_before_the_first_failure():
    from src.engine.universe_returns import UniverseReturnsCache

    cache = UniverseReturnsCache.__new__(UniverseReturnsCache)
    cache._consecutive_failures = 0

    assert cache._backoff_seconds() == 0.0


# ---------------------------------------------------------------------------
# Block-height provider
# ---------------------------------------------------------------------------


def test_a_failing_provider_cache_does_not_break_the_height_publish(monkeypatch):
    from src.data.block_height_provider import BlockHeightProvider

    provider = BlockHeightProvider()
    provider._height = 840_000
    monkeypatch.setattr(
        "src.data.provider_cache.get_provider_cache",
        lambda: (_ for _ in ()).throw(RuntimeError("cache down")),
    )

    provider._update_cache()


# ---------------------------------------------------------------------------
# zkSNARK detector: web3 construction failure
# ---------------------------------------------------------------------------


def test_a_web3_that_fails_to_construct_leaves_the_detector_disconnected(monkeypatch):
    from src.ecc.zksnark_detect import ZkSnarkDetector

    module = types.ModuleType("web3")
    module.Web3 = MagicMock(side_effect=RuntimeError("bad rpc url"))
    monkeypatch.setitem(sys.modules, "web3", module)

    detector = ZkSnarkDetector(eth_rpc_url="http://127.0.0.1:8545")

    assert detector._w3 is None


# ---------------------------------------------------------------------------
# Causal inference: a stratum whose effect is undefined
# ---------------------------------------------------------------------------


def test_a_constant_stratum_is_excluded_rather_than_averaged_in():
    from src.intelligence.causal_inference import CausalInferenceEngine

    rng = np.random.default_rng(3)
    n = 120
    treatment = np.concatenate([rng.normal(size=n // 2), np.ones(n // 2)])
    outcome = np.concatenate([rng.normal(size=n // 2), np.ones(n // 2)])
    regimes = np.array([0] * (n // 2) + [1] * (n // 2))

    result = CausalInferenceEngine().estimate_heterogeneous_treatment_effect(
        treatment, outcome, regimes
    )

    assert "1" not in result["effects_by_context"]
    assert "1" in result["excluded_strata"]
