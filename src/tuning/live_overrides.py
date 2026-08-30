"""
Live-value overlay for self-tuned parameters.

Design: docs/SELF_TUNING_DESIGN.md rollout plan step 3 -- a promoted
challenger value must reach the live trading path, not just sit in
VersionedConfigStore's audit log. This module is the single seam where
that happens: each effective_*_settings() helper returns a copy of the
relevant Settings section with any promoted/registered self-tuning value
overlaid on top of the static .env-derived default.

Only parameters actually registered in parameter_registry (see
src/tuning/bootstrap.py) are overlaid -- an unregistered field is
returned unchanged. Registration itself requires an explicit startup
step (AutoTuningScheduler.start(); the manual script counterpart was
removed by the config purge in #144), so
these helpers are a no-op overlay until self-tuning is enabled and that
step has run.

Callers that need a FIXED value regardless of concurrent promotions --
chiefly AutoTuningScheduler's own champion/challenger backtest
evaluation, which must compare against the
exact proposal it generated, not a value that could change mid-backtest
-- must keep passing an explicit cfg= / *_cfg= argument. These helpers
only change what each consumer's *default* resolves to; every call site
that already passes cfg explicitly is unaffected.
"""

from __future__ import annotations

from src.config import (
    FeatureSettings,
    HMMSettings,
    RiskSettings,
    XGBoostSettings,
    get_settings,
)
from src.tuning.registry import parameter_registry


# Matches src.tuning.backtest_harness.XGBOOST_INT_FIELDS -- duplicated here
# (rather than imported) to avoid this leaf module depending on the
# backtest harness; both must stay in sync with XGBoostSettings' own
# int-typed fields in src/config.py.
_XGBOOST_INT_FIELDS = frozenset({"n_estimators", "max_depth", "min_child_weight"})

_FEATURE_WINDOW_FIELDS = (
    "vwap_window",
    "ofi_window",
    "atr_window",
    "sharpe_window",
    "volume_zscore_window",
    "garch_window",
)

# Per-field minimum values for feature window fields. Fields not listed here use 2.
_FEATURE_WINDOW_FLOOR_OVERRIDES: dict[str, int] = {"garch_window": 50}

_XGBOOST_FIELDS = (
    "n_estimators",
    "max_depth",
    "learning_rate",
    "subsample",
    "colsample_bytree",
    "min_child_weight",
    "reg_alpha",
    "reg_lambda",
)


def _current_override(name: str) -> float | None:
    if not parameter_registry.is_registered(name):
        return None
    return parameter_registry.get(name).current


def effective_hmm_settings(base: HMMSettings | None = None) -> HMMSettings:
    """HMMSettings with hmm.entropy_threshold / hmm.entropy_scalar_floor
    overlaid from the registry when self-tuning has promoted a value."""
    base = base or get_settings().hmm
    overrides: dict[str, float] = {}
    for field_name in ("entropy_threshold", "entropy_scalar_floor"):
        value = _current_override(f"hmm.{field_name}")
        if value is not None:
            overrides[field_name] = value
    return base.model_copy(update=overrides) if overrides else base


def effective_risk_settings(base: RiskSettings | None = None) -> RiskSettings:
    """RiskSettings with risk.slippage_impact_coeff_bps, risk.ensemble_blend_weight,
    and risk.garch_vol_threshold overlaid from the registry when self-tuning has
    promoted a value. All other RiskSettings fields (Kelly sizing, drawdown halts,
    ...) are permanently excluded from self-tuning (see registry.EXCLUDED_PARAMS)
    and are never overlaid here."""
    base = base or get_settings().risk
    overrides: dict[str, float] = {}
    for field_name in ("slippage_impact_coeff_bps", "ensemble_blend_weight", "garch_vol_threshold"):
        value = _current_override(f"risk.{field_name}")
        if value is not None:
            overrides[field_name] = value
    return base.model_copy(update=overrides) if overrides else base


def effective_feature_settings(base: FeatureSettings | None = None) -> FeatureSettings:
    """FeatureSettings with the six features.*_window fields overlaid
    from the registry. Values are stored as float in the registry
    (TunableParameter.current); rounded to int here to match
    FeatureSettings' int fields, clamped to per-field floors (garch_window
    >= 50, all others >= 2) matching the scheduler's own backtest evaluation."""
    base = base or get_settings().features
    overrides: dict[str, int] = {}
    for field_name in _FEATURE_WINDOW_FIELDS:
        value = _current_override(f"features.{field_name}")
        if value is not None:
            floor = _FEATURE_WINDOW_FLOOR_OVERRIDES.get(field_name, 2)
            overrides[field_name] = max(floor, round(value))
    return base.model_copy(update=overrides) if overrides else base


def effective_xgboost_settings(base: XGBoostSettings | None = None) -> XGBoostSettings:
    """XGBoostSettings with the eight xgboost.* hyperparameters overlaid
    from the registry, rounding the int-typed fields to match
    XGBoostSettings' field types."""
    base = base or get_settings().xgboost
    overrides: dict[str, float] = {}
    for field_name in _XGBOOST_FIELDS:
        value = _current_override(f"xgboost.{field_name}")
        if value is not None:
            overrides[field_name] = round(value) if field_name in _XGBOOST_INT_FIELDS else value
    return base.model_copy(update=overrides) if overrides else base
