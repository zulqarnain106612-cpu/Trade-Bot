"""
Model registry with shadow-mode evaluation — v4 Adaptive Regime & Model Layer.

Lets a new model version run in parallel ("shadow") against the live
model without affecting trading decisions, tracking its predictions
against realized outcomes. Promotion to live is explicit and requires
out-of-sample outperformance over a minimum evaluation window — mirrors
the strategy-promotion gauntlet philosophy from v2/v6.

Authority:
  - López de Prado (2018) AFML Ch.11 — backtest overfitting; a model must
    prove itself out-of-sample, in shadow, before touching live capital
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(slots=True)
class ShadowPrediction:
    """One shadow-model prediction paired with its eventual realized outcome."""

    predicted_prob: float
    actual_direction: int  # 1 or -1; set once outcome is known


@dataclass(slots=True)
class ShadowModelState:
    """Tracks one shadow model's predictions vs. the live model's."""

    model_id: str
    shadow_predictions: list[ShadowPrediction] = field(default_factory=list)
    live_predictions: list[ShadowPrediction] = field(default_factory=list)


class ModelRegistry:
    """
    Holds the live model_id plus zero or more shadow models under
    evaluation. Promotion swaps a shadow into the live slot only via
    explicit promote_shadow() after min_evaluations is met and the
    shadow's accuracy beats the live model's over the same window.
    """

    def __init__(self, min_evaluations: int = 100) -> None:
        if min_evaluations < 1:
            raise ValueError(f"min_evaluations must be >= 1, got {min_evaluations}")
        self._min_evaluations = min_evaluations
        self._live_model_id: str | None = None
        self._shadows: dict[str, ShadowModelState] = {}

    def set_live_model(self, model_id: str) -> None:
        self._live_model_id = model_id

    @property
    def live_model_id(self) -> str | None:
        return self._live_model_id

    def register_shadow(self, model_id: str) -> None:
        if model_id in self._shadows:
            raise ValueError(f"shadow model_id {model_id!r} already registered")
        self._shadows[model_id] = ShadowModelState(model_id=model_id)

    def record_shadow_prediction(
        self, model_id: str, predicted_prob: float, actual_direction: int
    ) -> None:
        if model_id not in self._shadows:
            raise KeyError(f"shadow model_id {model_id!r} not registered")
        self._shadows[model_id].shadow_predictions.append(
            ShadowPrediction(predicted_prob, actual_direction)
        )

    def record_live_prediction_for_comparison(
        self, model_id: str, predicted_prob: float, actual_direction: int
    ) -> None:
        """Records the live model's prediction on the same bar, for a fair comparison."""
        if model_id not in self._shadows:
            raise KeyError(f"shadow model_id {model_id!r} not registered")
        self._shadows[model_id].live_predictions.append(
            ShadowPrediction(predicted_prob, actual_direction)
        )

    @staticmethod
    def _accuracy(predictions: list[ShadowPrediction]) -> float:
        if not predictions:
            return 0.0
        correct = sum(
            1 for p in predictions if (1 if p.predicted_prob > 0.5 else -1) == p.actual_direction
        )
        return correct / len(predictions)

    def evaluate_shadow(self, model_id: str) -> tuple[bool, str]:
        """
        Returns (ready_to_promote, reason). Never mutates state — promotion
        is a separate explicit call so a human/automation layer can gate it.
        """
        if model_id not in self._shadows:
            raise KeyError(f"shadow model_id {model_id!r} not registered")
        state = self._shadows[model_id]
        if len(state.shadow_predictions) < self._min_evaluations:
            return False, (
                f"insufficient evaluations ({len(state.shadow_predictions)} < "
                f"{self._min_evaluations})"
            )
        shadow_acc = self._accuracy(state.shadow_predictions)
        live_acc = self._accuracy(state.live_predictions)
        if shadow_acc <= live_acc:
            return False, f"shadow accuracy {shadow_acc:.3f} does not beat live {live_acc:.3f}"
        return True, f"shadow accuracy {shadow_acc:.3f} beats live {live_acc:.3f}"

    def promote_shadow(self, model_id: str) -> None:
        """
        Explicit promotion — swaps model_id into the live slot and removes
        it from the shadow set. Callers must have already checked
        evaluate_shadow() returns ready=True; this method does not
        re-validate, matching the strategy kill-switch's re_enable() pattern.
        """
        if model_id not in self._shadows:
            raise KeyError(f"shadow model_id {model_id!r} not registered")
        self._live_model_id = model_id
        del self._shadows[model_id]

    def discard_shadow(self, model_id: str) -> None:
        """
        Drops a shadow without promoting it. Used when a candidate has been
        evaluated long enough without beating the incumbent, or when a newer
        candidate supersedes it. Never touches the live slot — a discard must
        not be able to change what is trading.
        """
        if model_id not in self._shadows:
            raise KeyError(f"shadow model_id {model_id!r} not registered")
        del self._shadows[model_id]

    def evaluation_count(self, model_id: str) -> int:
        """Resolved shadow predictions recorded so far for `model_id`."""
        if model_id not in self._shadows:
            raise KeyError(f"shadow model_id {model_id!r} not registered")
        return len(self._shadows[model_id].shadow_predictions)

    def accuracies(self, model_id: str) -> tuple[float, float]:
        """(shadow_accuracy, live_accuracy) over the recorded window — for audit records."""
        if model_id not in self._shadows:
            raise KeyError(f"shadow model_id {model_id!r} not registered")
        state = self._shadows[model_id]
        return self._accuracy(state.shadow_predictions), self._accuracy(state.live_predictions)

    def shadow_ids(self) -> list[str]:
        return list(self._shadows.keys())


_registry: ModelRegistry = ModelRegistry()


def get_model_registry() -> ModelRegistry:
    """Module-level singleton for the v4 model registry."""
    return _registry
