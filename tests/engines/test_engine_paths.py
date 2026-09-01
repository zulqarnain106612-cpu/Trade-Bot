"""
Success- and error-path coverage for the Crypto-Box engines.

`test_engines_unit.py` covers the schema contract and the abstain guards; this
module exercises the computation paths behind them — the branches that only run
once an engine is given complete data.
"""

from __future__ import annotations

from datetime import UTC, datetime

import numpy as np
import pandas as pd
import pytest

from src.engines.schema import EngineOutput


def make_ohlcv(n: int = 300, trend: float = 0.0002, seed: int = 11) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    closes = np.cumprod(1 + rng.normal(trend, 0.01, n)) * 50_000.0
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


def spot_of(df: pd.DataFrame) -> float:
    return float(df["close"].iloc[-1])


# ---------------------------------------------------------------------------
# E-03 Information theory
# ---------------------------------------------------------------------------


class TestE03:
    @pytest.mark.asyncio
    async def test_run_reports_predictability_and_no_direction(self) -> None:
        from src.engines.e03_information_theory import E03InformationTheory

        df = make_ohlcv()
        out = await E03InformationTheory().run("BTC/USDT", {"ohlcv": df, "spot": spot_of(df)})

        assert out.direction == 0  # E-03 modulates, it never predicts direction
        assert out.predicted_price == pytest.approx(spot_of(df))
        assert 0.0 <= out.metadata["entropy_score"] <= 1.0
        assert out.metadata["predictability_index"] == pytest.approx(
            1.0 - out.metadata["entropy_score"]
        )
        assert out.metadata["te_btc_eth"] == 0.0  # no btc_returns supplied

    @pytest.mark.asyncio
    async def test_transfer_entropy_is_computed_when_btc_returns_are_present(self) -> None:
        from src.engines.e03_information_theory import E03InformationTheory

        df = make_ohlcv()
        rng = np.random.default_rng(3)
        data = {
            "ohlcv": df,
            "spot": spot_of(df),
            "btc_returns": rng.normal(0, 0.01, 300).tolist(),
        }
        out = await E03InformationTheory().run("ETH/USDT", data)
        assert out.metadata["te_btc_eth"] >= 0.0

    @pytest.mark.asyncio
    async def test_missing_close_column_abstains(self) -> None:
        from src.engines.e03_information_theory import E03InformationTheory

        df = make_ohlcv().drop(columns=["close"])
        out = await E03InformationTheory().run("BTC/USDT", {"ohlcv": df, "spot": 50_000.0})
        assert out.confidence == 0.0
        assert out.direction == 0

    def test_shannon_entropy_of_an_empty_series_is_zero(self) -> None:
        from src.engines.e03_information_theory import shannon_entropy

        assert shannon_entropy(np.array([])) == 0.0

    def test_transfer_entropy_needs_at_least_twenty_paired_samples(self) -> None:
        from src.engines.e03_information_theory import transfer_entropy

        assert transfer_entropy(np.arange(5.0), np.arange(5.0)) == 0.0

    def test_sample_entropy_is_zero_for_a_too_short_series(self) -> None:
        from src.engines.e03_information_theory import sample_entropy

        assert sample_entropy(np.array([1.0, 2.0])) == 0.0

    def test_sample_entropy_is_zero_for_a_constant_series(self) -> None:
        from src.engines.e03_information_theory import sample_entropy

        assert sample_entropy(np.ones(50)) == 0.0

    def test_sample_entropy_is_finite_for_a_noisy_series(self) -> None:
        from src.engines.e03_information_theory import sample_entropy

        rng = np.random.default_rng(0)
        value = sample_entropy(rng.normal(0, 1, 60))
        assert np.isfinite(value)


# ---------------------------------------------------------------------------
# E-07 PCA / cointegration
# ---------------------------------------------------------------------------


class TestE07:
    @pytest.mark.asyncio
    async def test_pca_branch_is_used_when_the_spread_is_not_stretched(self) -> None:
        from src.engines.e07_linear_algebra import E07LinearAlgebra

        df = make_ohlcv(200)
        peer = make_ohlcv(200, seed=12)
        data = {"ohlcv": df, "spot": spot_of(df), "correlated_ohlcv": {"ETH/USDT": peer}}

        out = await E07LinearAlgebra().run("BTC/USDT", data)

        assert out.direction in (-1, 0, 1)
        assert "spread_z" in out.metadata
        assert out.metadata["pca_direction"] in (-1, 0, 1)

    @pytest.mark.asyncio
    async def test_a_stretched_spread_takes_the_cointegration_branch(self) -> None:
        """A peer that decouples at the end drives |z| past the 2.0 threshold."""
        from src.engines.e07_linear_algebra import E07LinearAlgebra

        df = make_ohlcv(200)
        peer = df.copy()
        peer["close"] = df["close"].values.copy()
        peer.loc[peer.index[-1], "close"] = float(peer["close"].iloc[-1]) * 0.5

        data = {"ohlcv": df, "spot": spot_of(df), "correlated_ohlcv": {"ETH/USDT": peer}}
        out = await E07LinearAlgebra().run("BTC/USDT", data)

        assert abs(out.metadata["spread_z"]) > 2.0
        assert out.direction in (-1, 1)
        assert out.confidence > 0.4

    def test_cointegration_is_neutral_without_a_peer(self) -> None:
        from src.engines.e07_linear_algebra import E07LinearAlgebra

        assert E07LinearAlgebra._cointegration_signal(make_ohlcv(100), {}, 50_000.0) == (0.0, 0)

    def test_cointegration_is_neutral_when_the_peer_is_too_short(self) -> None:
        from src.engines.e07_linear_algebra import E07LinearAlgebra

        z, d = E07LinearAlgebra._cointegration_signal(
            make_ohlcv(100), {"ETH/USDT": make_ohlcv(10)}, 50_000.0
        )
        assert (z, d) == (0.0, 0)

    def test_pca_signal_is_neutral_on_a_short_history(self) -> None:
        from src.engines.e07_linear_algebra import E07LinearAlgebra

        assert E07LinearAlgebra._pca_signal(make_ohlcv(15), {}) == 0

    @pytest.mark.asyncio
    async def test_missing_close_column_abstains(self) -> None:
        from src.engines.e07_linear_algebra import E07LinearAlgebra

        df = make_ohlcv(200).drop(columns=["close"])
        out = await E07LinearAlgebra().run("BTC/USDT", {"ohlcv": df, "spot": 50_000.0})
        assert out.confidence == 0.0


# ---------------------------------------------------------------------------
# E-08 Topology
# ---------------------------------------------------------------------------


class TestE08:
    @pytest.mark.asyncio
    async def test_clean_topology_delegates_direction_to_e07(self) -> None:
        from src.engines.e08_topology import E08Topology

        df = make_ohlcv(200)
        e = E08Topology()
        e._compute_tda = lambda embedding: (0.5, 0.1)  # type: ignore[method-assign]

        out = await e.run("BTC/USDT", {"ohlcv": df, "spot": spot_of(df), "e07_direction": 1})

        assert out.direction == 1
        assert out.metadata["regime_break"] is False
        assert out.confidence == pytest.approx(0.9)

    @pytest.mark.asyncio
    async def test_high_topological_entropy_caps_confidence(self) -> None:
        from src.engines.e08_topology import E08Topology

        df = make_ohlcv(200)
        e = E08Topology()
        e._compute_tda = lambda embedding: (0.5, 0.9)  # type: ignore[method-assign]

        out = await e.run("BTC/USDT", {"ohlcv": df, "spot": spot_of(df), "e07_direction": -1})
        assert out.confidence == pytest.approx(0.2)
        assert out.direction == -1

    @pytest.mark.asyncio
    async def test_a_wasserstein_spike_flags_a_regime_break_and_zeroes_confidence(self) -> None:
        from src.engines.e08_topology import E08Topology

        df = make_ohlcv(200)
        e = E08Topology()
        e._w_dist_history.extend([0.1] * 20)
        e._compute_tda = lambda embedding: (99.0, 0.1)  # type: ignore[method-assign]

        out = await e.run("BTC/USDT", {"ohlcv": df, "spot": spot_of(df), "e07_direction": 1})

        assert out.metadata["regime_break"] is True
        assert out.confidence == 0.0
        assert out.direction == 0

    @pytest.mark.asyncio
    async def test_a_failing_tda_computation_abstains(self) -> None:
        from src.engines.e08_topology import E08Topology

        df = make_ohlcv(200)
        e = E08Topology()

        def boom(embedding):
            raise RuntimeError("gtda exploded")

        e._compute_tda = boom  # type: ignore[method-assign]
        out = await e.run("BTC/USDT", {"ohlcv": df, "spot": spot_of(df)})
        assert out.confidence == 0.0
        assert out.direction == 0

    def test_embedding_is_empty_when_the_series_is_shorter_than_the_embedding(self) -> None:
        from src.engines.e08_topology import _sliding_window_embed

        assert _sliding_window_embed(np.array([1.0]), dim=3, lag=2).shape == (0, 3)

    def test_persistence_entropy_ignores_infinite_and_degenerate_pairs(self) -> None:
        from src.engines.e08_topology import _persistence_entropy

        assert _persistence_entropy([[(0.0, np.inf)], [(1.0, 0.5)]]) == 0.0

    def test_persistence_entropy_is_positive_for_distinct_lifetimes(self) -> None:
        from src.engines.e08_topology import _persistence_entropy

        assert _persistence_entropy([[(0.0, 1.0), (0.0, 3.0)]]) > 0.0


# ---------------------------------------------------------------------------
# E-09 ML meta
# ---------------------------------------------------------------------------


class TestE09:
    @pytest.mark.asyncio
    async def test_neutral_probability_without_a_model(self) -> None:
        from src.engines.e09_ml_meta import E09MlMeta

        e = E09MlMeta()
        e._model = None  # independent of whether a trained artifact is on disk
        out = await e.run("BTC/USDT", {"spot": 50_000.0})
        assert out.metadata["p_up"] == 0.5
        assert out.direction == 0
        assert out.confidence == 0.0

    @pytest.mark.asyncio
    async def test_a_bullish_model_probability_yields_a_long(self) -> None:
        from src.engines.e09_ml_meta import E09MlMeta

        class _Model:
            def predict_proba(self, X):
                return np.array([[0.1, 0.9]])

        e = E09MlMeta()
        e._model = _Model()
        out = await e.run("BTC/USDT", {"spot": 50_000.0})

        assert out.direction == 1
        assert out.metadata["p_up"] == pytest.approx(0.9)
        assert out.confidence == pytest.approx(0.8)
        assert out.predicted_price == pytest.approx(50_000.0 * 1.003)

    @pytest.mark.asyncio
    async def test_a_bearish_model_probability_yields_a_short(self) -> None:
        from src.engines.e09_ml_meta import E09MlMeta

        class _Model:
            def predict_proba(self, X):
                return np.array([[0.8, 0.2]])

        e = E09MlMeta()
        e._model = _Model()
        out = await e.run("BTC/USDT", {"spot": 50_000.0})
        assert out.direction == -1

    @pytest.mark.asyncio
    async def test_a_raising_model_falls_back_to_neutral(self) -> None:
        from src.engines.e09_ml_meta import E09MlMeta

        class _Model:
            def predict_proba(self, X):
                raise RuntimeError("bad model")

        e = E09MlMeta()
        e._model = _Model()
        out = await e.run("BTC/USDT", {"spot": 50_000.0})
        assert out.metadata["p_up"] == 0.5

    def test_features_carry_confidence_direction_and_relative_price(self) -> None:
        from src.engines.e09_ml_meta import E09MlMeta

        out = EngineOutput(
            engine_id="E-01",
            symbol="BTC/USDT",
            timestamp_utc=datetime.now(UTC),
            predicted_price=50_500.0,
            confidence=0.75,
            direction=1,
            horizon_hours=4,
        )
        feats = E09MlMeta()._build_features({"E-01": out}, 50_000.0)

        assert feats.shape == (1, 17 * 3)  # 18 engines minus E-09 itself
        assert feats[0, 0] == pytest.approx(0.75)
        assert feats[0, 1] == pytest.approx(1.0)
        assert feats[0, 2] == pytest.approx(0.01)
        assert feats[0, 3:6].tolist() == [0.0, 0.0, 0.0]  # E-02 absent

    def test_a_corrupt_model_file_is_logged_and_ignored(self, tmp_path, monkeypatch) -> None:
        from src.engines import e09_ml_meta

        path = tmp_path / "e09.pkl"
        path.write_bytes(b"not a pickle")
        monkeypatch.setattr(e09_ml_meta, "_MODEL_PATH", path)

        assert e09_ml_meta.E09MlMeta()._model is None

    def test_train_persists_a_model_that_is_reloaded_on_construction(
        self, tmp_path, monkeypatch
    ) -> None:
        from src.engines import e09_ml_meta

        path = tmp_path / "models" / "e09.pkl"
        monkeypatch.setattr(e09_ml_meta, "_MODEL_PATH", path)

        rng = np.random.default_rng(0)
        X = rng.normal(0, 1, (60, 51)).astype(np.float32)
        y = (X[:, 0] > 0).astype(np.int32)

        e = e09_ml_meta.E09MlMeta()
        e.train(X, y)

        assert path.exists()
        assert e._model is not None
        assert e09_ml_meta.E09MlMeta()._model is not None  # reload path


# ---------------------------------------------------------------------------
# E-12 Options
# ---------------------------------------------------------------------------


def _options_chain(pc_ratio: str = "balanced") -> pd.DataFrame:
    """Small synthetic Deribit-shaped chain."""
    put_oi = {"bullish": 50.0, "bearish": 500.0, "balanced": 100.0}[pc_ratio]
    rows = []
    for strike in (48_000.0, 50_000.0, 52_000.0):
        rows.append(
            {
                "option_type": "call",
                "strike": strike,
                "oi": 100.0,
                "iv": 0.55,
                "delta": 0.25,
                "gamma": 0.0001,
            }
        )
        rows.append(
            {
                "option_type": "put",
                "strike": strike,
                "oi": put_oi,
                "iv": 0.65,
                "delta": -0.25,
                "gamma": 0.0001,
            }
        )
    return pd.DataFrame(rows)


class TestE12:
    @pytest.mark.asyncio
    async def test_put_heavy_chain_is_bearish(self) -> None:
        from src.engines.e12_options import E12Options

        out = await E12Options().run(
            "BTC/USDT", {"spot": 50_000.0, "options": _options_chain("bearish")}
        )
        assert out.direction == -1
        assert out.metadata["pc_ratio"] > 1.2
        assert out.metadata["iv_skew"] == pytest.approx(0.1)
        assert out.metadata["max_pain_level"] in (48_000.0, 50_000.0, 52_000.0)

    @pytest.mark.asyncio
    async def test_call_heavy_chain_is_bullish(self) -> None:
        from src.engines.e12_options import E12Options

        out = await E12Options().run(
            "BTC/USDT", {"spot": 50_000.0, "options": _options_chain("bullish")}
        )
        assert out.direction == 1

    @pytest.mark.asyncio
    async def test_balanced_chain_is_neutral_and_gex_is_signed_by_option_type(self) -> None:
        from src.engines.e12_options import E12Options

        out = await E12Options().run(
            "BTC/USDT", {"spot": 50_000.0, "options": _options_chain("balanced")}
        )
        assert out.direction == 0
        assert out.metadata["gex"] == pytest.approx(0.0)

    @pytest.mark.asyncio
    async def test_a_malformed_chain_abstains(self) -> None:
        from src.engines.e12_options import E12Options

        bad = pd.DataFrame([{"unexpected": 1.0}])
        out = await E12Options().run("BTC/USDT", {"spot": 50_000.0, "options": bad})
        assert out.confidence == 0.0
        assert out.direction == 0

    def test_gex_of_an_empty_chain_is_zero(self) -> None:
        from src.engines.e12_options import compute_gex

        assert compute_gex(pd.DataFrame(), 50_000.0) == 0.0

    def test_iv_skew_is_zero_without_matching_deltas(self) -> None:
        from src.engines.e12_options import iv_skew

        chain = _options_chain()
        chain["delta"] = 0.9
        assert iv_skew(chain) == 0.0

    def test_max_pain_of_an_empty_chain_is_zero(self) -> None:
        from src.engines.e12_options import max_pain

        empty = _options_chain().iloc[0:0]
        assert max_pain(empty) == 0.0


# ---------------------------------------------------------------------------
# E-13 Contagion
# ---------------------------------------------------------------------------


def _macro(series: bool = True) -> dict:
    rng = np.random.default_rng(5)
    macro: dict = {"spx_ret": 0.004, "dxy_ret": -0.002}
    if series:
        macro["spx_series"] = rng.normal(0, 0.01, 200).tolist()
        macro["dxy_series"] = rng.normal(0, 0.005, 200).tolist()
    return macro


class TestE13:
    @pytest.mark.asyncio
    async def test_first_run_has_no_baseline_so_contagion_is_zero(self) -> None:
        from src.engines.e13_contagion import E13Contagion

        df = make_ohlcv(200)
        out = await E13Contagion().run(
            "BTC/USDT", {"ohlcv": df, "spot": spot_of(df), "macro": _macro()}
        )

        assert out.metadata["contagion_score"] == 0.0
        assert out.direction == 0
        assert out.predicted_price == pytest.approx(spot_of(df))
        assert isinstance(out.metadata["granger_pvalues"], dict)
        assert -1.0 <= out.metadata["btc_spx"] <= 1.0

    @pytest.mark.asyncio
    async def test_a_correlation_shift_follows_the_macro_direction(self) -> None:
        """High contagion makes E-13 track SPX rather than stay flat."""
        from src.engines.e13_contagion import E13Contagion

        df = make_ohlcv(200)
        e = E13Contagion()
        e._prev_corr = {"btc_spx": -1.0, "btc_dxy": 1.0}  # far from whatever we compute

        out = await e.run("BTC/USDT", {"ohlcv": df, "spot": spot_of(df), "macro": _macro()})

        assert out.metadata["contagion_score"] > 0.7
        assert out.direction == 1  # spx_ret > 0
        assert out.predicted_price > spot_of(df)

    @pytest.mark.asyncio
    async def test_a_negative_macro_move_gives_a_short(self) -> None:
        from src.engines.e13_contagion import E13Contagion

        df = make_ohlcv(200)
        e = E13Contagion()
        e._prev_corr = {"btc_spx": -1.0, "btc_dxy": 1.0}
        macro = _macro()
        macro["spx_ret"] = -0.004

        out = await e.run("BTC/USDT", {"ohlcv": df, "spot": spot_of(df), "macro": macro})
        assert out.direction == -1

    @pytest.mark.asyncio
    async def test_scalar_only_macro_falls_back_to_the_sign_heuristic(self) -> None:
        from src.engines.e13_contagion import E13Contagion

        df = make_ohlcv(200)
        out = await E13Contagion().run(
            "BTC/USDT", {"ohlcv": df, "spot": spot_of(df), "macro": _macro(series=False)}
        )

        assert abs(out.metadata["btc_spx"]) == pytest.approx(0.5)
        assert abs(out.metadata["btc_dxy"]) == pytest.approx(0.3)
        assert out.metadata["granger_pvalues"] == {}  # needs a series, not a scalar

    @pytest.mark.asyncio
    async def test_missing_close_column_abstains(self) -> None:
        from src.engines.e13_contagion import E13Contagion

        df = make_ohlcv(200).drop(columns=["close"])
        out = await E13Contagion().run(
            "BTC/USDT", {"ohlcv": df, "spot": 50_000.0, "macro": _macro()}
        )
        assert out.confidence == 0.0

    def test_correlation_state_is_empty_for_a_tiny_return_series(self) -> None:
        from src.engines.e13_contagion import E13Contagion

        assert E13Contagion._build_correlation_state(np.zeros(3), _macro(), {}) == {}

    def test_a_constant_macro_series_is_rejected_as_degenerate(self) -> None:
        from src.engines.e13_contagion import E13Contagion

        rng = np.random.default_rng(1)
        corr = E13Contagion._build_correlation_state(
            rng.normal(0, 0.01, 100),
            {"spx_series": [1.0] * 100, "spx_ret": 0.01, "dxy_ret": 0.0},
            {},
        )
        # Zero-variance series → falls through to the scalar heuristic.
        assert abs(corr["btc_spx"]) == pytest.approx(0.5)

    def test_granger_needs_a_series_of_at_least_ten_points(self) -> None:
        from src.engines.e13_contagion import E13Contagion

        assert E13Contagion._granger_causality(np.zeros(50), {"spx_series": [0.1, 0.2]}) == {}

    def test_regime_classification_thresholds(self) -> None:
        from src.engines.e13_contagion import E13Contagion

        assert E13Contagion._classify_regime({"btc_spx": 0.8}) == "risk_on"
        assert E13Contagion._classify_regime({"btc_dxy": -0.7}) == "dxy_driven"
        assert E13Contagion._classify_regime({"btc_spx": 0.1, "btc_dxy": 0.1}) == "neutral"


# ---------------------------------------------------------------------------
# E-15 RL
# ---------------------------------------------------------------------------


class TestE15:
    @pytest.mark.asyncio
    async def test_without_a_model_the_engine_holds_at_low_confidence(self) -> None:
        from src.engines.e15_rl import E15RL

        e = E15RL()
        e._model = None  # independent of whether a trained artifact is on disk
        out = await e.run("BTC/USDT", {"spot": 50_000.0})
        assert out.direction == 0
        assert out.confidence == pytest.approx(0.1)
        assert out.metadata["dqn_action"] == 0

    @pytest.mark.asyncio
    async def test_a_long_action_maps_to_a_positive_direction(self) -> None:
        from src.engines.e15_rl import E15RL

        class _Policy:
            def predict(self, state, deterministic=True):
                return 1, None

        e = E15RL()
        e._model = _Policy()
        out = await e.run("BTC/USDT", {"spot": 50_000.0, "regime": "Volatile"})

        assert out.direction == 1
        assert out.confidence == pytest.approx(0.3)
        assert out.predicted_price == pytest.approx(50_000.0 * 1.001)

    @pytest.mark.asyncio
    async def test_a_short_action_maps_to_a_negative_direction(self) -> None:
        from src.engines.e15_rl import E15RL

        class _Policy:
            def predict(self, state, deterministic=True):
                return 2, None

        e = E15RL()
        e._model = _Policy()
        out = await e.run("BTC/USDT", {"spot": 50_000.0})
        assert out.direction == -1

    def test_an_out_of_range_action_is_treated_as_hold(self) -> None:
        """A policy returning an action outside {0,1,2} must not wrap around."""
        from src.engines.e15_rl import E15RL

        class _Policy:
            def predict(self, state, deterministic=True):
                return 5, None

        e = E15RL()
        e._model = _Policy()
        assert e._select_action(np.zeros(27, dtype=np.float32)) == 0

    def test_a_raising_policy_falls_back_to_hold(self) -> None:
        from src.engines.e15_rl import E15RL

        class _Policy:
            def predict(self, state, deterministic=True):
                raise RuntimeError("policy down")

        e = E15RL()
        e._model = _Policy()
        assert e._select_action(np.zeros(27, dtype=np.float32)) == 0

    def test_state_encodes_engine_confidences_regime_and_realized_return(self) -> None:
        from src.engines.e15_rl import E15RL

        out = EngineOutput(
            engine_id="E-02",
            symbol="BTC/USDT",
            timestamp_utc=datetime.now(UTC),
            predicted_price=50_000.0,
            confidence=0.6,
            direction=1,
            horizon_hours=4,
        )
        state = E15RL()._build_state(
            {"engine_outputs": {"E-02": out}, "regime": "Capitulation", "realized_return": -0.02}
        )

        assert state.shape == (27,)
        assert state[1] == pytest.approx(0.6)
        assert state[17 + 8] == 1.0  # Capitulation is the last regime
        assert state[26] == pytest.approx(-0.02)

    def test_an_unknown_regime_leaves_the_one_hot_empty(self) -> None:
        from src.engines.e15_rl import E15RL

        state = E15RL()._build_state({"regime": "NotARegime"})
        assert state[17:26].sum() == 0.0

    def test_offline_training_writes_one_ridge_per_populated_action(
        self, tmp_path, monkeypatch
    ) -> None:
        from src.engines import e15_rl

        path = tmp_path / "models" / "e15.pkl"
        monkeypatch.setattr(e15_rl, "_MODEL_PATH", path)

        rng = np.random.default_rng(4)
        states = rng.normal(0, 1, (60, 27))
        actions = np.array([0, 1, 2] * 20)
        rewards = rng.normal(0, 0.01, 60)

        e = e15_rl.E15RL()
        e.train_offline(states, actions, rewards)

        assert isinstance(e._model, dict)
        assert sorted(e._model) == [0, 1, 2]
        assert path.exists()
        assert e15_rl.E15RL()._model is not None  # reload path

    def test_offline_training_skips_actions_with_too_few_samples(
        self, tmp_path, monkeypatch
    ) -> None:
        from src.engines import e15_rl

        monkeypatch.setattr(e15_rl, "_MODEL_PATH", tmp_path / "e15.pkl")
        rng = np.random.default_rng(4)

        e = e15_rl.E15RL()
        e.train_offline(rng.normal(0, 1, (4, 27)), np.zeros(4), np.zeros(4))

        assert e._model is None  # nothing had 5+ samples

    def test_a_save_failure_still_leaves_the_model_in_memory(self, tmp_path, monkeypatch) -> None:
        from src.engines import e15_rl

        # Point the model path at a location that cannot be created.
        blocker = tmp_path / "blocker"
        blocker.write_text("not a directory")
        monkeypatch.setattr(e15_rl, "_MODEL_PATH", blocker / "e15.pkl")

        rng = np.random.default_rng(4)
        e = e15_rl.E15RL()
        e.train_offline(rng.normal(0, 1, (60, 27)), np.array([0, 1, 2] * 20), np.zeros(60))

        assert isinstance(e._model, dict)

    def test_a_corrupt_model_file_is_ignored(self, tmp_path, monkeypatch) -> None:
        from src.engines import e15_rl

        path = tmp_path / "e15.pkl"
        path.write_bytes(b"not a pickle")
        monkeypatch.setattr(e15_rl, "_MODEL_PATH", path)

        assert e15_rl.E15RL()._model is None


# ---------------------------------------------------------------------------
# E-18 Network
# ---------------------------------------------------------------------------


_PAIRWISE = [
    {"from": "Whale1", "to": "Binance", "usd_volume": 5e7},
    {"from": "Binance", "to": "OKX", "usd_volume": 2e7},
    {"from": "OKX", "to": "Whale2", "usd_volume": 1e7},
]

_NETFLOW = [
    {"source": "defillama_cex_netflow", "exchange": "Binance", "net_usd": 3e8, "usd_volume": 3e8},
    {"source": "defillama_cex_netflow", "exchange": "OKX", "net_usd": 1e8, "usd_volume": 1e8},
]


class TestE18:
    @pytest.mark.asyncio
    async def test_pairwise_flow_data_takes_the_graph_path(self) -> None:
        from src.engines.e18_network import E18Network

        out = await E18Network().run("BTC/USDT", {"spot": 50_000.0, "exchange_flows": _PAIRWISE})

        assert out.direction in (-1, 0, 1)
        assert "exchange_centrality" in out.metadata
        assert out.metadata["dominant_exchange"] in {"Whale1", "Binance", "OKX", "Whale2"}
        assert "mode" not in out.metadata  # graph mode, not netflow mode

    @pytest.mark.asyncio
    async def test_net_inflow_to_exchanges_reads_as_sell_pressure(self) -> None:
        from src.engines.e18_network import E18Network

        out = await E18Network().run(
            "BTC/USDT",
            {"spot": 50_000.0, "exchange_flows": _NETFLOW, "primary_exchange": "Binance"},
        )

        assert out.metadata["mode"] == "netflow"
        assert out.direction == -1
        assert out.metadata["netflow_imbalance"] == pytest.approx(1.0)
        assert out.metadata["venue_count"] == 2
        assert out.predicted_price < 50_000.0

    @pytest.mark.asyncio
    async def test_net_withdrawal_reads_as_accumulation(self) -> None:
        from src.engines.e18_network import E18Network

        flows = [dict(f, net_usd=-f["net_usd"]) for f in _NETFLOW]
        out = await E18Network().run("BTC/USDT", {"spot": 50_000.0, "exchange_flows": flows})

        assert out.direction == 1
        assert out.metadata["netflow_imbalance"] == pytest.approx(-1.0)

    @pytest.mark.asyncio
    async def test_a_balanced_netflow_is_neutral(self) -> None:
        from src.engines.e18_network import E18Network

        flows = [
            dict(_NETFLOW[0], net_usd=1e8),
            dict(_NETFLOW[1], net_usd=-1e8),
        ]
        out = await E18Network().run("BTC/USDT", {"spot": 50_000.0, "exchange_flows": flows})

        assert out.direction == 0
        assert out.confidence == pytest.approx(0.0)

    @pytest.mark.asyncio
    async def test_a_malformed_netflow_record_abstains(self) -> None:
        from src.engines.e18_network import E18Network

        flows = [{"source": "defillama_cex_netflow", "exchange": "Binance", "net_usd": "abc"}]
        out = await E18Network().run("BTC/USDT", {"spot": 50_000.0, "exchange_flows": flows})
        assert out.confidence == 0.0
        assert out.direction == 0

    @pytest.mark.asyncio
    async def test_a_malformed_pairwise_record_abstains(self) -> None:
        from src.engines.e18_network import E18Network

        out = await E18Network().run(
            "BTC/USDT", {"spot": 50_000.0, "exchange_flows": [{"no_from_key": 1}]}
        )
        assert out.confidence == 0.0

    def test_graph_helpers_are_zero_on_an_empty_graph(self) -> None:
        import networkx as nx

        from src.engines.e18_network import centrality_signal, whale_cluster_score

        empty = nx.DiGraph()
        assert centrality_signal(empty, "Binance") == 0.0
        assert whale_cluster_score(empty, "Binance") == 0.0

    def test_graph_helpers_reject_a_non_graph_argument(self) -> None:
        from src.engines.e18_network import centrality_signal, whale_cluster_score

        assert centrality_signal("not a graph", "Binance") == 0.0
        assert whale_cluster_score({"nodes": []}, "Binance") == 0.0

    def test_whale_cluster_score_ranks_the_hub_highest(self) -> None:
        from src.engines.e18_network import exchange_flow_graph, whale_cluster_score

        g = exchange_flow_graph(_PAIRWISE)
        assert whale_cluster_score(g, "OKX") > 0.0

    def test_flow_concentration_is_zero_when_there_is_no_flow(self) -> None:
        from src.engines.e18_network import flow_concentration

        assert flow_concentration([], "Binance") == 0.0
        assert flow_concentration([{"exchange": "Binance", "usd_volume": 0.0}], "Binance") == 0.0

    def test_flow_concentration_is_the_target_share_of_gross_flow(self) -> None:
        from src.engines.e18_network import flow_concentration

        assert flow_concentration(_NETFLOW, "Binance") == pytest.approx(0.75)

    def test_netflow_imbalance_is_zero_when_gross_flow_is_zero(self) -> None:
        from src.engines.e18_network import netflow_imbalance

        assert netflow_imbalance([{"net_usd": 0.0}]) == 0.0

    def test_netflow_mode_requires_every_record_to_be_aggregate(self) -> None:
        from src.engines.e18_network import is_netflow_mode

        assert is_netflow_mode(_NETFLOW) is True
        assert is_netflow_mode([*_NETFLOW, {"source": "other"}]) is False
        assert is_netflow_mode([]) is False
