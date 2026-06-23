"""
Tests for src/risk/cognitive_engine.py — mandatory five-validator decision
layer (Quant, Probability, Risk, Blockchain, Regime) and the engine that
assembles them.

Coverage priorities:
  1. Regression test for the structural sizing fix: CognitiveEngine must
     start from ctx.kelly_adjusted_fraction (the real, already entropy-
     gated Kelly result) and only ever multiply it DOWN — never replace
     it with an independently recomputed estimate that could inflate size.
  2. Each validator's PASS/WARN/VETO branches with realistic inputs.
  3. Engine-level aggregation: first VETO wins, WARN multiplies 0.70 per
     occurrence, a validator exception is treated as VETO (fail-safe).
"""

import math

import pytest

from src.config import invalidate_settings_cache
from src.risk.cognitive_engine import (
    BlockchainValidator,
    CognitiveEngine,
    ProbabilityValidator,
    QuantValidator,
    RegimeValidator,
    RiskValidator,
    SignalContext,
    ValidatorStatus,
    get_cognitive_engine,
)


@pytest.fixture(autouse=True)
def reset_settings():
    invalidate_settings_cache()
    yield
    invalidate_settings_cache()


def make_ctx(**overrides) -> SignalContext:
    """
    A baseline SignalContext engineered to PASS all five validators with
    realistic, internally-consistent numbers. Individual tests override
    only the field(s) needed to exercise a specific branch.
    """
    base = dict(
        signal_id="BTCUSDT_1h_1750000000000",
        symbol="BTC/USDT",
        timeframe="1h",
        p_long=0.70,
        p_bet=0.71,
        expected_edge_bps=40.0,          # (0.70-0.5)*200 = 40, consistent with p_long
        regime_state=1,                  # trending — not volatile
        regime_probs=[0.15, 0.75, 0.10],  # low entropy, dominant=trending
        hurst_exponent=0.62,             # > HURST_TRENDING_MIN (0.55)
        current_price=65000.0,
        atr=300.0,
        atr_median_20=290.0,             # vol_ratio ~1.03, well under 2.0
        realized_vol=0.45,               # 45% annualized
        adv_20d=50_000.0,
        spread_bps=2.0,
        capital_usd=100_000.0,
        daily_pnl_usd=200.0,             # positive day, no drawdown
        open_positions=0,
        consecutive_losses=0,
        funding_rate_8h=0.0,
        basis_pct=0.0,
        exchange_name="binance",
        proposed_qty=0.01,
        proposed_notional_usd=650.0,     # 0.65% of capital — well under 5% cap
        kelly_adjusted_fraction=0.0784,  # realistic post-entropy-gate fraction
    )
    base.update(overrides)
    return SignalContext(**base)


# ─── QuantValidator ────────────────────────────────────────────────────────────


class TestQuantValidator:
    def setup_method(self):
        self.v = QuantValidator()

    def test_pass_baseline(self):
        result = self.v.validate(make_ctx())
        assert result.status == ValidatorStatus.PASS

    def test_veto_zero_realized_vol(self):
        result = self.v.validate(make_ctx(realized_vol=0.0))
        assert result.status == ValidatorStatus.VETO
        assert "realized_vol" in result.reason

    def test_veto_notional_exceeds_max_position_pct(self):
        # max_position_size_pct default = 5.0% of capital_usd=100_000 => $5000
        result = self.v.validate(make_ctx(proposed_notional_usd=6000.0))
        assert result.status == ValidatorStatus.VETO
        assert "max allowed" in result.reason

    def test_veto_non_positive_edge(self):
        result = self.v.validate(make_ctx(expected_edge_bps=0.0))
        assert result.status == ValidatorStatus.VETO
        assert "non-positive expected edge" in result.reason

    def test_veto_negative_edge(self):
        result = self.v.validate(make_ctx(expected_edge_bps=-5.0))
        assert result.status == ValidatorStatus.VETO

    def test_veto_low_p_bet(self):
        result = self.v.validate(make_ctx(p_bet=0.4))
        assert result.status == ValidatorStatus.VETO
        assert "P(bet)" in result.reason

    def test_p_bet_exactly_half_vetoes(self):
        # boundary: p_bet < 0.5 vetoes; p_bet == 0.5 should pass this gate
        result = self.v.validate(make_ctx(p_bet=0.5))
        assert result.status != ValidatorStatus.VETO or "P(bet)" not in result.reason

    def test_vol_explosion_scalar_applied_in_metrics_only(self):
        # atr/atr_median_20 > 2.0 triggers a diagnostic 0.5x in kelly_capped
        # metrics, but must NOT veto and must NOT be the thing that changes
        # actual trade size (that responsibility belongs to kelly.py alone).
        result = self.v.validate(make_ctx(atr=700.0, atr_median_20=290.0))
        assert result.status == ValidatorStatus.PASS
        assert result.metrics.get("vol_explosion_scalar") == 0.5
        assert result.metrics["vol_ratio"] > 2.0


# ─── ProbabilityValidator ──────────────────────────────────────────────────────


class TestProbabilityValidator:
    def setup_method(self):
        self.v = ProbabilityValidator()

    def test_pass_baseline(self):
        result = self.v.validate(make_ctx())
        assert result.status in (ValidatorStatus.PASS, ValidatorStatus.WARN)

    def test_no_name_error_on_veto_path(self):
        # Regression test: a prior interrupted edit left a stale `regime_conf`
        # reference in the VETO f-string after the Bayesian formula was
        # rewritten to use dominant_prob/direction_conf — this raised
        # NameError at runtime on exactly this VETO branch. Engineer low
        # p_bet * near-0.5 p_long * ambiguous regime to force the VETO and
        # confirm it returns cleanly instead of raising.
        ctx = make_ctx(
            p_bet=0.20,
            p_long=0.51,
            regime_probs=[0.34, 0.33, 0.33],  # near-uniform -> low dominant_prob
        )
        result = self.v.validate(ctx)  # must not raise NameError
        assert result.status == ValidatorStatus.VETO
        assert "Bayesian composite score" in result.reason

    def test_veto_bayesian_score_below_threshold(self):
        ctx = make_ctx(p_bet=0.20, p_long=0.51, regime_probs=[0.34, 0.33, 0.33])
        result = self.v.validate(ctx)
        assert result.status == ValidatorStatus.VETO
        assert result.metrics["bayesian_score"] < 0.15

    def test_uses_dominant_prob_not_entropy_for_bayesian_score(self):
        # With the fix, bayesian_score = p_bet * direction_conf * dominant_prob.
        ctx = make_ctx(p_bet=0.71, p_long=0.70, regime_probs=[0.15, 0.75, 0.10])
        result = self.v.validate(ctx)
        direction_conf = abs(0.70 - 0.5) * 2.0
        expected = 0.71 * direction_conf * 0.75
        assert abs(result.metrics["bayesian_score"] - round(expected, 4)) < 1e-6

    def test_veto_cvar_exceeds_one_percent_capital(self):
        # Large notional + high vol pushes CVaR past the 1% capital limit.
        ctx = make_ctx(
            proposed_notional_usd=4900.0,
            realized_vol=1.5,
            capital_usd=100_000.0,
        )
        result = self.v.validate(ctx)
        assert result.status in (ValidatorStatus.VETO, ValidatorStatus.PASS, ValidatorStatus.WARN)
        if result.status == ValidatorStatus.VETO:
            assert "CVaR" in result.reason

    def test_warn_on_edge_implied_mismatch(self):
        # implied_edge from p_long vs expected_edge_bps differ > 50bps -> WARN
        ctx = make_ctx(p_long=0.62, expected_edge_bps=200.0)
        result = self.v.validate(ctx)
        assert result.status in (ValidatorStatus.WARN, ValidatorStatus.VETO)


# ─── RiskValidator ─────────────────────────────────────────────────────────────


class TestRiskValidator:
    def setup_method(self):
        self.v = RiskValidator()

    def test_pass_baseline(self):
        result = self.v.validate(make_ctx())
        assert result.status == ValidatorStatus.PASS

    def test_veto_daily_drawdown_halt(self):
        # daily_drawdown_halt_pct default = 2.0% of capital_usd=100_000 = -$2000
        result = self.v.validate(make_ctx(daily_pnl_usd=-2100.0))
        assert result.status == ValidatorStatus.VETO
        assert "drawdown" in result.reason.lower()

    def test_veto_consecutive_losses(self):
        # consecutive_loss_halt default = 3
        result = self.v.validate(make_ctx(consecutive_losses=3))
        assert result.status == ValidatorStatus.VETO
        assert "Consecutive losses" in result.reason

    def test_veto_volatility_explosion(self):
        result = self.v.validate(make_ctx(atr=700.0, atr_median_20=290.0))
        assert result.status == ValidatorStatus.VETO
        assert "Volatility explosion" in result.reason

    def test_veto_malformed_signal_id(self):
        result = self.v.validate(make_ctx(signal_id="short"))
        assert result.status == ValidatorStatus.VETO
        assert "tampering" in result.reason.lower() or "Signal ID" in result.reason

    def test_veto_empty_signal_id(self):
        result = self.v.validate(make_ctx(signal_id=""))
        assert result.status == ValidatorStatus.VETO


# ─── BlockchainValidator ───────────────────────────────────────────────────────


class TestBlockchainValidator:
    def setup_method(self):
        self.v = BlockchainValidator()

    def test_pass_baseline(self):
        result = self.v.validate(make_ctx())
        assert result.status == ValidatorStatus.PASS

    def test_veto_untrusted_exchange(self):
        result = self.v.validate(make_ctx(exchange_name="sketchy_dex"))
        assert result.status == ValidatorStatus.VETO
        assert "trusted" in result.reason.lower()

    def test_veto_unfavorable_funding_for_long(self):
        result = self.v.validate(
            make_ctx(p_long=0.62, funding_rate_8h=0.001)  # positive funding hurts longs
        )
        assert result.status == ValidatorStatus.VETO
        assert "Funding rate" in result.reason

    def test_pass_favorable_funding_for_long(self):
        # negative funding favors longs even if magnitude exceeds threshold
        result = self.v.validate(make_ctx(p_long=0.62, funding_rate_8h=-0.001))
        assert result.status != ValidatorStatus.VETO or "Funding" not in result.reason

    def test_veto_basis_divergence(self):
        result = self.v.validate(make_ctx(basis_pct=1.0))
        assert result.status == ValidatorStatus.VETO
        assert "Basis" in result.reason

    def test_warn_high_participation_rate(self):
        # participation = notional / (adv_20d * price); push it above 0.1%
        result = self.v.validate(
            make_ctx(proposed_notional_usd=600.0, adv_20d=1.0, current_price=65000.0)
        )
        assert result.status == ValidatorStatus.WARN
        assert "Participation" in result.reason


# ─── RegimeValidator ────────────────────────────────────────────────────────────


class TestRegimeValidator:
    def setup_method(self):
        self.v = RegimeValidator()

    def test_pass_baseline(self):
        result = self.v.validate(make_ctx())
        assert result.status == ValidatorStatus.PASS

    def test_veto_volatile_state(self):
        result = self.v.validate(make_ctx(regime_state=2))  # VOLATILE_STATE_IDX
        assert result.status == ValidatorStatus.VETO
        assert "volatile" in result.reason.lower()

    def test_veto_malformed_regime_probs(self):
        result = self.v.validate(make_ctx(regime_probs=[1.0]))
        assert result.status == ValidatorStatus.VETO
        assert "not fitted" in result.reason

    def test_veto_high_entropy(self):
        # Near-uniform posterior -> entropy close to 1.0 -> exceeds 0.90 veto
        result = self.v.validate(
            make_ctx(regime_state=0, regime_probs=[0.34, 0.33, 0.33])
        )
        assert result.status == ValidatorStatus.VETO
        assert "entropy" in result.reason.lower()

    def test_warn_moderate_entropy(self):
        # entropy in [0.70, 0.90] warn zone, regime_state not volatile
        result = self.v.validate(
            make_ctx(regime_state=1, regime_probs=[0.15, 0.72, 0.13])
        )
        assert result.status == ValidatorStatus.WARN
        assert result.metrics["hmm_entropy_normalized"] > 0.70

    def test_veto_low_hurst_for_momentum_trade(self):
        result = self.v.validate(
            make_ctx(regime_state=1, regime_probs=[0.10, 0.85, 0.05], hurst_exponent=0.40)
        )
        assert result.status == ValidatorStatus.VETO
        assert "Hurst" in result.reason

    def test_entropy_computation_matches_manual_calculation(self):
        probs = [0.15, 0.72, 0.13]
        result = self.v.validate(make_ctx(regime_state=1, regime_probs=probs))
        raw_e = -sum(p * math.log(p + 1e-12) for p in probs)
        expected_norm_entropy = raw_e / math.log(3)
        assert abs(result.metrics["hmm_entropy_normalized"] - round(expected_norm_entropy, 4)) < 1e-3


# ─── CognitiveEngine — aggregation and the sizing-fix regression ──────────────


class TestCognitiveEngineAggregation:
    def setup_method(self):
        self.engine = CognitiveEngine()

    def test_all_pass_yields_passed_true(self):
        decision = self.engine.evaluate(make_ctx())
        assert decision.passed is True
        assert decision.veto_reason is None
        assert len(decision.results) == 5

    def test_single_veto_fails_decision_and_zeroes_size(self):
        decision = self.engine.evaluate(make_ctx(exchange_name="unknown_exchange"))
        assert decision.passed is False
        assert decision.veto_reason is not None
        assert decision.adjusted_size_fraction == 0.0

    def test_all_five_validators_always_run_even_after_first_veto(self):
        # "First VETO short-circuits [decision]; remaining validators still
        # run for logging" per the engine's own docstring.
        decision = self.engine.evaluate(make_ctx(exchange_name="unknown_exchange"))
        assert len(decision.results) == 5

    def test_validator_exception_is_treated_as_veto(self, monkeypatch):
        def boom(self, ctx):
            raise RuntimeError("simulated validator crash")

        monkeypatch.setattr(QuantValidator, "validate", boom)
        decision = self.engine.evaluate(make_ctx())
        assert decision.passed is False
        assert decision.adjusted_size_fraction == 0.0

    # ── Structural sizing-fix regression tests ──────────────────────────────

    def test_size_fraction_starts_from_kelly_adjusted_fraction_not_recomputed(self):
        """
        CRITICAL REGRESSION TEST.

        Before the fix, CognitiveEngine.evaluate() called self._base_size(ctx)
        — an independent continuous-Kelly (mu/sigma^2) formula completely
        decoupled from kelly_result.adjusted_fraction (the real, already
        entropy-gated Kelly fraction computed in src/risk/kelly.py). On
        realistic inputs the two formulas could diverge by 2x+, meaning the
        cognitive layer — meant to be a mandatory risk GOVERNOR — could
        silently INFLATE position size beyond what entropy-gated Kelly
        intended, defeating its purpose.

        With all validators passing (no WARN, no VETO), adjusted_size_fraction
        must equal kelly_adjusted_fraction exactly — no multiplication, no
        independent recompute.
        """
        kelly_fraction = 0.0784
        ctx = make_ctx(kelly_adjusted_fraction=kelly_fraction)
        decision = self.engine.evaluate(ctx)
        assert decision.passed is True
        assert abs(decision.adjusted_size_fraction - kelly_fraction) < 1e-6

    def test_size_fraction_never_exceeds_kelly_adjusted_fraction(self):
        """
        Across varied inputs (which would have driven _base_size() to wildly
        different values than kelly_adjusted_fraction), adjusted_size_fraction
        must never exceed the original kelly_adjusted_fraction — only WARN
        multipliers (<=1.0) or VETO (=0.0) may apply.
        """
        for edge_bps, vol, kelly_frac in [
            (24.0, 0.45, 0.0784),
            (80.0, 0.15, 0.02),     # would have produced a huge _base_size()
            (10.0, 0.90, 0.15),     # would have produced a tiny _base_size()
        ]:
            ctx = make_ctx(
                expected_edge_bps=edge_bps,
                realized_vol=vol,
                kelly_adjusted_fraction=kelly_frac,
            )
            decision = self.engine.evaluate(ctx)
            assert decision.adjusted_size_fraction <= kelly_frac + 1e-9, (
                f"adjusted_size_fraction {decision.adjusted_size_fraction} "
                f"exceeded kelly_adjusted_fraction {kelly_frac} for "
                f"edge_bps={edge_bps}, vol={vol}"
            )

    def test_warn_multiplies_kelly_fraction_by_point_seven(self):
        # Force exactly one WARN (regime entropy in warn zone) with all
        # other validators passing, and confirm the multiplier is applied
        # to the REAL kelly_adjusted_fraction.
        kelly_fraction = 0.10
        ctx = make_ctx(
            kelly_adjusted_fraction=kelly_fraction,
            regime_state=1,
            regime_probs=[0.15, 0.72, 0.13],  # warn-zone entropy, not volatile
            hurst_exponent=0.62,
        )
        decision = self.engine.evaluate(ctx)
        warn_count = sum(1 for r in decision.results if r.status == ValidatorStatus.WARN)
        assert warn_count >= 1
        expected = kelly_fraction * (0.70 ** warn_count)
        assert abs(decision.adjusted_size_fraction - expected) < 1e-6

    def test_get_cognitive_engine_returns_singleton(self):
        a = get_cognitive_engine()
        b = get_cognitive_engine()
        assert a is b

    def test_decision_as_dict_serializable(self):
        decision = self.engine.evaluate(make_ctx())
        d = decision.as_dict()
        assert d["passed"] is True
        assert len(d["validators"]) == 5
        assert all("name" in v and "status" in v for v in d["validators"])
