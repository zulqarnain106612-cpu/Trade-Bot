"""
Strategy registry — v2 multi-strategy portfolio engine, Sub-task 1.

Defines the contract every strategy must satisfy to participate in the
portfolio, plus a registry that strategies plug into without the
orchestrator needing to know each strategy's concrete type.

A strategy is any object implementing ``StrategyProtocol``:
  - ``strategy_id``            : unique, stable identifier (used for
                                  attribution, kill-switch, correlation
                                  tracking — never reused across strategies)
  - ``generate_signal(bar)``   : pure function of a bar/context -> Signal
  - ``required_capital_fraction()`` : upper bound on capital this strategy
                                  may request, enforced independently of
                                  Kelly (Kelly is a ceiling, not a target)

``Signal`` is intentionally narrower than ``SignalEngine.SignalResult`` —
it is the common currency every strategy family (existing model-driven
signal engine, and the new mean-reversion/carry/breakout/cross-sectional
strategies) must reduce to, so the orchestrator and risk layer can treat
them uniformly regardless of internal complexity.

Authority:
  - López de Prado (2018) AFML Ch.16 — portfolio construction across
    heterogeneous strategies
  - Carver (2019) Systematic Trading Ch.11 — strategy diversification
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

import structlog


log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class Signal:
    """
    Common signal currency across all strategy families.

    direction   : 1 = long, -1 = short, 0 = flat/no-op
    confidence  : model/strategy confidence in [0, 1]; not a position size
    regime_fit  : how well current regime suits this strategy, in [0, 1].
                  0 means "do not trade in this regime" and must be treated
                  as a hard gate by the caller, not just a scalar.
    """

    direction: int
    confidence: float
    regime_fit: float

    def __post_init__(self) -> None:
        if self.direction not in (-1, 0, 1):
            raise ValueError(f"Signal.direction must be -1, 0, or 1, got {self.direction}")
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError(f"Signal.confidence must be in [0, 1], got {self.confidence}")
        if not 0.0 <= self.regime_fit <= 1.0:
            raise ValueError(f"Signal.regime_fit must be in [0, 1], got {self.regime_fit}")


@runtime_checkable
class StrategyProtocol(Protocol):
    """Contract every registrable strategy must satisfy."""

    strategy_id: str

    def generate_signal(self, bar: object) -> Signal:
        """Compute a Signal from the current bar/context. Must not raise on
        benign no-signal conditions — return Signal(0, 0.0, 0.0) instead."""
        ...

    def required_capital_fraction(self) -> float:
        """Upper bound on the fraction of book capital this strategy may
        request, in (0, 1]. Enforced independently of Kelly sizing."""
        ...


class DuplicateStrategyError(ValueError):
    """Raised when a strategy_id is registered more than once."""


class StrategyRegistry:
    """
    Holds the active strategy set. Orchestrator/risk layer iterate this
    registry instead of calling strategy implementations directly.

    Registration is explicit and validated at add-time so a malformed
    strategy fails fast, not mid-tick.
    """

    def __init__(self) -> None:
        self._strategies: dict[str, StrategyProtocol] = {}

    def register(self, strategy: StrategyProtocol) -> None:
        if not isinstance(strategy, StrategyProtocol):
            raise TypeError(
                f"{strategy!r} does not satisfy StrategyProtocol "
                "(needs strategy_id, generate_signal, required_capital_fraction)"
            )
        if not strategy.strategy_id:
            raise ValueError("strategy_id must be a non-empty string")
        if strategy.strategy_id in self._strategies:
            raise DuplicateStrategyError(f"strategy_id {strategy.strategy_id!r} already registered")
        capital_fraction = strategy.required_capital_fraction()
        if not 0.0 < capital_fraction <= 1.0:
            raise ValueError(
                f"{strategy.strategy_id}: required_capital_fraction() must be in "
                f"(0, 1], got {capital_fraction}"
            )
        self._strategies[strategy.strategy_id] = strategy
        log.info("strategy_registered", strategy_id=strategy.strategy_id)

    def unregister(self, strategy_id: str) -> None:
        self._strategies.pop(strategy_id, None)

    def get(self, strategy_id: str) -> StrategyProtocol | None:
        return self._strategies.get(strategy_id)

    def all(self) -> tuple[StrategyProtocol, ...]:
        return tuple(self._strategies.values())

    def __len__(self) -> int:
        return len(self._strategies)

    def __contains__(self, strategy_id: str) -> bool:
        return strategy_id in self._strategies


_default_registry: StrategyRegistry | None = None


def get_default_registry() -> StrategyRegistry:
    """Process-wide default registry, lazily constructed."""
    global _default_registry
    if _default_registry is None:
        _default_registry = StrategyRegistry()
    return _default_registry
