"""
Explicit parameter registration for the self-tuning subsystem.

Design: docs/SELF_TUNING_IMPLEMENTATION_PLAN.md Phase 4 / Phase 8.

Registration is never an import-time side effect (see src/tuning/registry.py
-- ParameterRegistry ships empty by default). Each `register_*` function
here must be called explicitly by an operator-run script or an explicit
startup step, so a parameter never becomes tunable just because a module
got imported somewhere.

Bounds are +/-20% of the operator's configured default, per the design
doc's "user sets the ceiling, bot proposes within it" invariant -- the
20% window is deliberately narrow; it can be widened later by an
operator re-registering with different bounds, never by the bot itself.
"""

from __future__ import annotations

from src.config import Settings, get_settings
from src.tuning.registry import ParameterRegistry, TunableParameter


_DEFAULT_BOUND_WINDOW_PCT = 0.20


def _symmetric_bounds(
    current: float, window_pct: float = _DEFAULT_BOUND_WINDOW_PCT
) -> tuple[float, float]:
    span = abs(current) * window_pct
    return current - span, current + span


def register_hmm_entropy_threshold(
    registry: ParameterRegistry, settings: Settings | None = None
) -> TunableParameter:
    """Phase 4 -- the first (and, until Phase 8, only) live-eligible parameter."""
    settings = settings or get_settings()
    current = settings.hmm.entropy_threshold
    floor, ceiling = _symmetric_bounds(current)
    # Clamp to the HMMSettings field's own validation range [0, 1].
    floor = max(0.0, floor)
    ceiling = min(1.0, ceiling)
    param = TunableParameter(
        name="hmm.entropy_threshold",
        description="Regime posterior entropy gate above which position size scales down.",
        floor=floor,
        ceiling=ceiling,
        current=current,
        eval_strategy="cpcv_oos_sharpe",
    )
    registry.register(param)
    return param


def register_hmm_entropy_scalar_floor(
    registry: ParameterRegistry, settings: Settings | None = None
) -> TunableParameter:
    """Phase 8 -- paired with entropy_threshold, same eval cost (both consumed
    by the same backtest_harness.run_entropy_threshold_backtest call)."""
    settings = settings or get_settings()
    current = settings.hmm.entropy_scalar_floor
    floor, ceiling = _symmetric_bounds(current)
    floor = max(0.0, floor)
    ceiling = min(1.0, ceiling)
    param = TunableParameter(
        name="hmm.entropy_scalar_floor",
        description="Minimum position-size scalar at maximum regime-posterior entropy.",
        floor=floor,
        ceiling=ceiling,
        current=current,
        eval_strategy="cpcv_oos_sharpe",
    )
    registry.register(param)
    return param


def register_slippage_impact_coeff(
    registry: ParameterRegistry, settings: Settings | None = None
) -> TunableParameter:
    """
    Phase 8 -- RiskSettings.slippage_impact_coeff_bps is already flagged in
    src/config.py as a TODO recalibration target "from realized fills once
    live data exists." This is that recalibration path, gated the same way
    as every other self-tuned parameter (shadow-mode evaluation, gate,
    never touching the excluded hard-limit parameters).
    """
    settings = settings or get_settings()
    current = settings.risk.slippage_impact_coeff_bps
    floor, ceiling = _symmetric_bounds(current)
    floor = max(0.0, floor)
    ceiling = min(2000.0, ceiling)
    param = TunableParameter(
        name="risk.slippage_impact_coeff_bps",
        description="Almgren-Chriss market-impact coefficient, recalibrated from realized fills.",
        floor=floor,
        ceiling=ceiling,
        current=current,
        eval_strategy="realized_fill_error",
    )
    registry.register(param)
    return param
