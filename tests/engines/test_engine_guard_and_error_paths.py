"""Abstain guards, error handlers and residual branches of the Crypto-Box engines.

`test_engines_unit.py` covers the schema contract and `test_engine_paths.py`
the happy computation paths; what was left uncovered were the `except` arms,
a few abstain guards and the optional-dependency branches. Each engine is
driven through them here with real inputs wherever the branch is reachable
that way, and with a targeted patch of the engine's own helper otherwise.
"""

from __future__ import annotations

import json
import sys
import types

import numpy as np
import pandas as pd
import pytest

from src.engines.schema import EngineOutput


def make_ohlcv(n: int = 300, seed: int = 5) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    closes = np.cumprod(1 + rng.normal(0.0002, 0.01, n)) * 50_000.0
    return pd.DataFrame(
        {
            "timestamp_utc": pd.date_range("2025-01-01", periods=n, freq="h", tz="UTC"),
            "open": closes * 0.999,
            "high": closes * 1.004,
            "low": closes * 0.996,
            "close": closes,
            "volume": rng.uniform(1000, 5000, n),
        }
    )


def _assert_abstained(out: EngineOutput, spot: float) -> None:
    assert out.direction == 0
    assert out.confidence == 0.0
    assert out.predicted_price == pytest.approx(spot)


# ---------------------------------------------------------------------------
# E-02 microstructure
# ---------------------------------------------------------------------------


class TestE02:
    def _ob(self, bids: list, asks: list) -> pd.DataFrame:
        return pd.DataFrame(
            [{"bids_json": json.dumps(bids), "asks_json": json.dumps(asks)}],
        )

    @pytest.mark.asyncio
    async def test_malformed_orderbook_json_abstains_with_the_error(self) -> None:
        from src.engines.e02_microstructure import E02Microstructure

        df = pd.DataFrame([{"bids_json": "not-json", "asks_json": "[]"}])
        out = await E02Microstructure().run("BTC/USDT", {"orderbook": df, "spot": 50_000.0})

        _assert_abstained(out, 50_000.0)

    def test_empty_book_sides_report_a_balanced_imbalance(self) -> None:
        from src.engines.e02_microstructure import E02Microstructure

        assert E02Microstructure._bid_ask_imbalance([], []) == 0.5

    @pytest.mark.parametrize(
        ("imbalance", "expected"),
        [(0.8, 1), (0.2, -1), (0.5, 0)],
    )
    def test_direction_thresholds(self, imbalance: float, expected: int) -> None:
        from src.engines.e02_microstructure import E02Microstructure

        assert E02Microstructure._direction_from_imbalance(imbalance) == expected

    @pytest.mark.asyncio
    async def test_ask_heavy_book_predicts_a_short(self) -> None:
        from src.engines.e02_microstructure import E02Microstructure

        df = self._ob(bids=[[49_000, 1.0]], asks=[[51_000, 9.0]])
        out = await E02Microstructure().run("BTC/USDT", {"orderbook": df, "spot": 50_000.0})

        assert out.direction == -1
        assert out.predicted_price < 50_000.0


# ---------------------------------------------------------------------------
# E-04 Fourier
# ---------------------------------------------------------------------------


class TestE04:
    @pytest.mark.asyncio
    async def test_short_history_abstains(self) -> None:
        from src.engines.e04_fourier import E04Fourier

        out = await E04Fourier().run("BTC/USDT", {"ohlcv": make_ohlcv(20), "spot": 50_000.0})
        _assert_abstained(out, 50_000.0)

    @pytest.mark.asyncio
    async def test_non_btc_symbol_skips_the_halving_overlay(self) -> None:
        from src.engines.e04_fourier import E04Fourier

        df = make_ohlcv()
        spot = float(df["close"].iloc[-1])
        out = await E04Fourier().run("ETH/USDT", {"ohlcv": df, "spot": spot})

        assert out.engine_id == "E-04"
        assert "explained_variance" in out.metadata

    @pytest.mark.asyncio
    async def test_failure_inside_the_fft_abstains_with_the_error(self, monkeypatch) -> None:
        from src.engines import e04_fourier

        def _boom(*_a, **_kw):
            raise ValueError("fft blew up")

        monkeypatch.setattr(e04_fourier.E04Fourier, "_fft_predict", staticmethod(_boom))
        out = await e04_fourier.E04Fourier().run(
            "BTC/USDT", {"ohlcv": make_ohlcv(), "spot": 50_000.0}
        )
        _assert_abstained(out, 50_000.0)


# ---------------------------------------------------------------------------
# E-05 on-chain
# ---------------------------------------------------------------------------


class TestE05:
    @pytest.mark.asyncio
    async def test_falls_back_to_defillama_when_no_cached_onchain_data(self, monkeypatch) -> None:
        from src.engines import e05_onchain

        class _Provider:
            async def fetch_metrics(self):
                return {"tvl": 1.0, "tvl_change_pct": 0.05}

        module = types.ModuleType("src.intelligence.onchain.defillama_provider")
        module.DeFiLlamaProvider = _Provider
        monkeypatch.setitem(sys.modules, "src.intelligence.onchain.defillama_provider", module)

        out = await e05_onchain.E05OnChain().run("BTC/USDT", {"spot": 50_000.0})

        assert out.direction == 1  # +5% TVL is above the 2% accumulation threshold
        assert out.metadata["net_flow_normalized"] == pytest.approx(0.05)

    @pytest.mark.asyncio
    async def test_defillama_failure_degrades_to_neutral_flow(self, monkeypatch) -> None:
        from src.engines import e05_onchain

        class _Provider:
            async def fetch_metrics(self):
                raise RuntimeError("defillama down")

        module = types.ModuleType("src.intelligence.onchain.defillama_provider")
        module.DeFiLlamaProvider = _Provider
        monkeypatch.setitem(sys.modules, "src.intelligence.onchain.defillama_provider", module)

        out = await e05_onchain.E05OnChain().run("BTC/USDT", {"spot": 50_000.0})

        assert out.direction == 0
        assert out.metadata["net_flow_normalized"] == 0.0

    @pytest.mark.asyncio
    async def test_strong_outflow_is_read_as_distribution(self) -> None:
        from src.engines.e05_onchain import E05OnChain

        out = await E05OnChain().run(
            "BTC/USDT", {"spot": 50_000.0, "onchain": {"tvl_24h_change_pct": -0.4}}
        )
        assert out.direction == -1
        assert out.predicted_price < 50_000.0

    @pytest.mark.asyncio
    async def test_a_failing_flow_computation_abstains(self, monkeypatch) -> None:
        from src.engines import e05_onchain

        async def _boom(self, symbol, data):
            raise RuntimeError("flow failed")

        monkeypatch.setattr(e05_onchain.E05OnChain, "_compute_net_flow", _boom)
        out = await e05_onchain.E05OnChain().run("BTC/USDT", {"spot": 50_000.0})
        _assert_abstained(out, 50_000.0)


# ---------------------------------------------------------------------------
# E-06 fractal
# ---------------------------------------------------------------------------


class TestE06:
    def test_hurst_of_a_too_short_series_is_the_random_walk_default(self) -> None:
        from src.engines.e06_fractal import hurst_dfa

        assert hurst_dfa(np.arange(8, dtype=float), min_scale=4) == 0.5

    def test_hurst_skips_scales_that_cannot_hold_two_segments(self) -> None:
        from src.engines.e06_fractal import hurst_dfa

        rng = np.random.default_rng(3)
        # max_scale above n//2 means the largest scales yield <2 segments and
        # are skipped rather than producing a one-segment fluctuation.
        h = hurst_dfa(rng.normal(size=40), min_scale=4, max_scale=30)
        assert 0.0 <= h <= 1.0

    def test_hurst_of_a_perfectly_flat_series_is_the_default(self) -> None:
        from src.engines.e06_fractal import hurst_dfa

        # zero fluctuation at every scale -> no usable log-log pairs
        assert hurst_dfa(np.zeros(64)) == 0.5

    def test_hurst_needs_at_least_two_distinct_scales(self) -> None:
        from src.engines.e06_fractal import hurst_dfa

        # n // 4 == min_scale, so the log-spaced scales collapse to a single
        # value and there is no slope to fit.
        assert hurst_dfa(np.random.default_rng(9).normal(size=16), min_scale=4) == 0.5

    @pytest.mark.asyncio
    async def test_short_history_abstains(self) -> None:
        from src.engines.e06_fractal import E06Fractal

        out = await E06Fractal().run("BTC/USDT", {"ohlcv": make_ohlcv(20), "spot": 50_000.0})
        _assert_abstained(out, 50_000.0)

    @pytest.mark.asyncio
    async def test_failure_inside_hurst_abstains_with_the_error(self, monkeypatch) -> None:
        from src.engines import e06_fractal

        monkeypatch.setattr(
            e06_fractal, "hurst_dfa", lambda *_a, **_kw: (_ for _ in ()).throw(ValueError("nan"))
        )
        out = await e06_fractal.E06Fractal().run(
            "BTC/USDT", {"ohlcv": make_ohlcv(), "spot": 50_000.0}
        )
        _assert_abstained(out, 50_000.0)


# ---------------------------------------------------------------------------
# E-08 topology
# ---------------------------------------------------------------------------


class TestE08:
    @pytest.mark.asyncio
    async def test_short_history_abstains(self) -> None:
        from src.engines.e08_topology import E08Topology

        out = await E08Topology().run("BTC/USDT", {"ohlcv": make_ohlcv(20), "spot": 50_000.0})
        _assert_abstained(out, 50_000.0)

    def test_compute_tda_uses_giotto_persistence_when_installed(self, monkeypatch) -> None:
        from src.engines.e08_topology import E08Topology

        class _VR:
            def __init__(self, homology_dimensions):
                self.dims = homology_dimensions

            def fit_transform(self, _x):
                # (birth, death, homology_dim) triples: two H0 and two H1 bars
                return [
                    np.array(
                        [
                            [0.0, 1.0, 0.0],
                            [0.0, 2.0, 0.0],
                            [0.5, 1.5, 1.0],
                            [0.5, 3.0, 1.0],
                        ]
                    )
                ]

        gtda = types.ModuleType("gtda")
        homology = types.ModuleType("gtda.homology")
        homology.VietorisRipsPersistence = _VR
        gtda.homology = homology
        monkeypatch.setitem(sys.modules, "gtda", gtda)
        monkeypatch.setitem(sys.modules, "gtda.homology", homology)

        w_dist, entropy = E08Topology()._compute_tda(np.random.default_rng(0).normal(size=(20, 3)))

        assert w_dist == pytest.approx(2.5)  # longest H1 bar: 3.0 - 0.5
        assert entropy > 0.0

    def test_persistence_entropy_of_only_infinite_bars_is_zero(self) -> None:
        from src.engines.e08_topology import _persistence_entropy

        assert _persistence_entropy([[(0.0, np.inf)], [(1.0, 1.0)]]) == 0.0

    def test_sliding_window_embed_of_a_too_short_series_is_empty(self) -> None:
        from src.engines.e08_topology import _sliding_window_embed

        assert _sliding_window_embed(np.zeros(2), dim=5, lag=1).shape == (0, 5)


# ---------------------------------------------------------------------------
# E-09 ML meta
# ---------------------------------------------------------------------------


class TestE09:
    @pytest.mark.asyncio
    async def test_missing_spot_abstains(self) -> None:
        from src.engines.e09_ml_meta import E09MlMeta

        out = await E09MlMeta().run("BTC/USDT", {"spot": 0.0})
        _assert_abstained(out, 0.0)

    @pytest.mark.asyncio
    async def test_failure_building_features_abstains(self, monkeypatch) -> None:
        from src.engines import e09_ml_meta

        def _boom(self, engine_outputs, spot):
            raise RuntimeError("bad feature")

        monkeypatch.setattr(e09_ml_meta.E09MlMeta, "_build_features", _boom)
        out = await e09_ml_meta.E09MlMeta().run("BTC/USDT", {"spot": 50_000.0})
        _assert_abstained(out, 50_000.0)


# ---------------------------------------------------------------------------
# E-11 stochastic
# ---------------------------------------------------------------------------


class TestE11:
    def test_yang_zhang_falls_back_to_close_to_close_on_a_short_frame(self) -> None:
        from src.engines.e11_stochastic import yang_zhang_vol

        vol = yang_zhang_vol(make_ohlcv(10), window=21)
        assert vol > 0.0

    def test_merton_jump_prob_of_an_empty_series_is_zero(self) -> None:
        from src.engines.e11_stochastic import merton_jump_prob

        assert merton_jump_prob(np.array([]), 0.5) == 0.0

    @pytest.mark.asyncio
    async def test_short_history_abstains(self) -> None:
        from src.engines.e11_stochastic import E11Stochastic

        out = await E11Stochastic().run("BTC/USDT", {"ohlcv": make_ohlcv(10), "spot": 50_000.0})
        _assert_abstained(out, 50_000.0)

    @pytest.mark.asyncio
    async def test_failure_in_the_monte_carlo_abstains(self, monkeypatch) -> None:
        from src.engines import e11_stochastic

        monkeypatch.setattr(
            e11_stochastic,
            "gbm_mc",
            lambda *_a, **_kw: (_ for _ in ()).throw(ValueError("mc failed")),
        )
        out = await e11_stochastic.E11Stochastic().run(
            "BTC/USDT", {"ohlcv": make_ohlcv(), "spot": 50_000.0}
        )
        _assert_abstained(out, 50_000.0)


# ---------------------------------------------------------------------------
# E-14 sentiment
# ---------------------------------------------------------------------------


class TestE14:
    @pytest.mark.asyncio
    async def test_missing_spot_abstains(self) -> None:
        from src.engines.e14_sentiment import E14Sentiment

        out = await E14Sentiment().run("BTC/USDT", {"spot": 0.0})
        _assert_abstained(out, 0.0)

    @pytest.mark.asyncio
    async def test_failure_scoring_sentiment_abstains(self, monkeypatch) -> None:
        from src.engines import e14_sentiment

        monkeypatch.setattr(
            e14_sentiment,
            "raw_sentiment",
            lambda *_a, **_kw: (_ for _ in ()).throw(ValueError("bad score")),
        )
        out = await e14_sentiment.E14Sentiment().run("BTC/USDT", {"spot": 50_000.0})
        _assert_abstained(out, 50_000.0)


# ---------------------------------------------------------------------------
# E-15 RL
# ---------------------------------------------------------------------------


class TestE15:
    @pytest.mark.asyncio
    async def test_missing_spot_abstains(self) -> None:
        from src.engines.e15_rl import E15RL

        out = await E15RL().run("BTC/USDT", {"spot": 0.0})
        _assert_abstained(out, 0.0)

    @pytest.mark.asyncio
    async def test_failure_building_state_abstains(self, monkeypatch) -> None:
        from src.engines import e15_rl

        def _boom(self, data):
            raise RuntimeError("state failed")

        monkeypatch.setattr(e15_rl.E15RL, "_build_state", _boom)
        out = await e15_rl.E15RL().run("BTC/USDT", {"spot": 50_000.0})
        _assert_abstained(out, 50_000.0)

    def test_a_regressor_that_raises_scores_that_action_zero(self) -> None:
        from src.engines.e15_rl import E15RL

        class _Bad:
            def predict(self, _x):
                raise RuntimeError("not fitted")

        class _Good:
            def predict(self, _x):
                return np.array([1.0])

        engine = E15RL()
        engine._model = {0: _Bad(), 1: _Good()}

        # the failing regressor scores 0.0, so the working long action wins
        assert engine._select_action(np.zeros(27, dtype=np.float32)) == 1

    def test_model_load_failure_leaves_the_engine_modelless(self, monkeypatch, tmp_path) -> None:
        from src.engines import e15_rl

        bad = tmp_path / "dqn.pkl"
        bad.write_bytes(b"not a pickle")
        monkeypatch.setattr(e15_rl, "_MODEL_PATH", bad)

        engine = e15_rl.E15RL()
        assert engine._model is None


# ---------------------------------------------------------------------------
# E-18 network
# ---------------------------------------------------------------------------


class TestE18:
    @pytest.mark.asyncio
    async def test_missing_spot_abstains(self) -> None:
        from src.engines.e18_network import E18Network

        out = await E18Network().run("BTC/USDT", {"spot": 0.0, "exchange_flows": [{"a": 1}]})
        _assert_abstained(out, 0.0)

    def test_whale_cluster_score_is_zero_when_pagerank_fails(self, monkeypatch) -> None:
        import networkx as nx

        from src.engines.e18_network import whale_cluster_score

        g = nx.DiGraph()
        g.add_edge("Binance", "OKX", weight=1.0)
        monkeypatch.setattr(
            nx, "pagerank", lambda *_a, **_kw: (_ for _ in ()).throw(RuntimeError("no converge"))
        )

        assert whale_cluster_score(g, "Binance") == 0.0

    @pytest.mark.asyncio
    async def test_graph_failure_abstains_with_the_error(self, monkeypatch) -> None:
        from src.engines import e18_network

        monkeypatch.setattr(
            e18_network,
            "exchange_flow_graph",
            lambda *_a, **_kw: (_ for _ in ()).throw(ValueError("bad flow row")),
        )
        out = await e18_network.E18Network().run(
            "BTC/USDT",
            {
                "spot": 50_000.0,
                "exchange_flows": [{"from": "Binance", "to": "OKX", "amount_usd": 1.0}],
            },
        )
        _assert_abstained(out, 50_000.0)


# ---------------------------------------------------------------------------
# Residual guards across the remaining engines
# ---------------------------------------------------------------------------


class TestResidualGuards:
    def test_sample_entropy_is_zero_when_no_templates_match(self) -> None:
        from src.engines.e03_information_theory import sample_entropy

        # a strict ramp: consecutive samples are further apart than the
        # 0.2-sigma tolerance, so no template matches and the ratio is undefined.
        series = np.arange(12, dtype=float)
        assert sample_entropy(series) == 0.0

    @pytest.mark.asyncio
    async def test_e03_short_history_abstains(self) -> None:
        from src.engines.e03_information_theory import E03InformationTheory

        out = await E03InformationTheory().run(
            "BTC/USDT", {"ohlcv": make_ohlcv(10), "spot": 50_000.0}
        )
        _assert_abstained(out, 50_000.0)

    @pytest.mark.asyncio
    async def test_e07_short_history_abstains(self) -> None:
        from src.engines.e07_linear_algebra import E07LinearAlgebra

        out = await E07LinearAlgebra().run("BTC/USDT", {"ohlcv": make_ohlcv(10), "spot": 50_000.0})
        _assert_abstained(out, 50_000.0)

    @pytest.mark.asyncio
    async def test_e10_computation_failure_abstains(self, monkeypatch) -> None:
        from src.engines import e10_supply

        monkeypatch.setattr(
            e10_supply.E10Supply,
            "_compute",
            staticmethod(lambda *_a: (_ for _ in ()).throw(ValueError("bad height"))),
        )
        out = await e10_supply.E10Supply().run("BTC/USDT", {"spot": 50_000.0})
        _assert_abstained(out, 50_000.0)

    @pytest.mark.asyncio
    async def test_e13_missing_spot_abstains(self) -> None:
        from src.engines.e13_contagion import E13Contagion

        out = await E13Contagion().run("BTC/USDT", {"spot": 0.0})
        _assert_abstained(out, 0.0)

    def test_e13_correlation_skips_a_macro_series_that_is_too_short(self) -> None:
        from src.engines.e13_contagion import E13Contagion

        btc = np.linspace(0.0, 0.05, 40)
        corr = E13Contagion._build_correlation_state(
            btc, {"spx_series": [1.0, 2.0, 3.0], "spx_ret": 0.01}, {}
        )

        # too few SPX points to correlate -> the scalar sign heuristic instead
        assert corr["btc_spx"] == pytest.approx(0.5)

    def test_e13_granger_returns_empty_when_the_test_fails(self) -> None:
        from src.engines.e13_contagion import E13Contagion

        # a macro payload the Granger path cannot consume -> {} rather than raising
        # a non-numeric SPX series makes np.asarray raise inside the try
        assert (
            E13Contagion._granger_causality(np.linspace(0, 1, 40), {"spx_series": ["a", "b", "c"]})
            == {}
        )

    def test_e13_granger_reports_p_values_for_a_real_spx_series(self) -> None:
        from src.engines.e13_contagion import E13Contagion

        rng = np.random.default_rng(4)
        spx = rng.normal(0, 0.01, 60)
        btc = np.roll(spx, 1) * 0.8 + rng.normal(0, 0.002, 60)  # BTC lags SPX

        out = E13Contagion._granger_causality(btc, {"spx_series": spx.tolist()})

        assert set(out) == {"lag_1", "lag_2"}
        assert all(0.0 <= p <= 1.0 for p in out.values())

    @pytest.mark.asyncio
    async def test_e16_missing_spot_abstains(self) -> None:
        from src.engines.e16_adversarial import E16Adversarial

        out = await E16Adversarial().run("BTC/USDT", {"spot": 0.0})
        _assert_abstained(out, 0.0)

    @pytest.mark.asyncio
    async def test_e17_short_history_abstains(self) -> None:
        from src.engines.e17_liquidity import E17Liquidity

        out = await E17Liquidity().run("BTC/USDT", {"ohlcv": make_ohlcv(5), "spot": 50_000.0})
        _assert_abstained(out, 50_000.0)

    @pytest.mark.asyncio
    async def test_e18_abstains_when_networkx_is_not_installed(self, monkeypatch) -> None:
        from src.engines.e18_network import E18Network

        # a None entry in sys.modules makes `import networkx` raise ImportError,
        # which is exactly what a deployment without the extra sees.
        monkeypatch.setitem(sys.modules, "networkx", None)
        out = await E18Network().run(
            "BTC/USDT",
            {
                "spot": 50_000.0,
                "exchange_flows": [{"from": "Binance", "to": "OKX", "amount_usd": 1.0}],
            },
        )
        _assert_abstained(out, 50_000.0)

    def test_entropy_hysteresis_falls_back_to_the_long_horizon(self) -> None:
        from src.engines.consensus import TtlManager

        sel = TtlManager()
        assert sel.compute(0.95) == 1  # high entropy -> short horizon
        assert sel.compute(0.05) == 24  # decisively low again -> back to 24h

    def test_engine_output_log_failure_is_swallowed(self, tmp_path) -> None:
        from src.engines.orchestrator import EngineOrchestrator

        orch = EngineOrchestrator(data_root=tmp_path)
        # an entry that is not an EngineOutput raises while the rows are built;
        # logging is best-effort and must not propagate into the signal path.
        orch._log_engine_outputs("BTC/USDT", {"E-01": object()})

    def test_risk_quantifier_flags_an_active_tail_risk(self) -> None:
        from src.engines.risk_quantifier import RiskQuantifier

        out = RiskQuantifier().quantify(
            ci_low=45_000.0,
            ci_high=55_000.0,
            consensus=50_000.0,
            jump_prob=0.9,
            liquidity_score=0.05,
            yz_vol=1.2,
            horizon_hours=4,
        )
        assert out["tail_risk_score"] > 0.3
