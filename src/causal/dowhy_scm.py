"""
DoWhy Structural Causal Model (SCM) — do-calculus causal effect estimation.

Answers causal questions that correlation-based models cannot:
  - Does whale selling CAUSE volatility increases?
  - What is the DIRECT effect of liquidations on price (not mediated by vol)?
  - Would reducing position size CAUSE better P&L?

Uses DoWhy (Pearl 2009) with a predefined causal graph:
  liquidations → volatility → price
  whale_flow → price (direct)
  funding → open_interest → price

The graph is expressed in GML format and stored as a class attribute.
"""

from __future__ import annotations

import warnings
from dataclasses import dataclass

import pandas as pd
import structlog


log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

# Causal graph in DOT notation (GML-like, accepted by DoWhy)
_CAUSAL_GRAPH = """
digraph {
    liquidations -> volatility;
    volatility -> price;
    whale_flow -> price;
    whale_flow -> volatility;
    funding_rate -> open_interest;
    open_interest -> price;
    macro_score -> price;
    sentiment -> volatility;
    sentiment -> price;
}
"""


@dataclass
class CausalEstimate:
    treatment: str
    outcome: str
    ate: float  # Average Treatment Effect
    confidence: float  # Refutation test p-value proxy
    method: str


class DoWhySCM:
    """
    DoWhy-based SCM for crypto market causal inference.

    Wraps the DoWhy CausalModel API with a predefined graph and exposes
    `estimate_effect()` for arbitrary treatment-outcome pairs.
    """

    def __init__(self) -> None:
        self._available = False
        try:
            import dowhy  # type: ignore[import]  # noqa: F401

            self._available = True
            log.info("dowhy_scm_ready")
        except ImportError:
            log.warning("dowhy_not_installed_causal_layer_disabled")

    def estimate_effect(
        self,
        data: pd.DataFrame,
        treatment: str,
        outcome: str,
        method: str = "backdoor.linear_regression",
    ) -> CausalEstimate:
        """
        Estimate the causal effect of `treatment` on `outcome` using do-calculus.

        data: DataFrame with columns matching the causal graph node names.
        treatment: variable name (e.g. 'liquidations')
        outcome: variable name (e.g. 'price')

        Returns CausalEstimate with ATE and method used.
        """
        if not self._available or data.empty:
            return CausalEstimate(
                treatment=treatment, outcome=outcome, ate=0.0, confidence=0.0, method="unavailable"
            )
        try:
            from dowhy import CausalModel  # type: ignore[import]

            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                model = CausalModel(
                    data=data,
                    treatment=treatment,
                    outcome=outcome,
                    graph=_CAUSAL_GRAPH,
                )
                identified = model.identify_effect(proceed_when_unidentifiable=True)
                estimate = model.estimate_effect(
                    identified,
                    method_name=method,
                    test_significance=False,
                )
                # Refutation: placebo test
                try:
                    refutation = model.refute_estimate(
                        identified, estimate, method_name="placebo_treatment_refuter"
                    )
                    confidence = float(getattr(refutation, "p_value", 0.5))
                except Exception:
                    confidence = 0.5

                ate = float(estimate.value) if estimate.value is not None else 0.0
                return CausalEstimate(
                    treatment=treatment,
                    outcome=outcome,
                    ate=ate,
                    confidence=confidence,
                    method=method,
                )
        except Exception as exc:
            log.warning("dowhy_estimate_failed", treatment=treatment, outcome=outcome, exc=str(exc))
            return CausalEstimate(
                treatment=treatment, outcome=outcome, ate=0.0, confidence=0.0, method="failed"
            )

    def batch_estimate(
        self,
        data: pd.DataFrame,
        pairs: list[tuple[str, str]],
    ) -> list[CausalEstimate]:
        """Estimate multiple treatment-outcome pairs from the same dataset."""
        return [self.estimate_effect(data, t, o) for t, o in pairs]

    def causal_signal(self, data: pd.DataFrame) -> dict[str, float]:
        """
        Run the standard set of causal queries and return a signal dict.

        Returns dict with ATE values for the key causal relationships.
        """
        pairs = [
            ("liquidations", "price"),
            ("whale_flow", "price"),
            ("funding_rate", "price"),
            ("sentiment", "price"),
        ]
        estimates = self.batch_estimate(data, pairs)
        return {f"ate_{e.treatment}_on_{e.outcome}": e.ate for e in estimates}
