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

Passing `store=` (src/tuning/state.py's version_store) resumes `current`
from the last promoted value on record, so a process restart doesn't
reset the tuning loop back to the raw .env default -- but bounds always
stay anchored to that fresh .env default, never to the resumed value
(see _resume_current).
"""

from __future__ import annotations

from src.config import Settings, get_settings
from src.tuning.registry import ParameterRegistry, TunableParameter
from src.tuning.store import VersionedConfigStore


_DEFAULT_BOUND_WINDOW_PCT = 0.20


def _symmetric_bounds(
    current: float, window_pct: float = _DEFAULT_BOUND_WINDOW_PCT
) -> tuple[float, float]:
    span = abs(current) * window_pct
    return current - span, current + span


def _resume_current(
    param_name: str,
    default: float,
    floor: float,
    ceiling: float,
    store: VersionedConfigStore | None,
) -> float:
    """
    If `store` already holds a promoted value for `param_name` within
    [floor, ceiling], resume from it instead of the raw operator default --
    so a process restart doesn't silently forget every prior promotion and
    reset the tuning loop back to its Phase-4/8 starting point (see
    src/tuning/live_overrides.py). Otherwise returns `default` unchanged
    (an explicit bounds/None check, not `resumed or default` -- a
    legitimately promoted value of 0.0 must not be discarded as falsy).

    Bounds are deliberately NOT recomputed from the resumed value -- they
    stay anchored to the operator's current .env-configured default (per
    this module's docstring: "user sets the ceiling, bot proposes within
    it"). A promoted value outside that window (e.g. the operator lowered
    the .env default since the last promotion) is discarded in favor of
    the fresh operator default, rather than silently operating outside the
    bounds the operator currently intends.
    """
    if store is None or not store.has_versions(param_name):
        return default
    promoted = store.current(param_name).value
    return promoted if floor <= promoted <= ceiling else default


def register_hmm_entropy_threshold(
    registry: ParameterRegistry,
    settings: Settings | None = None,
    store: VersionedConfigStore | None = None,
) -> TunableParameter:
    """Phase 4 -- the first (and, until Phase 8, only) live-eligible parameter."""
    settings = settings or get_settings()
    default = settings.hmm.entropy_threshold
    floor, ceiling = _symmetric_bounds(default)
    # Clamp to the HMMSettings field's own validation range [0, 1].
    floor = max(0.0, floor)
    ceiling = min(1.0, ceiling)
    current = _resume_current("hmm.entropy_threshold", default, floor, ceiling, store)
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
    registry: ParameterRegistry,
    settings: Settings | None = None,
    store: VersionedConfigStore | None = None,
) -> TunableParameter:
    """Phase 8 -- paired with entropy_threshold, same eval cost (both consumed
    by the same backtest_harness.run_entropy_threshold_backtest call)."""
    settings = settings or get_settings()
    default = settings.hmm.entropy_scalar_floor
    floor, ceiling = _symmetric_bounds(default)
    floor = max(0.0, floor)
    ceiling = min(1.0, ceiling)
    current = _resume_current("hmm.entropy_scalar_floor", default, floor, ceiling, store)
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
    registry: ParameterRegistry,
    settings: Settings | None = None,
    store: VersionedConfigStore | None = None,
) -> TunableParameter:
    """
    Phase 8 -- RiskSettings.slippage_impact_coeff_bps is already flagged in
    src/config.py as a TODO recalibration target "from realized fills once
    live data exists." This is that recalibration path, gated the same way
    as every other self-tuned parameter (shadow-mode evaluation, gate,
    never touching the excluded hard-limit parameters).
    """
    settings = settings or get_settings()
    default = settings.risk.slippage_impact_coeff_bps
    floor, ceiling = _symmetric_bounds(default)
    floor = max(0.0, floor)
    ceiling = min(2000.0, ceiling)
    current = _resume_current("risk.slippage_impact_coeff_bps", default, floor, ceiling, store)
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


def register_ensemble_blend_weight(
    registry: ParameterRegistry,
    settings: Settings | None = None,
    store: VersionedConfigStore | None = None,
) -> TunableParameter:
    """
    RiskSettings.ensemble_blend_weight -- how much of signal_engine.py's
    p_long comes from the diversified prediction ensemble
    (src/intelligence/ensemble_predictor.py) vs. the XGBoost direction
    model. Registered so this weight can only move via the same
    propose/evaluate/gate/shadow-mode machinery as every other tunable
    parameter -- never a direct .env edit while the bot is live.

    Registered but deliberately left UNSCHEDULED in
    src/tuning/scheduler.py's _attempt_all() -- same documented state
    hmm.entropy_threshold was in during Phase 4 before its backtest
    harness existed (see scheduler.py's module docstring: "Any other
    registered parameter with no evaluate_fn is intentionally left
    unscheduled here"). A dedicated backtest harness comparing champion
    vs. challenger blend weights against realized OOS trade outcomes is
    future work; until it exists this parameter is visible/adjustable
    manually (scripts/run_tuning_attempt.py, /self-tuning/status) but not
    auto-tuned on a cycle.
    """
    settings = settings or get_settings()
    default = settings.risk.ensemble_blend_weight
    floor, ceiling = _symmetric_bounds(default)
    floor = max(0.0, floor)
    ceiling = min(1.0, ceiling)
    current = _resume_current("risk.ensemble_blend_weight", default, floor, ceiling, store)
    param = TunableParameter(
        name="risk.ensemble_blend_weight",
        description="Weight of the prediction ensemble's implied probability in p_long.",
        floor=floor,
        ceiling=ceiling,
        current=current,
        eval_strategy="ensemble_blend_oos_accuracy",
    )
    registry.register(param)
    return param


# Phase 8 item 3 -- the five rolling-window feature parameters, all with the
# same `ge=2` Pydantic floor (src/config.py FeatureSettings) and all consumed
# by backtest_harness.run_feature_window_backtest.
FEATURE_WINDOW_FIELDS: frozenset[str] = frozenset(
    {
        "vwap_window",
        "ofi_window",
        "atr_window",
        "sharpe_window",
        "volume_zscore_window",
        "garch_window",
    }
)

_FEATURE_WINDOW_MIN_VALUE = 2.0  # matches FeatureSettings' `ge=2` on most fields
# garch_window has ge=50 in FeatureSettings (needs enough obs for GARCH MLE)
_FEATURE_WINDOW_FLOOR_OVERRIDES: dict[str, float] = {
    "garch_window": 50.0,
}


def register_feature_window_param(
    registry: ParameterRegistry,
    field_name: str,
    settings: Settings | None = None,
    store: VersionedConfigStore | None = None,
) -> TunableParameter:
    """
    Phase 8 item 3 -- one FeatureSettings rolling-window field at a time.

    Evaluated by backtest_harness.run_feature_window_backtest against the
    currently deployed, FROZEN direction model's OOS predictive quality --
    NOT full retraining (see docs/SELF_TUNING_IMPLEMENTATION_PLAN.md Phase
    8 item 3, risk 1: this tests the frozen model's sensitivity to a
    perturbed input, not whether a retrained model would be better).
    """
    if field_name not in FEATURE_WINDOW_FIELDS:
        raise ValueError(
            f"{field_name!r} is not a registered feature-window field; "
            f"supported: {sorted(FEATURE_WINDOW_FIELDS)}"
        )
    settings = settings or get_settings()
    default = float(getattr(settings.features, field_name))
    floor, ceiling = _symmetric_bounds(default)
    min_val = _FEATURE_WINDOW_FLOOR_OVERRIDES.get(field_name, _FEATURE_WINDOW_MIN_VALUE)
    floor = max(min_val, floor)
    param_name = f"features.{field_name}"
    current = _resume_current(param_name, default, floor, ceiling, store)
    param = TunableParameter(
        name=param_name,
        description=f"Rolling window (bars) for the {field_name} feature.",
        floor=floor,
        ceiling=ceiling,
        current=current,
        eval_strategy="frozen_model_oos_sharpe",
    )
    registry.register(param)
    return param


# Phase 8 item 4 -- the eight tunable XGBoost hyperparameters, each with its
# own valid range from XGBoostSettings' own Pydantic field constraints
# (src/config.py), clamped the same way register_hmm_entropy_threshold
# clamps to HMMSettings' [0, 1]. Consumed by
# backtest_harness.run_xgboost_hyperparam_backtest, which does a full CPCV
# retrain per candidate -- the most expensive parameter group, evaluated
# last per the design doc's ordering.
XGBOOST_HYPERPARAM_FIELDS: frozenset[str] = frozenset(
    {
        "n_estimators",
        "max_depth",
        "learning_rate",
        "subsample",
        "colsample_bytree",
        "min_child_weight",
        "reg_alpha",
        "reg_lambda",
    }
)

_XGB_FIELD_BOUNDS: dict[str, tuple[float, float]] = {
    # Fields with a Pydantic upper bound (src/config.py XGBoostSettings) use
    # it directly. Fields with no upper bound get a generous but finite
    # safety ceiling (same "arbitrary but reasonable backstop" convention as
    # register_slippage_impact_coeff's ceiling=2000.0) -- math.inf would let
    # the proposer's `range_width * step_pct` step computation produce inf
    # or NaN, which must never reach XGBClassifier's constructor.
    "n_estimators": (10.0, 2000.0),
    "max_depth": (1.0, 20.0),
    "learning_rate": (1e-5, 1.0),
    "subsample": (0.1, 1.0),
    "colsample_bytree": (0.1, 1.0),
    "min_child_weight": (1.0, 100.0),
    "reg_alpha": (0.0, 100.0),
    "reg_lambda": (0.0, 100.0),
}


def register_garch_vol_threshold(
    registry: ParameterRegistry,
    settings: Settings | None = None,
    store: VersionedConfigStore | None = None,
) -> TunableParameter:
    """
    RiskSettings.garch_vol_threshold — the per-bar conditional-vol level above
    which the GARCH vol-targeting scalar (Carver 2019) begins scaling position
    size down. Below the threshold, no reduction is applied (scalar = 1.0).

    A higher threshold is more permissive (fewer reductions); a lower threshold
    is more aggressive (earlier and deeper reductions in high-vol regimes).
    Evaluated using cpcv_oos_sharpe: the impact flows through position sizing,
    so a frozen-model OOS Sharpe comparison captures whether the threshold
    improves risk-adjusted returns without a full XGBoost retrain.

    Registered but intentionally left unscheduled in scheduler.py until a
    dedicated backtest harness measuring the vol-targeting scalar's sensitivity
    to threshold changes exists (same pattern as register_ensemble_blend_weight).
    """
    settings = settings or get_settings()
    default = settings.risk.garch_vol_threshold
    floor, ceiling = _symmetric_bounds(default)
    floor = max(0.001, floor)  # RiskSettings enforces gt=0.0; keep a safe margin
    ceiling = min(0.50, ceiling)  # RiskSettings enforces le=0.50
    current = _resume_current("risk.garch_vol_threshold", default, floor, ceiling, store)
    param = TunableParameter(
        name="risk.garch_vol_threshold",
        description=(
            "GARCH vol-targeting threshold (Carver 2019): per-bar sigma above which "
            "position size is scaled down proportionally."
        ),
        floor=floor,
        ceiling=ceiling,
        current=current,
        eval_strategy="cpcv_oos_sharpe",
    )
    registry.register(param)
    return param


def register_xgboost_hyperparam_param(
    registry: ParameterRegistry,
    field_name: str,
    settings: Settings | None = None,
    store: VersionedConfigStore | None = None,
) -> TunableParameter:
    """
    Phase 8 item 4 -- one XGBoostSettings hyperparameter at a time.

    Evaluated by backtest_harness.run_xgboost_hyperparam_backtest, which
    trains real champion and challenger models via ModelTrainer's own
    CPCV harness (not a frozen-model sensitivity test like items 1-3) --
    the most faithful but also the most expensive evaluation in this
    subsystem.
    """
    if field_name not in XGBOOST_HYPERPARAM_FIELDS:
        raise ValueError(
            f"{field_name!r} is not a registered XGBoost hyperparameter field; "
            f"supported: {sorted(XGBOOST_HYPERPARAM_FIELDS)}"
        )
    settings = settings or get_settings()
    default = float(getattr(settings.xgboost, field_name))
    floor, ceiling = _symmetric_bounds(default)
    field_floor, field_ceiling = _XGB_FIELD_BOUNDS[field_name]
    floor = max(field_floor, floor)
    ceiling = min(field_ceiling, ceiling)
    param_name = f"xgboost.{field_name}"
    current = _resume_current(param_name, default, floor, ceiling, store)
    param = TunableParameter(
        name=param_name,
        description=f"XGBoost hyperparameter {field_name}, recalibrated via full CPCV retraining.",
        floor=floor,
        ceiling=ceiling,
        current=current,
        eval_strategy="cpcv_full_retrain",
    )
    registry.register(param)
    return param
