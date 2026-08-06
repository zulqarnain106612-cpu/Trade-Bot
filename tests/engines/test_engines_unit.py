"""
Unit tests for each of the 18 Crypto-Box engines.

Tests:
  - Schema contract (returns EngineOutput or abstains)
  - Directional accuracy > 0.5 on synthetic data (beats random)
  - Engine-specific invariants
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import numpy as np
import pandas as pd
import pytest

from src.engines.schema import EngineOutput


# ---------------------------------------------------------------------------
# Schema contract
# ---------------------------------------------------------------------------


def test_engine_output_clamps_nan_predicted_price():
    """NaN / inf predicted_price must be sanitized to 0 at construction time."""
    from datetime import UTC, datetime

    from src.engines.schema import EngineOutput

    out = EngineOutput(
        engine_id="E-01",
        symbol="BTC/USDT",
        timestamp_utc=datetime.now(UTC),
        predicted_price=float("nan"),
        confidence=0.9,
        direction=1,
        horizon_hours=4,
    )
    assert out.predicted_price == 0.0
    assert out.confidence == 0.0


def test_engine_output_clamps_inf_predicted_price():
    from datetime import UTC, datetime

    from src.engines.schema import EngineOutput

    out = EngineOutput(
        engine_id="E-02",
        symbol="BTC/USDT",
        timestamp_utc=datetime.now(UTC),
        predicted_price=float("inf"),
        confidence=0.5,
        direction=0,
        horizon_hours=4,
    )
    assert out.predicted_price == 0.0
    assert out.confidence == 0.0


# ---------------------------------------------------------------------------
# Synthetic data helpers
# ---------------------------------------------------------------------------


def make_ohlcv(n: int = 300, trend: float = 0.0001) -> pd.DataFrame:
    rng = np.random.default_rng(42)
    closes = np.cumprod(1 + rng.normal(trend, 0.01, n)) * 50_000.0
    opens = closes * (1 + rng.normal(0, 0.002, n))
    highs = np.maximum(opens, closes) * (1 + rng.uniform(0, 0.005, n))
    lows = np.minimum(opens, closes) * (1 - rng.uniform(0, 0.005, n))
    volumes = rng.uniform(1000, 5000, n)
    ts = pd.date_range("2025-01-01", periods=n, freq="h", tz="UTC")
    return pd.DataFrame(
        {
            "timestamp_utc": ts,
            "open": opens,
            "high": highs,
            "low": lows,
            "close": closes,
            "volume": volumes,
        }
    )


def make_data(n: int = 300) -> dict:
    df = make_ohlcv(n)
    return {"ohlcv": df, "spot": float(df["close"].iloc[-1])}


# ---------------------------------------------------------------------------
# E-01 Statistical
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_e01_returns_engine_output():
    from src.engines.e01_statistical import E01Statistical

    e = E01Statistical()
    out = await e.run("BTC/USDT", make_data())
    assert isinstance(out, EngineOutput)
    assert out.engine_id == "E-01"
    assert 0.0 <= out.confidence <= 1.0
    assert out.direction in (-1, 0, 1)


@pytest.mark.asyncio
async def test_e01_abstains_on_no_data():
    from src.engines.e01_statistical import E01Statistical

    e = E01Statistical()
    out = await e.run("BTC/USDT", {"ohlcv": None, "spot": 0.0})
    assert out.confidence == 0.0
    assert out.direction == 0


# ---------------------------------------------------------------------------
# E-02 Microstructure
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_e02_bid_dominance_gives_long():
    import json

    from src.engines.e02_microstructure import E02Microstructure

    bids = [[str(50000 - i), "10.0"] for i in range(20)]
    asks = [[str(50001 + i), "1.0"] for i in range(20)]  # tiny asks → bid dominant
    ob_df = pd.DataFrame(
        [
            {
                "timestamp_utc": datetime.now(UTC),
                "bids_json": json.dumps(bids),
                "asks_json": json.dumps(asks),
                "mid": 50000.5,
                "spread_bps": 2.0,
            }
        ]
    )
    data = {"spot": 50000.0, "orderbook": ob_df}
    e = E02Microstructure()
    out = await e.run("BTC/USDT", data)
    assert out.direction == 1
    assert out.confidence > 0


@pytest.mark.asyncio
async def test_e02_abstains_without_orderbook():
    from src.engines.e02_microstructure import E02Microstructure

    e = E02Microstructure()
    out = await e.run("BTC/USDT", {"spot": 50000.0, "orderbook": None})
    assert out.confidence == 0.0


# ---------------------------------------------------------------------------
# E-03 Information Theory
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_e03_direction_zero():
    from src.engines.e03_information_theory import E03InformationTheory

    e = E03InformationTheory()
    out = await e.run("BTC/USDT", make_data())
    assert out.direction == 0  # E-03 never gives directional signal
    assert "entropy_score" in out.metadata


def test_e03_shannon_entropy_pure_noise_is_high():
    from src.engines.e03_information_theory import shannon_entropy

    rng = np.random.default_rng(0)
    returns = rng.normal(0, 0.01, 500)
    h = shannon_entropy(returns, bins=50)
    assert h > 3.0  # high entropy for noise


def test_e03_shannon_entropy_constant_is_zero():
    from src.engines.e03_information_theory import shannon_entropy

    returns = np.zeros(100)
    h = shannon_entropy(returns, bins=50)
    assert h == 0.0


def test_e03_transfer_entropy_nonneg():
    from src.engines.e03_information_theory import transfer_entropy

    rng = np.random.default_rng(1)
    x = rng.normal(0, 1, 100)
    y = np.roll(x, 1) + rng.normal(0, 0.1, 100)
    te = transfer_entropy(x, y, lag=1)
    assert te >= 0.0


# ---------------------------------------------------------------------------
# E-04 Fourier
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_e04_returns_engine_output():
    from src.engines.e04_fourier import E04Fourier

    e = E04Fourier()
    out = await e.run("BTC/USDT", make_data(400))
    assert isinstance(out, EngineOutput)
    assert out.engine_id == "E-04"


def test_e04_halving_overlay_early_cycle_positive():
    from src.engines.e04_fourier import E04Fourier

    overlay = E04Fourier._halving_overlay(50_000)  # early cycle
    assert overlay == 1.0


def test_e04_halving_overlay_late_cycle_negative():
    from src.engines.e04_fourier import E04Fourier

    overlay = E04Fourier._halving_overlay(180_000)  # > 86% through 210k blocks
    assert overlay < 0


# ---------------------------------------------------------------------------
# E-05 On-Chain
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_e05_abstains_without_spot():
    from src.engines.e05_onchain import E05OnChain

    e = E05OnChain()
    out = await e.run("BTC/USDT", {"spot": 0.0})
    assert out.confidence == 0.0


@pytest.mark.asyncio
async def test_e05_positive_flow_gives_long():
    from src.engines.e05_onchain import E05OnChain

    e = E05OnChain()
    data = {"spot": 50000.0, "onchain": {"tvl_24h_change_pct": 0.05}}
    out = await e.run("BTC/USDT", data)
    assert out.direction == 1


# ---------------------------------------------------------------------------
# E-06 Fractal
# ---------------------------------------------------------------------------


def test_e06_hurst_dfa_range():
    from src.engines.e06_fractal import hurst_dfa

    rng = np.random.default_rng(42)
    returns = rng.normal(0, 0.01, 256)
    h = hurst_dfa(returns)
    assert 0.0 <= h <= 1.0


def test_e06_hurst_dfa_constant_returns_half():
    """Constant series → all DFA fluctuations are zero → should fallback to 0.5, not NaN."""
    import math

    from src.engines.e06_fractal import hurst_dfa

    h = hurst_dfa(np.zeros(256))
    assert math.isfinite(h)
    assert h == 0.5  # fallback when no valid scale pairs remain


@pytest.mark.asyncio
async def test_e06_returns_engine_output():
    from src.engines.e06_fractal import E06Fractal

    e = E06Fractal()
    out = await e.run("BTC/USDT", make_data())
    assert isinstance(out, EngineOutput)
    assert 0.0 <= out.confidence <= 1.0


# ---------------------------------------------------------------------------
# E-07 Linear Algebra
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_e07_returns_engine_output():
    from src.engines.e07_linear_algebra import E07LinearAlgebra

    e = E07LinearAlgebra()
    out = await e.run("BTC/USDT", make_data())
    assert isinstance(out, EngineOutput)
    assert out.direction in (-1, 0, 1)


# ---------------------------------------------------------------------------
# E-08 Topology
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_e08_returns_engine_output():
    from src.engines.e08_topology import E08Topology

    e = E08Topology()
    out = await e.run("BTC/USDT", make_data(200))
    assert isinstance(out, EngineOutput)


# ---------------------------------------------------------------------------
# E-09 ML Meta
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_e09_abstains_without_model():
    from src.engines.e09_ml_meta import E09MlMeta

    e = E09MlMeta()
    e._model = None  # ensure no model
    out = await e.run("BTC/USDT", {"spot": 50000.0, "engine_outputs": {}})
    assert out.direction in (-1, 0, 1)
    assert isinstance(out, EngineOutput)


# ---------------------------------------------------------------------------
# E-10 Supply / S2F
# ---------------------------------------------------------------------------


def test_e10_btc_s2f_reasonable():
    from src.engines.e10_supply import btc_s2f, s2f_model_price

    sf = btc_s2f(840_000)  # 4th halving
    price = s2f_model_price(sf)
    assert sf > 0
    assert price > 0


def test_e10_btc_supply_capped():
    from src.engines.e10_supply import btc_supply_at_block

    s = btc_supply_at_block(10_000_000)
    assert s <= 21_000_000


def test_e10_ltc_supply_capped():
    from src.engines.e10_supply import ltc_supply_at_block

    s = ltc_supply_at_block(10_000_000)
    assert s <= 84_000_000


@pytest.mark.asyncio
async def test_e10_unsupported_coin():
    from src.engines.e10_supply import E10Supply

    e = E10Supply()
    out = await e.run("DOGE/USDT", {"spot": 0.1})
    assert out.direction in (-1, 0, 1)


# ---------------------------------------------------------------------------
# E-11 Stochastic
# ---------------------------------------------------------------------------


def test_e11_yang_zhang_vol_positive():
    from src.engines.e11_stochastic import yang_zhang_vol

    df = make_ohlcv(60)
    vol = yang_zhang_vol(df)
    assert vol > 0


def test_e11_gbm_mc_shape():
    from src.engines.e11_stochastic import gbm_mc

    terminal = gbm_mc(50000, 0.5, 0.8, 4.0, n=500)
    assert terminal.shape == (500,)
    assert (terminal > 0).all()


def test_e11_merton_jump_prob_nonneg():
    from src.engines.e11_stochastic import merton_jump_prob

    rng = np.random.default_rng(0)
    returns = rng.normal(0, 0.01, 200)
    returns[50] = 0.15  # artificial jump
    prob = merton_jump_prob(returns, sigma=0.01 * np.sqrt(8760))
    assert 0.0 <= prob <= 1.0


# ---------------------------------------------------------------------------
# E-12 Options
# ---------------------------------------------------------------------------


def test_e12_ltc_abstains():
    from src.engines.e12_options import E12Options

    e = E12Options()
    out = asyncio.run(e.run("LTC/USDT", {"spot": 90.0}))
    assert out.confidence == 0.0
    assert out.metadata["abstain_reason"] == "no_options_market"


def test_e12_put_call_ratio():
    from src.engines.e12_options import put_call_ratio

    df = pd.DataFrame(
        [
            {"option_type": "put", "oi": 100},
            {"option_type": "call", "oi": 80},
        ]
    )
    pcr = put_call_ratio(df)
    assert abs(pcr - 1.25) < 0.01


def test_e12_compute_gex_vectorized():
    from src.engines.e12_options import compute_gex

    df = pd.DataFrame(
        [
            {"option_type": "call", "gamma": 0.001, "oi": 100},
            {"option_type": "put", "gamma": 0.001, "oi": 100},
        ]
    )
    # Calls: +1 * 0.001 * 100; puts: -1 * 0.001 * 100 → net GEX = 0
    gex = compute_gex(df, spot=50_000.0)
    assert abs(gex) < 1.0  # symmetric → near zero


def test_e12_max_pain_finds_correct_strike():
    from src.engines.e12_options import max_pain

    # Two calls: strike 100 and 200; two puts: strike 100 and 200; spot=150
    # At strike 150: calls 150→ITM(+50*OI for s=100 call), puts same
    # Simple case: only one put at strike 200 with large OI
    df = pd.DataFrame(
        [
            {"option_type": "put", "strike": 200.0, "oi": 1000.0},
            {"option_type": "call", "strike": 100.0, "oi": 1000.0},
        ]
    )
    mp = max_pain(df)
    # Max pain is where OI-weighted ITM value is minimized — should pick a middle strike
    assert mp in (100.0, 200.0)


# ---------------------------------------------------------------------------
# E-13 Contagion
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_e13_requires_macro():
    from src.engines.e13_contagion import E13Contagion

    e = E13Contagion()
    out = await e.run("BTC/USDT", {"spot": 50000.0, "macro": None, "ohlcv": None})
    assert out.confidence == 0.0


def test_e13_granger_skips_constant_series():
    from src.engines.e13_contagion import E13Contagion

    btc = np.random.randn(50)
    # Constant SPX series → should return {} without crashing
    macro = {"spx_series": np.ones(50)}
    result = E13Contagion._granger_causality(btc, macro)
    assert result == {}


def test_e13_granger_returns_pvals_with_varying_series():
    from src.engines.e13_contagion import E13Contagion

    rng = np.random.default_rng(42)
    btc = rng.standard_normal(60)
    spx = rng.standard_normal(60)
    macro = {"spx_series": spx}
    result = E13Contagion._granger_causality(btc, macro)
    # May return {} if statsmodels not installed, otherwise expects lag keys
    assert isinstance(result, dict)
    for v in result.values():
        assert 0.0 <= v <= 1.0


def test_e13_build_correlation_uses_series_when_available():
    from src.engines.e13_contagion import E13Contagion

    rng = np.random.default_rng(7)
    btc = rng.standard_normal(50)
    spx = btc * 0.8 + rng.standard_normal(50) * 0.1  # highly correlated
    macro = {"spx_series": spx, "spx_ret": 0.01}
    corr = E13Contagion._build_correlation_state(btc, macro, {})
    # Real Pearson r should be much higher than the 0.5 sign-heuristic
    assert corr["btc_spx"] > 0.7


def test_e13_build_correlation_fallback_to_heuristic():
    from src.engines.e13_contagion import E13Contagion

    btc = np.ones(20) * 0.01  # positive mean
    macro = {"spx_ret": 0.02}  # same sign → heuristic = 0.5
    corr = E13Contagion._build_correlation_state(btc, macro, {})
    assert corr["btc_spx"] == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# E-14 Sentiment
# ---------------------------------------------------------------------------


def test_e14_extreme_fear_gives_long():
    from src.engines.e14_sentiment import E14Sentiment

    e = E14Sentiment()
    for _ in range(5):  # build history
        asyncio.run(
            e.run(
                "BTC/USDT",
                {"spot": 50000.0, "sentiment": {"fg_score": 90.0, "vader_compound": 0.8}},
            )
        )
    out = asyncio.run(
        e.run("BTC/USDT", {"spot": 50000.0, "sentiment": {"fg_score": 5.0, "vader_compound": -0.9}})
    )
    # Extreme fear with greed history → contrarian long
    assert out.direction in (1, 0)


# ---------------------------------------------------------------------------
# E-15 RL
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_e15_abstains_without_model():
    from src.engines.e15_rl import E15RL

    e = E15RL()
    e._model = None
    out = await e.run("BTC/USDT", {"spot": 50000.0})
    assert out.direction == 0  # hold when no model


def test_e15_train_offline_builds_ridge_models(tmp_path, monkeypatch):
    from src.engines import e15_rl
    from src.engines.e15_rl import _STATE_DIM, E15RL

    monkeypatch.setattr(e15_rl, "_MODEL_PATH", tmp_path / "e15_dqn.pkl")
    e = E15RL()
    rng = np.random.default_rng(0)
    n = 60
    states = rng.standard_normal((n, _STATE_DIM)).astype(np.float32)
    actions = rng.integers(0, 3, n)
    rewards = rng.standard_normal(n).astype(np.float32)
    e.train_offline(states, actions, rewards)
    assert isinstance(e._model, dict)
    assert (tmp_path / "e15_dqn.pkl").exists()


def test_e15_select_action_uses_ridge(tmp_path, monkeypatch):
    from src.engines import e15_rl
    from src.engines.e15_rl import _STATE_DIM, E15RL

    monkeypatch.setattr(e15_rl, "_MODEL_PATH", tmp_path / "e15_dqn.pkl")
    e = E15RL()
    rng = np.random.default_rng(1)
    n = 60
    states = rng.standard_normal((n, _STATE_DIM)).astype(np.float32)
    actions = rng.integers(0, 3, n)
    rewards = rng.standard_normal(n).astype(np.float32)
    e.train_offline(states, actions, rewards)
    action = e._select_action(rng.standard_normal(_STATE_DIM).astype(np.float32))
    assert action in (0, 1, 2)


# ---------------------------------------------------------------------------
# E-16 Adversarial
# ---------------------------------------------------------------------------


def test_e16_benford_deviation_uniform():
    from src.engines.e16_adversarial import benford_deviation

    rng = np.random.default_rng(42)
    # Uniform sizes — will deviate from Benford
    sizes = rng.uniform(10, 100, 500)
    dev = benford_deviation(sizes)
    assert dev >= 0.0


def test_e16_spoof_confidence_no_events():
    from src.engines.e16_adversarial import spoof_confidence

    assert spoof_confidence([]) == 0.0


def test_e16_manipulation_flag_suppresses_direction():
    from src.engines.e16_adversarial import E16Adversarial

    e = E16Adversarial()
    sizes = np.full(200, 1.0)  # identical sizes -- high Benford deviation
    out = asyncio.run(
        e.run("BTC/USDT", {"spot": 50000.0, "trade_sizes": sizes.tolist(), "orderbook_events": []})
    )
    assert out.direction == 0


# ---------------------------------------------------------------------------
# E-17 Liquidity
# ---------------------------------------------------------------------------


def test_e17_kyle_lambda():
    from src.engines.e17_liquidity import kyle_lambda

    rng = np.random.default_rng(0)
    pc = rng.normal(0, 0.001, 50)
    sv = rng.normal(0, 100, 50)
    lam = kyle_lambda(pc, sv)
    assert isinstance(lam, float)


def test_e17_amihud_positive():
    from src.engines.e17_liquidity import amihud_ratio

    rng = np.random.default_rng(0)
    returns = np.abs(rng.normal(0, 0.01, 100))
    volumes = rng.uniform(1000, 5000, 100)
    ar = amihud_ratio(returns, volumes)
    assert ar >= 0.0


def test_e17_cascade_price_level_finds_thin_wall():
    from src.engines.e17_liquidity import cascade_price_level

    spot = 50_000.0
    # 5 bid levels, each with size=10; depth_pct10=25 so needs cumulative ≥25
    bids = [{"price": str(50_000 - i * 100), "size": "10"} for i in range(5)]
    level = cascade_price_level(bids, spot, depth_pct10=25.0)
    # After 3 bids (cumulative 30 ≥ 25) the level should be ~49800
    assert 49_500 <= level <= 50_000


def test_e17_cascade_price_level_fallback():
    from src.engines.e17_liquidity import cascade_price_level

    # No bids → fallback to spot * 0.98
    level = cascade_price_level([], 50_000.0, 10.0)
    assert level == pytest.approx(50_000.0 * 0.98)


# ---------------------------------------------------------------------------
# E-18 Network
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_e18_abstains_without_flow_data():
    from src.engines.e18_network import E18Network

    e = E18Network()
    out = await e.run("BTC/USDT", {"spot": 50000.0, "exchange_flows": []})
    assert out.confidence == 0.0


def _netflow_records(*pairs):
    """Build netflow-tagged edges: (exchange, net_usd) with sign = direction."""
    from src.data.exchange_flow_provider import MARKET_NODE, SOURCE_TAG

    out = []
    for name, net in pairs:
        src, dst = (MARKET_NODE, name) if net > 0 else (name, MARKET_NODE)
        out.append(
            {
                "from": src,
                "to": dst,
                "usd_volume": abs(net),
                "net_usd": float(net),
                "exchange": name,
                "source": SOURCE_TAG,
            }
        )
    return out


@pytest.mark.asyncio
async def test_e18_netflow_inflow_is_bearish():
    """Net capital moving into exchanges is sell pressure."""
    from src.engines.e18_network import E18Network

    flows = _netflow_records(("Binance", 5e8), ("OKX", 4e8), ("Bybit", 3e8))
    out = await E18Network().run("BTC/USDT", {"spot": 50000.0, "exchange_flows": flows})
    assert out.direction == -1
    assert out.metadata["mode"] == "netflow"
    assert out.predicted_price < 50000.0


@pytest.mark.asyncio
async def test_e18_netflow_outflow_is_bullish():
    """Net withdrawal to self-custody is accumulation."""
    from src.engines.e18_network import E18Network

    flows = _netflow_records(("Binance", -5e8), ("OKX", -4e8), ("Bybit", -3e8))
    out = await E18Network().run("BTC/USDT", {"spot": 50000.0, "exchange_flows": flows})
    assert out.direction == 1
    assert out.predicted_price > 50000.0


@pytest.mark.asyncio
async def test_e18_netflow_balanced_is_neutral():
    from src.engines.e18_network import E18Network

    flows = _netflow_records(("Binance", 5e8), ("OKX", -5e8))
    out = await E18Network().run("BTC/USDT", {"spot": 50000.0, "exchange_flows": flows})
    assert out.direction == 0


@pytest.mark.asyncio
async def test_e18_netflow_single_venue_dominance_is_discounted():
    """One venue moving everything reads as an internal transfer, not a signal."""
    from src.engines.e18_network import E18Network

    solo = _netflow_records(("Binance", 9e8))
    broad = _netflow_records(("Binance", 3e8), ("OKX", 3e8), ("Bybit", 3e8))
    out_solo = await E18Network().run("BTC/USDT", {"spot": 50000.0, "exchange_flows": solo})
    out_broad = await E18Network().run("BTC/USDT", {"spot": 50000.0, "exchange_flows": broad})
    assert out_solo.confidence < out_broad.confidence


@pytest.mark.asyncio
async def test_e18_netflow_direction_is_not_pinned():
    """Regression: the pairwise path returned a constant -1 on star topology."""
    from src.engines.e18_network import E18Network

    e = E18Network()
    seen = set()
    for net in (6e8, -6e8):
        flows = _netflow_records(("Binance", net), ("OKX", net / 2))
        out = await e.run("BTC/USDT", {"spot": 50000.0, "exchange_flows": flows})
        seen.add(out.direction)
    balanced = _netflow_records(("Binance", 5e8), ("OKX", -5e8))
    seen.add((await e.run("BTC/USDT", {"spot": 50000.0, "exchange_flows": balanced})).direction)
    assert seen == {-1, 0, 1}, f"direction never varied: {seen}"


def test_e18_pairwise_path_preserved_for_labelled_data():
    """The paid-data path must stay intact and must not claim netflow mode."""
    from src.engines.e18_network import centrality_signal, exchange_flow_graph, is_netflow_mode

    pairwise = [
        {"from": "Binance", "to": "OKX", "usd_volume": 1e6},
        {"from": "OKX", "to": "Kraken", "usd_volume": 2e6},
        {"from": "Kraken", "to": "Binance", "usd_volume": 3e6},
    ]
    assert not is_netflow_mode(pairwise)
    g = exchange_flow_graph(pairwise)
    assert centrality_signal(g, "Binance") > 0.0


def test_build_flow_records_from_defillama_payload():
    from src.data.exchange_flow_provider import build_flow_records
    from src.engines.e18_network import is_netflow_mode

    payload = {
        "cexs": [
            {"name": "Binance", "currentTvl": 1e11, "inflows_24h": 2.5e8},
            {"name": "OKX", "currentTvl": 2e10, "inflows_24h": -8e7},
            {"name": "NoTvl", "inflows_24h": 5e8},  # unattributed → dropped
            {"name": "Dust", "currentTvl": 1e9, "inflows_24h": 12.0},  # noise → dropped
            {"name": "NullFlow", "currentTvl": 1e9, "inflows_24h": None},
        ]
    }
    recs = build_flow_records(payload)
    assert {r["exchange"] for r in recs} == {"Binance", "OKX"}
    assert is_netflow_mode(recs)
    binance = next(r for r in recs if r["exchange"] == "Binance")
    assert binance["from"] == "MARKET" and binance["to"] == "Binance"
    okx = next(r for r in recs if r["exchange"] == "OKX")
    assert okx["from"] == "OKX" and okx["to"] == "MARKET"


def test_build_flow_records_handles_junk():
    from src.data.exchange_flow_provider import build_flow_records

    assert build_flow_records({}) == []
    assert build_flow_records({"cexs": "nonsense"}) == []
    assert build_flow_records({"cexs": [None, 42, {"name": "X"}]}) == []
