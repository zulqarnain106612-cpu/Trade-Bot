"""
Parameter registry for the self-tuning subsystem.

Design: docs/SELF_TUNING_DESIGN.md §3.

The registry is the single place that decides *which* parameters the bot
is allowed to propose changes to, and the hard floor/ceiling bounds a
proposal can never exceed. Hard risk limits are permanently excluded and
cannot be registered, regardless of caller intent -- this is enforced in
code, not just by convention, so a future call site can't accidentally
open up Kelly sizing or drawdown halts to self-tuning.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, replace


# Parameters that must NEVER be self-tunable, per SELF_TUNING_DESIGN.md §3.
# Match is by dotted "section.field" name as used in src/config.py.
EXCLUDED_PARAMS: frozenset[str] = frozenset(
    {
        "risk.kelly_multiplier",
        "risk.kelly_ceiling",
        "risk.daily_drawdown_halt_pct",
        "risk.consecutive_loss_halt",
        "risk.max_position_size_pct",
        "risk.notional_limit_usd",
        "risk.oos_sharpe_threshold",
        "risk.max_drawdown_threshold",
        "risk.min_trades_live_gate",
        "trading_mode",
        "execution_mode",
        "binance.api_key",
        "binance.api_secret",
        "okx.api_key",
        "okx.api_secret",
        "okx.passphrase",
    }
)


class ExcludedParameterError(ValueError):
    """Raised when code attempts to register a permanently-excluded parameter."""


class InvalidBoundsError(ValueError):
    """Raised when a parameter's floor/ceiling/current values are inconsistent."""


class DuplicateParameterError(ValueError):
    """Raised when a parameter name is registered twice."""


class UnknownParameterError(KeyError):
    """Raised when looking up a parameter that was never registered."""


@dataclass(frozen=True)
class TunableParameter:
    """
    A single self-tunable parameter and the bounds self-tuning may operate within.

    name          : dotted "section.field" matching src/config.py, e.g. "hmm.entropy_threshold".
    description   : human-readable purpose, surfaced in /self-tuning/status.
    floor, ceiling: hard bounds set by the user (via registration call site) —
                    a challenger value outside [floor, ceiling] is rejected
                    before evaluation ever runs.
    current       : the current champion value.
    eval_strategy : identifier for how ChallengerEvaluator scores this
                    parameter (e.g. "cpcv_oos_sharpe"); Phase 2 concern,
                    stored here so the registry stays the single source of
                    truth for a parameter's full contract.
    """

    name: str
    description: str
    floor: float
    ceiling: float
    current: float
    eval_strategy: str

    def __post_init__(self) -> None:
        if self.name in EXCLUDED_PARAMS:
            raise ExcludedParameterError(
                f"{self.name!r} is a hard risk-limit parameter and can never be "
                "self-tunable (see docs/SELF_TUNING_DESIGN.md §3)."
            )
        if self.floor > self.ceiling:
            raise InvalidBoundsError(
                f"{self.name!r}: floor ({self.floor}) must be <= ceiling ({self.ceiling})"
            )
        if not (self.floor <= self.current <= self.ceiling):
            raise InvalidBoundsError(
                f"{self.name!r}: current ({self.current}) must lie within "
                f"[{self.floor}, {self.ceiling}]"
            )

    def in_bounds(self, value: float) -> bool:
        return self.floor <= value <= self.ceiling


class ParameterRegistry:
    """
    Process-wide registry of self-tunable parameters.

    Thread-safe (registration happens at startup; reads may happen from
    concurrent request handlers once the API layer is wired in Phase 2+).
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._params: dict[str, TunableParameter] = {}

    def register(self, param: TunableParameter) -> None:
        with self._lock:
            if param.name in self._params:
                raise DuplicateParameterError(f"{param.name!r} is already registered")
            self._params[param.name] = param

    def get(self, name: str) -> TunableParameter:
        with self._lock:
            try:
                return self._params[name]
            except KeyError as exc:
                raise UnknownParameterError(name) from exc

    def list_all(self) -> list[TunableParameter]:
        with self._lock:
            return list(self._params.values())

    def is_registered(self, name: str) -> bool:
        with self._lock:
            return name in self._params

    def update_current(self, name: str, value: float) -> TunableParameter:
        """
        Advance a registered parameter's champion value after a live
        promotion (TuningRunner.attempt(), non-shadow-mode path only).

        Without this, TunableParameter.current is frozen at registration
        time forever, so (a) subsequent proposals keep centering on the
        original startup default instead of the newly promoted value, and
        (b) live consumers reading through the registry (see
        src/tuning/live_overrides.py) never observe a promotion. Re-runs
        TunableParameter.__post_init__'s floor/ceiling validation via
        dataclasses.replace, so an out-of-bounds value still raises
        InvalidBoundsError instead of silently corrupting the champion.
        """
        with self._lock:
            try:
                existing = self._params[name]
            except KeyError as exc:
                raise UnknownParameterError(name) from exc
            updated = replace(existing, current=value)
            self._params[name] = updated
            return updated

    def unregister(self, name: str) -> None:
        """Test/admin helper -- not exposed over the live API."""
        with self._lock:
            self._params.pop(name, None)


# Singleton -- Phase 1 ships with zero parameters registered against it.
# Registration of the first real parameter (hmm.entropy_threshold) happens
# in Phase 2, gated behind shadow-mode-only evaluation.
parameter_registry: ParameterRegistry = ParameterRegistry()
