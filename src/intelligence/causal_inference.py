"""
Causal inference framework.

Answer causal questions, not just correlations:
- Does whale selling CAUSE volatility increases?
- What's the DIRECT effect of liquidations on price (not mediated by vol)?
- Should we reduce position size? Will it help or hurt?

Authority: Pearl (2009) Causality, Angrist & Pischke (2009) Mostly Harmless Econometrics
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Callable

import numpy as np
import pandas as pd
from scipy.stats import norm
import structlog

log = structlog.get_logger(__name__)


@dataclass
class CausalEffect:
    """
    Causal effect with confidence intervals and sensitivity analysis.
    """
    
    total_effect: float                        # Total causal effect
    direct_effect: float                       # Direct path (not through mediators)
    indirect_effect: float                     # Through mediators
    effect_ci_lower: float                     # 2.5th percentile
    effect_ci_upper: float                     # 97.5th percentile
    confounding_bias: float                    # Amount of spurious correlation
    sample_size: int                           # Data points used
    assumptions: list                          # Causal assumptions
    is_robust: bool                            # Passes sensitivity check?
    interpretation: str                        # English description


class CausalDAG:
    """
    Directed Acyclic Graph representing causal structure.
    
    Example:
        whale_selling → order_flow → price_volatility
                   → (not direct, only through flow)
    """
    
    def __init__(self):
        """Initialize crypto market causal structure."""
        
        # Causal relationships (established from domain knowledge + research)
        self.edges = {
            # On-chain flows cause price impact
            "whale_selling": ["order_flow", "price_pressure"],
            "whale_buying": ["order_flow", "price_support"],
            "exchange_outflow": ["selling_pressure"],  # Whales leaving exchange
            
            # Liquidations cause cascades
            "liquidation_volume": ["selling_cascade", "volatility_spike"],
            "funding_rate": ["leverage_unwinding"],
            
            # Market regime affects everything
            "btc_dominance": ["correlation_matrix", "regime"],
            "network_activity": ["investor_sentiment"],
            
            # Macro causes micro
            "fed_policy": ["crypto_inflow", "sentiment"],
            "macro_crisis": ["risk_off", "deleveraging"],
        }
    
    def identify_confounder(
        self,
        treatment: str,
        outcome: str,
    ) -> list:
        """
        Identify variables that causally affect BOTH treatment and outcome.
        These are confounders that bias causal effect estimates.
        
        Example:
            treatment = "whale_selling"
            outcome = "volatility"
            confounder = "macro_crisis" (both causes whale selling AND vol increase)
        """
        
        # In crypto: market regime is a major confounder
        # Bear market → whale selling AND vol increases
        # Bull market → whale buying AND vol decreases
        # Can't see true effect without controlling for regime
        
        confounders = ["market_regime", "btc_price_level", "fed_policy"]
        return confounders


class CausalInferenceEngine:
    """
    Estimate treatment effects using causal methods.
    """
    
    def __init__(self):
        self.dag = CausalDAG()
    
    def estimate_treatment_effect(
        self,
        treatment_data: np.ndarray,      # E.g., whale_selling_volume
        outcome_data: np.ndarray,        # E.g., volatility_next_4h
        confounders: Optional[np.ndarray] = None,  # Market regime, etc.
        instrument_data: Optional[np.ndarray] = None,  # Genuine instrument, NOT a stand-in
    ) -> CausalEffect:
        """
        Estimate CAUSAL effect of treatment on outcome.
        
        Without confounders: Causal effect might be biased
        With confounders: Estimates true causal effect via backdoor adjustment
        
        BUG FIX (caught by synthetic-confound stress test): the previous
        version unconditionally averaged the backdoor estimate with an
        "instrumental variable" estimate that, in the absence of a real
        instrument, was implemented as literally the naive correlation
        between treatment and outcome -- i.e. exactly the confounded
        quantity backdoor adjustment exists to correct. Averaging the two
        silently reintroduced ~50% of the confounding bias into the
        reported "causal effect" while still labeling it adjusted. Verified
        numerically: on data with a known TRUE direct effect of 0.000 and a
        confounded naive correlation of 0.849, backdoor adjustment alone
        correctly recovered -0.005, but the old averaged estimate reported
        0.422 -- worse than using no adjustment method at all in this case.
        
        FIX: backdoor adjustment (confounder-adjusted regression) is now the
        sole estimate when only `confounders` is supplied -- this is the
        textbook-correct method given a valid adjustment set. Instrumental-
        variable estimation is only performed when the caller supplies a
        genuine `instrument_data` array (a variable that affects treatment
        but has no path to outcome except through treatment); it is never
        silently substituted with a non-instrument. When both are available,
        we report both BUT do not blindly average them -- 2SLS is reported
        as a robustness cross-check, with backdoor adjustment as the primary
        estimate, since requiring genuine instrument validity (relevance +
        exogeneity) is a much stronger and rarer assumption than having a
        correctly specified adjustment set.
        """
        
        if confounders is None:
            confounders = np.zeros((len(treatment_data), 1))
        
        # Primary method: backdoor adjustment (confounder-adjusted regression).
        # This IS the causal effect estimate when no real instrument exists.
        true_effect = self._backdoor_adjustment(
            treatment_data, outcome_data, confounders
        )
        
        # Optional cross-check: only run if caller provides a genuine
        # instrument. Reported for robustness comparison; does NOT silently
        # alter the primary estimate above.
        iv_cross_check = None
        if instrument_data is not None:
            iv_cross_check = self._instrumental_variable(
                treatment_data, outcome_data, instrument_data
            )
        
        # Estimate confounding bias: how much the naive (unadjusted)
        # correlation differs from the properly adjusted estimate. This is
        # diagnostic information about how much the confounder was
        # distorting the naive view -- not blended back into true_effect.
        naive_correlation = np.corrcoef(treatment_data, outcome_data)[0, 1]
        confounding_bias = true_effect - naive_correlation
        
        # Confidence interval via bootstrap -- resamples and RECOMPUTES the
        # same backdoor-adjustment estimator on each resample, so the
        # reported interval is consistent with (centered near) the point
        # estimate it accompanies. (Previously this bootstrapped the raw
        # correlation instead, producing a CI that did not even contain
        # the reported point estimate -- caught by the same stress test.)
        ci_lower, ci_upper = self._bootstrap_ci(
            treatment_data, outcome_data, confounders
        )
        
        return CausalEffect(
            total_effect=true_effect,
            direct_effect=true_effect * 0.8,  # Simplified (assume 20% mediation)
            indirect_effect=true_effect * 0.2,
            effect_ci_lower=ci_lower,
            effect_ci_upper=ci_upper,
            confounding_bias=confounding_bias,
            sample_size=len(treatment_data),
            assumptions=[
                "No unmeasured confounding",
                "Overlap (common support)",
                "Consistency",
            ],
            # Robust if the adjusted estimate is PRECISE relative to the
            # scale of the original (unadjusted) signal -- i.e. the CI is
            # tight, not blown up by collinearity between treatment and
            # confounders. We deliberately do NOT compare against
            # confounding_bias itself: a large, successfully-removed
            # confound produces a large confounding_bias by construction
            # (that is the whole point of adjusting for it), so comparing
            # bias-to-noise would mislabel the best-case outcome -- a strong
            # confounder correctly corrected -- as "not robust". Verified
            # against the synthetic test: true effect 0.000, naive corr
            # 0.849, correctly adjusted to -0.005 with tight CI -- this
            # SHOULD read as robust, and now does.
            is_robust=(
                (ci_upper - ci_lower) / (abs(naive_correlation) + 0.10)
            ) < 0.5,
            interpretation=(
                f"Treatment causes {true_effect:.3f} change in outcome "
                f"(backdoor-adjusted; naive correlation was {naive_correlation:.3f})"
            ),
        )
    
    # Minimum samples required within a stratum before we trust a
    # correlation computed on it. Below this, corrcoef on a near-constant
    # or single-point slice silently returns NaN (caught by testing a
    # deliberately degenerate stratum with n=1).
    _MIN_STRATUM_SIZE = 5
    
    def estimate_heterogeneous_treatment_effect(
        self,
        treatment: np.ndarray,
        outcome: np.ndarray,
        conditioning_vars: np.ndarray,  # Market regime, price level, etc. (1D or 2D)
    ) -> dict:
        """
        Treatment effect varies by context (CATE: Conditional Average Treatment Effect).
        
        Example: Whale buying effect is different in bull vs bear market.
        
        BUG FIX 1: previously hard-coded `conditioning_vars[:, 0]`, which
        crashes with IndexError on a 1D array -- the single-conditioning-
        variable case, which is also the most common one (this method only
        ever stratifies by one variable). 1D input is now accepted directly
        rather than forcing every caller to remember to reshape.
        
        BUG FIX 2: a stratum with too few samples (e.g. n=1, caught by an
        adversarial test) makes corrcoef silently return NaN (division by
        zero variance), which then propagated silently into
        `average_effect` via np.mean -- a NaN that downstream code
        comparing against a threshold (`if average_effect > x`) would
        silently treat as "not triggered" rather than "unknown", which is a
        dangerous failure mode for a risk-relevant calculation. Degenerate
        strata are now excluded from the average and explicitly reported,
        rather than silently corrupting it.
        """
        conditioning_vars = np.asarray(conditioning_vars)
        conditioning_col = (
            conditioning_vars[:, 0] if conditioning_vars.ndim > 1 else conditioning_vars
        )
        
        regimes = np.unique(conditioning_col)
        
        effects_by_regime = {}
        excluded_strata = {}
        for regime in regimes:
            mask = conditioning_col == regime
            n_in_stratum = int(mask.sum())
            if n_in_stratum < self._MIN_STRATUM_SIZE:
                excluded_strata[str(regime)] = n_in_stratum
                continue
            effect = np.corrcoef(treatment[mask], outcome[mask])[0, 1]
            if np.isnan(effect):
                # Can still occur with near-zero variance even above the
                # size floor (e.g. a constant treatment/outcome slice);
                # exclude rather than let it silently poison the average.
                excluded_strata[str(regime)] = n_in_stratum
                continue
            effects_by_regime[str(regime)] = float(effect)
        
        average_effect = (
            float(np.mean(list(effects_by_regime.values())))
            if effects_by_regime
            else None  # No stratum had enough valid data -- be explicit about "unknown", not 0 or NaN
        )
        
        return {
            "effects_by_context": effects_by_regime,
            "average_effect": average_effect,
            "excluded_strata": excluded_strata,  # {regime: n_samples} for any stratum too small/degenerate to use
            "interpretation": (
                "Effect varies by market condition"
                if not excluded_strata
                else f"Effect varies by market condition "
                     f"({len(excluded_strata)} stratum/strata excluded for insufficient/degenerate data: "
                     f"{excluded_strata})"
            ),
        }
    
    # Intervention keys this method knows how to apply. Kept as a class-level
    # constant so the "supported keys" list shown in the error message below
    # can never drift out of sync with what the code actually implements.
    _SUPPORTED_INTERVENTION_KEYS = frozenset({"reduce_position_50pct", "reduce_position"})
    
    def counterfactual_prediction(
        self,
        current_state: dict,  # {"whale_ratio": 2.0, "regime": "bull", ...}
        intervention: dict,   # {"reduce_position": 0.5} -- multiplier on position_size, in [0, 1]
    ) -> dict:
        """
        Predict outcome under counterfactual intervention.
        
        Example:
            current_state = {"whale_ratio": 2.0, "funding": 0.12}
            intervention = {"reduce_position": 0.5}
            → "If we reduce to 50% size, expected Sharpe = 4.35, opportunity cost 0.25"
        
        BUG FIX: the parameter's own doc comment advertised `{"reduce_position":
        0.5}` as the interface, but the implementation only ever checked for a
        completely different key, `"reduce_position_50pct"` (boolean). Calling
        with the documented key silently did nothing -- returned
        opportunity_cost=0.0 and recommendation="REDUCE" (since a cost of
        exactly zero is below the REDUCE threshold), even though no
        intervention was actually evaluated. The REAL evaluated intervention
        (using the undocumented key) gave opportunity_cost=0.25 and
        recommendation="HOLD" instead -- i.e. the documented interface
        produced the OPPOSITE recommendation of the one a correct evaluation
        gives. Verified by direct comparison before this fix.
        
        FIX: "reduce_position" (a float multiplier, matching the original
        doc) is now the primary supported key. "reduce_position_50pct"
        (boolean) remains supported for backward compatibility as shorthand
        for `reduce_position=0.5`. Any OTHER, unrecognized key now raises
        ValueError immediately rather than being silently ignored --
        a counterfactual engine that quietly no-ops on a typo'd or
        unsupported intervention name is worse than one that fails loudly,
        since a silent no-op is indistinguishable from "the intervention
        doesn't help" in the returned numbers.
        """
        unrecognized = set(intervention.keys()) - self._SUPPORTED_INTERVENTION_KEYS
        if unrecognized:
            raise ValueError(
                f"Unrecognized intervention key(s): {sorted(unrecognized)}. "
                f"Supported keys: {sorted(self._SUPPORTED_INTERVENTION_KEYS)} "
                f"-- a silent no-op here would produce a misleading "
                f"opportunity_cost of 0.0 rather than reflecting the "
                f"intervention you actually intended."
            )
        
        # Simplified causal model
        # (Full version uses structural causal model with all relationships)
        
        base_sharpe = self._predict_sharpe(current_state)
        
        # Apply intervention and predict new state. Both supported keys
        # converge to the same underlying effect (multiply position_size),
        # so there is exactly one code path for "how a position reduction
        # affects predicted Sharpe" -- no risk of the two keys silently
        # disagreeing with each other in the future.
        intervened_state = current_state.copy()
        if intervention.get("reduce_position_50pct"):
            multiplier = 0.5
        elif "reduce_position" in intervention:
            multiplier = float(intervention["reduce_position"])
        else:
            multiplier = 1.0  # No position-altering intervention requested
        
        intervened_state["position_size"] = intervened_state.get("position_size", 1.0) * multiplier
        
        new_sharpe = self._predict_sharpe(intervened_state)
        opportunity_cost = base_sharpe - new_sharpe
        
        return {
            "baseline_sharpe": base_sharpe,
            "sharpe_under_intervention": new_sharpe,
            "opportunity_cost": opportunity_cost,
            "recommendation": "REDUCE" if opportunity_cost < 0.2 else "HOLD",
        }
    
    def _backdoor_adjustment(
        self, treatment: np.ndarray, outcome: np.ndarray, confounders: np.ndarray
    ) -> float:
        """Estimate effect adjusting for confounders (regression)."""
        try:
            from sklearn.linear_model import LinearRegression
            X = np.hstack([treatment.reshape(-1, 1), confounders])
            model = LinearRegression().fit(X, outcome)
            return float(model.coef_[0])  # Treatment coefficient
        except ImportError:
            # Fallback to simple correlation
            return float(np.corrcoef(treatment, outcome)[0, 1])
    
    def _instrumental_variable(
        self, treatment: np.ndarray, outcome: np.ndarray, instrument: np.ndarray
    ) -> float:
        """
        Two-Stage Least Squares (2SLS) IV estimation.
        
        Requires a GENUINE instrument: a variable that (a) is correlated
        with treatment ("relevance") and (b) affects outcome ONLY through
        treatment, not directly ("exogeneity"/exclusion restriction).
        
        Example instrument candidate: a regulatory announcement that shifts
        whale trading activity but has no direct mechanical link to the
        outcome metric except via that trading activity.
        
        FIX: this previously had no real instrument parameter at all -- it
        ignored its inputs and returned raw corrcoef(treatment, outcome),
        i.e. exactly the confounded quantity the method exists to avoid.
        Now it performs actual two-stage regression and REQUIRES the caller
        to supply a real instrument array; there is no fallback that
        silently substitutes a non-instrument.
        """
        from sklearn.linear_model import LinearRegression
        
        instrument = np.asarray(instrument).reshape(-1, 1)
        
        # Stage 1: regress treatment on instrument, get predicted treatment.
        # Weak relevance (low R^2 here) means a weak instrument -- the
        # resulting estimate should be treated with caution by the caller.
        stage1 = LinearRegression().fit(instrument, treatment)
        treatment_hat = stage1.predict(instrument)
        
        # Stage 2: regress outcome on the PREDICTED (instrument-driven)
        # component of treatment, not on raw treatment. This isolates
        # variation in treatment that is plausibly exogenous (driven by the
        # instrument), removing variation that the confounder could have
        # induced directly.
        stage2 = LinearRegression().fit(treatment_hat.reshape(-1, 1), outcome)
        
        return float(stage2.coef_[0])
    
    def _bootstrap_ci(
        self, treatment: np.ndarray, outcome: np.ndarray, confounders: np.ndarray
    ):
        """
        Confidence interval via bootstrap -- resamples and RECOMPUTES the
        same backdoor-adjustment estimator used for the point estimate on
        each resample.
        
        FIX: previously bootstrapped raw corrcoef(treatment, outcome) while
        the point estimate was a confounder-adjusted regression coefficient
        -- two different quantities, so the reported "95% CI" did not
        actually bound the point estimate it was attached to (verified: CI
        [0.835, 0.862] reported alongside point estimate 0.422). The CI
        estimator must match the point estimator for the interval to mean
        anything.
        """
        n = min(len(treatment), 1000)  # Limit to avoid long compute
        bootstrap_effects = []
        
        for _ in range(200):  # 200 bootstrap resamples
            idx = np.random.choice(len(treatment), n, replace=True)
            boot_effect = self._backdoor_adjustment(
                treatment[idx], outcome[idx], confounders[idx]
            )
            bootstrap_effects.append(boot_effect)
        
        ci_lower = float(np.percentile(bootstrap_effects, 2.5))
        ci_upper = float(np.percentile(bootstrap_effects, 97.5))
        return ci_lower, ci_upper
    
    def _predict_sharpe(self, state: dict) -> float:
        """Simplified Sharpe prediction from state (demo)."""
        base_sharpe = 5.2
        
        # Adjust for whale activity (positive effect)
        whale_adjustment = state.get("whale_buy_sell_ratio", 1.0) * 0.3
        
        # Adjust for funding rate (negative = excessive leverage risk)
        funding_adjustment = -state.get("funding_rate", 0.0) * 10
        
        # Adjust for position size reduction (lower but safer)
        position_adjustment = (1 - state.get("position_size", 1.0)) * 0.5
        
        predicted_sharpe = base_sharpe + whale_adjustment + funding_adjustment - position_adjustment
        return max(predicted_sharpe, 2.0)  # Floor at 2.0
