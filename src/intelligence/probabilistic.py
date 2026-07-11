"""
Probabilistic inference engine for crypto intelligence.

Replaces deterministic thresholds with Bayesian probability models.

Authority:
  - Gelman et al. (2013) Bayesian Data Analysis
  - McElreath (2020) Statistical Rethinking
  - Pearl (2009) Causality
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd
import structlog
from scipy.special import expit  # Logistic function
from scipy.stats import beta, norm


log = structlog.get_logger(__name__)


@dataclass
class ProbabilisticPrediction:
    """
    Complete prediction with uncertainty quantification.

    Instead of: prediction = 0.75
    We output: prediction = 0.75, credible_interval = [0.62, 0.88], confidence = 0.82
    """

    point_estimate: float  # Expected value (posterior mean)
    lower_credible_interval: float  # 2.5th percentile
    upper_credible_interval: float  # 97.5th percentile
    posterior_samples: np.ndarray | None = None  # Full distribution
    model_uncertainty: float = 0.0  # From ensemble disagreement
    aleatoric_uncertainty: float = 0.0  # Irreducible noise
    epistemic_uncertainty: float = 0.0  # Reducible (learnable)
    confidence: float = 0.0  # 0-1, how certain are we?

    @property
    def credible_interval_width(self) -> float:
        """Width of 95% credible interval."""
        return self.upper_credible_interval - self.lower_credible_interval

    @property
    def is_confident(self) -> bool:
        """Is uncertainty small relative to estimate? (< 20% of estimate)."""
        if abs(self.point_estimate) < 0.01:
            return False
        return self.credible_interval_width < 0.2 * abs(self.point_estimate)

    def decision_weight(self) -> float:
        """
        Weight estimate by confidence for decision-making.
        Strong signal + high confidence → weight 1.0
        Weak signal + low confidence → weight << 1.0
        """
        return self.point_estimate * self.confidence


@dataclass
class RiskAssessment:
    """
    Complete risk picture (no assumptions hidden).
    """

    value_at_risk_95: float  # 95% VaR (worst 5%)
    conditional_var_95: float  # Expected loss in tail
    probability_of_drawdown_gt_20pct: float  # P(DD > 20%)
    stress_test_loss: dict  # {scenario → loss}
    regime: str  # "bull", "bear", "neutral"
    regime_transition_prob_24h: float  # P(regime change)
    recommendation: str  # "HALT", "REDUCE", "HOLD", "INCREASE"
    confidence_in_rec: float  # 0-1


class BayesianExchangeStressModel:
    """
    Probabilistic alternative to Gate 7 (deterministic stress > 0.75).

    Model: P(exchange_failure | observed_indicators)

    Training data: Historical crises (Celsius, Luna, FTX, etc.)
    Inputs: netflow_zscore, funding_rate, basis_spread, reserve_ratio
    Output: Probability of exchange failure in next 24h
    """

    def __init__(self):
        """
        Initialize with weakly informative priors.
        Will be updated with historical crisis data.
        """
        # Prior: 5% baseline probability of major exchange issue
        # (Based on crypto history: ~5 crises/year across all exchanges)
        self.prior_probability = 0.05

        # Likelihood: How much do indicators increase probability?
        # E.g., netflow_zscore=-3.0 (extreme outflows) multiplies prob by 10x
        self.indicator_weights = {
            "netflow_zscore": {"slope": -2.0, "intercept": 1.0},  # Stronger = more negative
            "funding_rate": {"slope": 5.0, "intercept": 0.0},  # Higher rate = riskier
            "basis_spread": {"slope": 0.01, "intercept": 0.0},  # Wider spread = fragmentation
            "reserve_ratio": {"slope": -3.0, "intercept": 0.5},  # Lower reserves = riskier
        }

    def predict_failure_probability(
        self,
        netflow_zscore: float,
        funding_rate: float,
        basis_spread: float,  # bps
        reserve_ratio: float,  # 0-1
    ) -> ProbabilisticPrediction:
        """
        P(exchange_failure | indicators) using Bayesian logistic regression.

        Args:
            netflow_zscore: Z-score of exchange netflow (negative = sellers leaving)
            funding_rate: Binance funding rate in %
            basis_spread: Binance/OKX price difference in bps
            reserve_ratio: Exchange reserves / total market cap

        Returns:
            ProbabilisticPrediction with P(failure), credible interval, confidence
        """

        # Logit (log-odds) of failure
        # logit(p) = log(p / (1-p))
        logit_failure = self._compute_logit(
            netflow_zscore, funding_rate, basis_spread, reserve_ratio
        )

        # CROMWELL'S RULE: Clip logit (not probability) before transforming.
        # A Bayesian model must never assign literal certainty (P=0 or P=1) --
        # doing so means no amount of contrary evidence could ever update the belief.
        # Clipping logit to [-8, 8] bounds P to [0.00034, 0.99966], preserving
        # monotonic ordering while keeping the estimate falsifiable.
        logit_failure = float(np.clip(logit_failure, -8.0, 8.0))

        # Convert back to probability via logistic function
        # P = 1 / (1 + exp(-logit))
        prob_failure = expit(logit_failure)  # Logistic(logit) = probability

        # Uncertainty via credible interval
        # Bootstrap resampling to account for estimation uncertainty
        ci_lower, ci_upper = self._compute_credible_interval(
            netflow_zscore, funding_rate, basis_spread, reserve_ratio
        )

        # Confidence: derived from credible-interval width, not a separate
        # ad-hoc function of inputs. This is the principled approach --
        # confidence and the interval must agree with each other by
        # construction, rather than being two independent (and potentially
        # contradictory) computations over the same evidence.
        # A maximally uncertain probability estimate has CI width -> 1.0
        # (e.g. [0.0, 1.0], i.e. "could be anything"). A tight, well-evidenced
        # estimate has CI width -> 0. confidence = 1 - normalized_width.
        ci_width = ci_upper - ci_lower
        confidence = float(np.clip(1.0 - ci_width, 0.02, 0.98))

        log.info(
            "exchange_failure_probability",
            prob=prob_failure,
            ci=[ci_lower, ci_upper],
            confidence=confidence,
        )

        return ProbabilisticPrediction(
            point_estimate=prob_failure,
            lower_credible_interval=ci_lower,
            upper_credible_interval=ci_upper,
            confidence=confidence,
            epistemic_uncertainty=(ci_upper - ci_lower) / 2 / 1.96,
        )

    def _compute_logit(self, netflow: float, funding: float, basis: float, reserve: float) -> float:
        """Logit of failure probability from indicators."""
        logit = np.log(self.prior_probability / (1 - self.prior_probability))

        # Each indicator adds to logit
        logit += netflow * self.indicator_weights["netflow_zscore"]["slope"]
        logit += funding * self.indicator_weights["funding_rate"]["slope"]
        logit += basis * self.indicator_weights["basis_spread"]["slope"]
        logit += reserve * self.indicator_weights["reserve_ratio"]["slope"]

        return logit

    def _compute_credible_interval(
        self, netflow: float, funding: float, basis: float, reserve: float
    ) -> tuple[float, float]:
        """
        Bayesian credible interval via Beta-distribution approximation.

        WHY BETA, NOT NORMAL (Wald) INTERVALS:
        A symmetric normal interval (prob +/- z*se) is the standard textbook
        mistake for bounding a probability: it has poor coverage near 0/1
        (can produce nonsensical results like CI upper > 1 before clipping,
        or CI that excludes the point estimate after clipping) and the
        post-hoc clip to [0,1] is exactly the kind of papered-over assumption
        we are removing. The Beta distribution is the conjugate distribution
        for probabilities and is continuous on the OPEN interval (0,1) by
        construction -- no clipping needed, no boundary pathology.

        EFFECTIVE SAMPLE SIZE (n_eff): the interval width is controlled by
        how much relevant historical evidence informs this region of
        indicator-space. We do NOT assume uniform confidence everywhere.
        Inputs that are extreme/unusual relative to the calibration data
        (FTX, Celsius, Luna, etc. -- a small number of historical episodes)
        have LESS precedent, hence a SMALLER effective sample size and a
        WIDER interval. This directly encodes "we have less evidence about
        truly unprecedented combinations of stress indicators" rather than
        artificially growing confidence with extremity (the original bug).
        """
        logit = self._compute_logit(netflow, funding, basis, reserve)
        logit = float(np.clip(logit, -8.0, 8.0))  # Cromwell's rule, see predict_failure_probability
        prob = float(expit(logit))

        # How far is this input from the "normal/calibrated" region?
        # Each term normalized by a scale roughly matching observed historical
        # crisis magnitudes (documented in CRYPTO_INTELLIGENCE_INTEGRATION_SPEC.md).
        # UI-011: funding/basis were not wrapped in abs() here, unlike
        # netflow/reserve -- a large NEGATIVE funding rate or basis spread
        # (a real crisis signal, e.g. deeply negative funding during a
        # short squeeze) reduced `extremity` instead of increasing it,
        # which (via n_eff = base_n_eff / (1 + extremity)) made n_eff
        # LARGER -- an artificially narrower, overconfident interval for
        # exactly the kind of extreme/unprecedented input this function's
        # docstring says must widen it.
        extremity = (
            abs(netflow) / 3.0
            + abs(funding) / 0.15
            + abs(basis) / 150.0
            + abs(reserve - 0.35) / 0.35
        )

        # Baseline effective sample size ~ number of historical crisis/non-crisis
        # episodes the prior was informed by. Floors at 3 (always some irreducible
        # uncertainty; never claim near-infinite effective evidence).
        base_n_eff = 25.0
        n_eff = max(3.0, base_n_eff / (1.0 + extremity))

        alpha = max(prob * n_eff, 0.5)
        beta_param = max((1.0 - prob) * n_eff, 0.5)

        ci_lower = float(beta.ppf(0.025, alpha, beta_param))
        ci_upper = float(beta.ppf(0.975, alpha, beta_param))

        return ci_lower, ci_upper


class BayesianWhaleActivityModel:
    """
    Probabilistic whale signal with uncertainty and causal effect.

    Old: IF whale_ratio > 3.0 THEN signal is "strong buy"
    New: whale_ratio=3.0 → P(true_ratio > 2.5) = 65%, expected_impact = +1.2% vol reduction
    """

    def __init__(self):
        # Prior: Based on historical whale trading, ratio ~ 1.5 on average
        self.prior_mean = 1.5
        self.prior_std = 0.4

    def estimate_true_ratio(
        self,
        observed_ratio: float,
        sample_size: int,  # Number of whale transactions observed
        prior_mean: float | None = None,
    ) -> ProbabilisticPrediction:
        """
        Estimate TRUE whale ratio, accounting for sampling variability.

        Small sample (n=10) → huge credible interval
        Large sample (n=1000) → tight credible interval

        Combines prior belief with observed data (Bayesian update).
        """
        prior_mean = prior_mean or self.prior_mean

        # Bayesian update: posterior = weighted average of prior + likelihood
        # Weight by sample size (large n → data dominates)
        posterior_mean = (
            (prior_mean * 10)  # Prior: effective sample size = 10
            + (observed_ratio * sample_size)
        ) / (10 + sample_size)

        # Posterior uncertainty decreases with sample size
        # SE = prior_std / sqrt(1 + sample_size / prior_effective_size)
        posterior_std = self.prior_std / np.sqrt(1 + sample_size / 10)

        ci_lower = norm.ppf(0.025, posterior_mean, posterior_std)
        ci_upper = norm.ppf(0.975, posterior_mean, posterior_std)

        confidence = 1 - np.exp(-sample_size / 100)  # Confidence grows with n

        log.info(
            "whale_ratio_estimated",
            observed=observed_ratio,
            estimated_true=posterior_mean,
            sample_size=sample_size,
            confidence=confidence,
        )

        return ProbabilisticPrediction(
            point_estimate=posterior_mean,
            lower_credible_interval=ci_lower,
            upper_credible_interval=ci_upper,
            confidence=confidence,
            epistemic_uncertainty=posterior_std,
        )

    def estimate_market_impact(
        self,
        whale_ratio: float,
        market_regime: str,  # "bull", "bear", "neutral"
    ) -> dict:
        """
        Causal effect: Does whale buying actually reduce volatility?

        Different effects by regime (CATE: Conditional Average Treatment Effect).
        """
        # Effect sizes calibrated on backtesting (whale trading data)
        regime_effects = {
            "bull": {"impact": -0.015, "std": 0.008},  # -1.5% vol in bull
            "bear": {"impact": -0.005, "std": 0.012},  # -0.5% vol in bear (less effective)
            "neutral": {"impact": -0.012, "std": 0.010},  # -1.2% vol neutral
        }

        effect = regime_effects[market_regime]["impact"] * whale_ratio
        std = regime_effects[market_regime]["std"]

        return {
            "causal_effect": effect,  # Volatility reduction from whale buying
            "effect_ci_lower": effect - 1.96 * std,
            "effect_ci_upper": effect + 1.96 * std,
            "interpretation": f"Whale buying reduces volatility by {-effect:.1%} in {market_regime} regime",
        }


class BayesianRegimeDetection:
    """
    Probabilistic regime identification (replaces hard Gate 6 logic).

    Old: IF btc_dominance_zscore > 2.0 THEN regime_shift
    New: P(regime=bull | data) = 0.78, P(regime=bear | data) = 0.15, P(regime=neutral | data) = 0.07
    """

    def detect_regime(
        self,
        returns_series: pd.Series,  # Recent returns (30-90 days)
        btc_dominance: float,
        network_activity_zscore: float,
        liquidation_pressure_zscore: float,
    ) -> dict:
        """
        Posterior probability of each regime via an ORDERED LOGISTIC
        (proportional-odds) model over a continuous latent "bullishness"
        score -- NOT hard if/elif thresholds.

        WHY: the original if/elif/else scoring produced literal 0.0
        probabilities and literal 1.0 confidence whenever indicators sat
        on one side of an arbitrary cutoff. That is a Cromwell's-rule
        violation in the multi-class setting: claiming absolute certainty
        that bear-market probability is EXACTLY zero is never
        epistemically justified from four noisy indicators. The ordered
        logit model is the standard statistical tool for an ordinal
        outcome (bear < neutral < bull): it is smooth, monotonic in each
        indicator, and -- combined with a small Dirichlet smoothing term
        below -- structurally cannot output an exact 0 or 1.

        Returns:
            {"bull": 0.65, "neutral": 0.25, "bear": 0.10,
             "most_likely": "bull", "confidence": 0.65}
        """
        # NOTE: we deliberately do NOT use "mean_return * 252" (naive
        # annualization) as the return signal. For a 60-90 day lookback,
        # annualizing the raw mean multiplies its SAMPLING NOISE by the same
        # factor (252x) as its signal -- a perfectly neutral 60-day window
        # can produce wild annualized swings purely by chance, which would
        # then dominate Z and falsely classify a neutral regime as bull/bear
        # with high confidence. (Caught by stress-testing this model against
        # a synthetic neutral-regime input -- see test suite.)
        #
        # Instead we use a one-sample t-statistic: how many standard errors
        # is the observed mean away from zero, given the ACTUAL sample size
        # and observed volatility. This is the standard statistical tool for
        # "is this mean reliably different from zero" and naturally
        # downweights short/noisy windows while letting strong, sustained
        # signals (large n, consistent direction) come through clearly.
        n_obs = len(returns_series)
        if n_obs > 1:
            daily_mean = float(returns_series.mean())
            daily_std = float(returns_series.std(ddof=1))
            standard_error = daily_std / np.sqrt(n_obs) if daily_std > 0 else np.inf
            t_stat = (
                daily_mean / standard_error
                if standard_error > 0 and np.isfinite(standard_error)
                else 0.0
            )
        else:
            t_stat = 0.0

        # Standardize each indicator onto a comparable scale and combine into
        # a single latent "bullishness" score Z. Relative weights (0.4/0.3/
        # 0.2/0.1) match the indicator importances from the original design,
        # but now contribute continuously rather than through step functions.
        # t_stat of +-2 (roughly the conventional "statistically significant"
        # threshold) is mapped to one scale-unit, consistent with the other
        # already-standardized z-score inputs below.
        z_return = float(np.clip(t_stat / 2.0, -3.0, 3.0))
        z_dominance = (btc_dominance - 47.5) / 10.0  # 47.5 ~ historical BTC.D midpoint
        z_network = network_activity_zscore
        z_liquidation = -liquidation_pressure_zscore  # high liquidation pressure -> bearish

        Z = 0.4 * z_return + 0.3 * z_dominance + 0.2 * z_network + 0.1 * z_liquidation

        # Cutpoints separating bear|neutral|bull on the Z scale, calibrated
        # so that Z=0 (perfectly neutral indicators) gives roughly equal
        # probability mass to all three regimes.
        c1, c2 = -0.55, 0.55

        p_le_bear = expit(c1 - Z)  # P(regime <= bear)
        p_le_neutral = expit(c2 - Z)  # P(regime <= neutral)

        p_bear = p_le_bear
        p_neutral = max(p_le_neutral - p_le_bear, 0.0)
        p_bull = max(1.0 - p_le_neutral, 0.0)

        # Dirichlet smoothing: every regime keeps a minimum floor of
        # plausibility. This is the multi-class analogue of clipping the
        # logit in the exchange-stress model above -- it ensures the model
        # always retains some humility about regimes it currently disfavors,
        # rather than asserting impossibility from four indicators alone.
        smoothing = 0.02
        raw = np.array([p_bear, p_neutral, p_bull]) + smoothing
        raw = raw / raw.sum()

        regime_probs = {"bear": float(raw[0]), "neutral": float(raw[1]), "bull": float(raw[2])}

        most_likely = max(regime_probs, key=regime_probs.get)
        # Cap confidence below 1.0 -- structurally guaranteed by smoothing above,
        # but clip defensively in case smoothing parameters are tuned later.
        confidence = float(np.clip(regime_probs[most_likely], 0.0, 0.98))

        log.info(
            "regime_detected",
            probabilities=regime_probs,
            most_likely=most_likely,
            confidence=confidence,
        )

        return {
            "probabilities": regime_probs,
            "most_likely_regime": most_likely,
            "confidence": confidence,
        }
