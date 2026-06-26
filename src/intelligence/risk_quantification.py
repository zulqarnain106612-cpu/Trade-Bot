"""
Risk quantification and uncertainty analysis.

Measures: VaR, CVaR, stress testing, uncertainty decomposition.

Authority: Jorion (2006) Value at Risk, Dowd (2007) Measuring Market Risk
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

import numpy as np
import pandas as pd
from scipy.stats import norm, t
import structlog

log = structlog.get_logger(__name__)


@dataclass
class RiskMetrics:
    """
    Complete risk assessment (no hidden assumptions).
    """
    
    value_at_risk_95: float                # 95% VaR (worst 5% loss)
    conditional_var_95: float              # Expected loss in tail
    prob_drawdown_20pct: float             # P(DD > 20%)
    prob_drawdown_50pct: float             # P(ruin, DD > 50%)
    sharpe_credible_interval: tuple        # Sharpe [lower, upper]
    max_loss_scenario: dict                # Stress scenario → loss
    regime_prob: dict                      # {"bull": 0.6, "bear": 0.3, ...}
    regime_transition_prob: float          # P(regime change 24h)
    recommendation: str                    # "HALT", "REDUCE", "HOLD", "INCREASE"
    confidence_in_rec: float               # 0-1


class RiskQuantifier:
    """
    Rigorous risk measurement.
    """
    
    def __init__(self, lookback_days: int = 90):
        self.lookback_days = lookback_days
        self.historical_returns = None
    
    def value_at_risk(
        self,
        returns: np.ndarray,
        confidence_level: float = 0.95,
        method: str = "historical",  # "historical", "parametric", "montecarlo"
    ) -> dict:
        """
        Value-at-Risk: "95% chance loss < X%"
        
        Multiple methods to cross-validate:
        1. Historical: Empirical quantile
        2. Parametric: Assume distribution (normal, t, etc.)
        3. Monte Carlo: Simulate market paths
        """
        
        if method == "historical":
            var = np.quantile(returns, 1 - confidence_level)
            
        elif method == "parametric":
            # Assume Student-t distribution (handles fat tails better than normal)
            mean, std = returns.mean(), returns.std()
            df = len(returns) - 1  # Degrees of freedom
            var = mean + std * t.ppf(1 - confidence_level, df)
            
        elif method == "montecarlo":
            # Simulate 10,000 market paths
            var = self._monte_carlo_var(returns, confidence_level)
        
        else:
            raise ValueError(f"Unknown method: {method}")
        
        # Conditional VaR: Expected loss given tail event
        tail_returns = returns[returns <= var]
        cvar = tail_returns.mean() if len(tail_returns) > 0 else var * 1.25
        
        return {
            "var": var,
            "cvar": cvar,
            "interpretation": f"{confidence_level:.0%} chance loss < {-var:.2%}, "
                            f"tail loss {-cvar:.2%}",
            "method": method,
        }
    
    def stress_test(
        self,
        current_price: float,
        current_volatility: float,
        scenarios: Optional[dict] = None,
    ) -> dict:
        """
        "What happens in extreme scenarios?"
        
        Pre-defined scenarios:
        1. 30% BTC crash (2018, 2022)
        2. 50% liquidation cascade (Luna, FTX)
        3. Exchange insolvency (Celsius, Voyager)
        4. Contagion (all exchanges deleverage)
        """
        
        if scenarios is None:
            scenarios = {
                "btc_crash_30pct": {"price_change": -0.30},
                "liquidation_cascade": {"slippage": -0.10},
                "exchange_insolvency": {"liquidity_loss": -0.20},
                "contagion": {"correlation_shock": 0.95},  # All assets move together
            }
        
        results = {}
        for scenario_name, params in scenarios.items():
            loss = self._simulate_scenario(current_price, params)
            results[scenario_name] = {"loss": loss, "severity": "high" if loss < -0.20 else "medium"}
        
        return results
    
    def estimate_probability_of_ruin(
        self,
        initial_capital: float,
        daily_returns: np.ndarray,
        drawdown_threshold: float = 0.50,  # 50% loss = ruin
    ) -> dict:
        """
        Probability of catastrophic loss (> drawdown_threshold).
        
        Uses: Actual drawdown history + extreme value theory.
        """
        
        # Empirical probability: % of days with loss > threshold
        empirical_prob = (daily_returns < -drawdown_threshold).sum() / len(daily_returns)
        
        # Parametric estimate using generalized Pareto distribution (tail model)
        tail_returns = daily_returns[daily_returns < np.quantile(daily_returns, 0.10)]
        if len(tail_returns) > 10:
            # Fit GPD to extreme losses
            u = np.quantile(daily_returns, 0.90)
            n_excess = (daily_returns < u).sum()
            prob_exceedance = n_excess / len(daily_returns)
            
            # Estimate P(loss > threshold | in tail)
            if prob_exceedance > 0:
                gpd_prob = prob_exceedance * (1 - np.exp(-len(tail_returns) * 0.01))
            else:
                gpd_prob = empirical_prob
        else:
            gpd_prob = empirical_prob
        
        # Use average of empirical + parametric
        prob_ruin = (empirical_prob + gpd_prob) / 2
        
        return {
            "probability": prob_ruin,
            "percentile_equivalent": f"1 in {1/prob_ruin:.0f} days",
            "confidence": "moderate" if len(tail_returns) > 30 else "low",
        }
    
    def uncertainty_decomposition(
        self,
        predictions: np.ndarray,       # Model predictions
        targets: np.ndarray,           # Actual values
        ensemble_members: list = None, # Individual model predictions
    ) -> dict:
        """
        Decompose total prediction error into components:
        - Aleatoric: Irreducible (market noise, luck)
        - Epistemic: Reducible (learnable, with more data/better model)
        """
        
        mse = np.mean((predictions - targets)**2)
        rmse = np.sqrt(mse)
        
        if ensemble_members is not None:
            # Aleatoric: Average individual model variance
            individual_errors = [
                np.mean((m - targets)**2) for m in ensemble_members
            ]
            aleatoric = np.mean(individual_errors)
            
            # Epistemic: Variance across ensemble predictions
            ensemble_variance = np.var(ensemble_members, axis=0).mean()
            epistemic = ensemble_variance
        else:
            # No ensemble, estimate from residuals
            aleatoric = np.var(predictions - targets)
            epistemic = 0.0  # Can't quantify without ensemble
        
        total = aleatoric + epistemic
        
        return {
            "total_rmse": rmse,
            "aleatoric_rmse": np.sqrt(aleatoric),
            "epistemic_rmse": np.sqrt(epistemic),
            "aleatoric_pct": aleatoric / total if total > 0 else 0.5,
            "epistemic_pct": epistemic / total if total > 0 else 0.5,
            "interpretation": f"{aleatoric/total:.0%} irreducible noise, "
                            f"{epistemic/total:.0%} model disagreement",
        }
    
    def _monte_carlo_var(
        self,
        historical_returns: np.ndarray,
        confidence_level: float = 0.95,
        num_simulations: int = 10000,
    ) -> float:
        """Estimate VaR via Monte Carlo simulation."""
        
        mean, std = historical_returns.mean(), historical_returns.std()
        
        # Simulate 10k returns from fitted distribution
        simulated_returns = np.random.normal(mean, std, num_simulations)
        
        # VaR = quantile
        var = np.quantile(simulated_returns, 1 - confidence_level)
        return var
    
    def _simulate_scenario(
        self,
        current_price: float,
        scenario_params: dict,
    ) -> float:
        """Simulate loss under scenario."""
        
        loss = 0.0
        if "price_change" in scenario_params:
            loss += current_price * scenario_params["price_change"]
        if "slippage" in scenario_params:
            loss += current_price * scenario_params["slippage"]
        if "liquidity_loss" in scenario_params:
            loss += current_price * scenario_params["liquidity_loss"]
        
        return loss / current_price if current_price != 0 else loss
