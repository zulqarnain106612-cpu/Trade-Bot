"""
Adapter — wraps the existing model-driven SignalEngine as a registry
strategy so it participates in the v2 multi-strategy portfolio without
any change to its internals (Sub-task 1: "existing strategies wrapped
first so nothing regresses").

This is the bridge, not a new strategy: all XGBoost/HMM/Kelly logic stays
in src/engine/signal_engine.py exactly as-is.
"""

from __future__ import annotations

from src.engine.signal_engine import SignalResult
from src.strategies.registry import Signal

STRATEGY_ID_SIGNAL_ENGINE: str = "signal_engine_v1"


class SignalEngineStrategy:
    """
    Wraps a precomputed ``SignalResult`` as a registry ``Signal``.

    The underlying SignalEngine is async and stateful (storage, models),
    unlike the pure-function strategies added in later sub-tasks, so this
    adapter does not call it directly — the orchestrator still drives the
    engine's tick and passes the resulting ``SignalResult`` in here for
    translation into the common Signal currency.
    """

    strategy_id: str = STRATEGY_ID_SIGNAL_ENGINE

    def __init__(self, max_capital_fraction: float = 1.0) -> None:
        if not 0.0 < max_capital_fraction <= 1.0:
            raise ValueError(f"max_capital_fraction must be in (0, 1], got {max_capital_fraction}")
        self._max_capital_fraction = max_capital_fraction
        self._last_result: SignalResult | None = None

    def submit_result(self, result: SignalResult) -> None:
        """Feed the latest SignalEngine tick output prior to generate_signal()."""
        self._last_result = result

    def generate_signal(self, bar: object) -> Signal:  # - bar unused, result-driven
        result = self._last_result
        if result is None or not result.tradeable:
            return Signal(direction=0, confidence=0.0, regime_fit=0.0)

        direction = 1 if result.direction == 1 else -1
        regime_fit = 1.0 if result.gate_result is None else float(result.gate_result.passed)
        return Signal(
            direction=direction,
            confidence=max(0.0, min(1.0, result.p_bet)),
            regime_fit=max(0.0, min(1.0, regime_fit)),
        )

    def required_capital_fraction(self) -> float:
        return self._max_capital_fraction
