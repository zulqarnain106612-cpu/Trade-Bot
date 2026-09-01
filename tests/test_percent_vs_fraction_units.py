"""
Pins the units of every ``*_pct`` setting.

The suffix is not a reliable guide: RiskSettings holds
``daily_drawdown_halt_pct = 2.0`` (a PERCENT) two fields away from
``capital_preservation_max_drawdown_pct = 0.30`` (a FRACTION). Reading one
and assuming the other is exactly how a fraction ended up being reported
beside a percent in the stress-test response.

Renaming is not an option -- these names are the ``RISK_*`` / ``FEATURES_*``
/ ``SELF_TUNING_*`` environment variables and changing them would silently
reset deployed configuration to defaults. So the units are pinned here
instead: a future edit that flips one has to flip this test too, in a diff
that says so.
"""

from __future__ import annotations

import pytest

from src.config import FeatureSettings, RiskSettings, SelfTuningSettings

# Fields whose value is a FRACTION of 1 despite the _pct suffix.
_FRACTION_FIELDS = [
    (RiskSettings, "capital_preservation_max_drawdown_pct"),
    (RiskSettings, "cvar_limit_pct"),
    (FeatureSettings, "embargo_pct"),
    (SelfTuningSettings, "proposer_step_pct"),
]

# Fields whose value really is a percentage.
_PERCENT_FIELDS = [
    (RiskSettings, "daily_drawdown_halt_pct"),
    (RiskSettings, "max_position_size_pct"),
]


def _bounds(model: type, field: str) -> tuple[float | None, float | None]:
    meta = model.model_fields[field].metadata
    lo = hi = None
    for m in meta:
        for attr, target in (("ge", "lo"), ("gt", "lo"), ("le", "hi"), ("lt", "hi")):
            if hasattr(m, attr):
                value = getattr(m, attr)
                if target == "lo":
                    lo = value
                else:
                    hi = value
    return lo, hi


@pytest.mark.parametrize(("model", "field"), _FRACTION_FIELDS)
def test_fraction_fields_are_bounded_at_or_below_one(model: type, field: str) -> None:
    """
    The bound is the real guard: it is what stops a percent-shaped value
    (say 30 for "30%") from being accepted where a fraction is meant.
    """
    _, hi = _bounds(model, field)
    assert hi is not None, f"{model.__name__}.{field} must bound its upper end"
    assert hi <= 1.0


@pytest.mark.parametrize(("model", "field"), _FRACTION_FIELDS)
def test_fraction_field_defaults_are_fractions(model: type, field: str) -> None:
    default = model.model_fields[field].default
    if default is None:
        # Optional control (None = disabled). The bound test above is what
        # pins its units; there is no default to check.
        return
    assert 0.0 < default <= 1.0


@pytest.mark.parametrize(("model", "field"), _PERCENT_FIELDS)
def test_percent_fields_permit_values_above_one(model: type, field: str) -> None:
    _, hi = _bounds(model, field)
    assert hi is not None and hi > 1.0


@pytest.mark.parametrize(("model", "field"), _PERCENT_FIELDS)
def test_percent_field_defaults_are_percentages(model: type, field: str) -> None:
    assert model.model_fields[field].default > 1.0


def test_a_percent_shaped_value_is_rejected_by_the_fraction_field() -> None:
    """An operator writing 30 meaning "30%" must get an error, not a 3000% floor."""
    with pytest.raises(ValueError):
        RiskSettings(capital_preservation_max_drawdown_pct=30.0)


def test_every_pct_field_is_classified_here() -> None:
    """
    A new *_pct setting must declare which convention it follows, rather
    than inheriting whichever one the reader assumed.
    """
    classified = {f for _, f in _FRACTION_FIELDS + _PERCENT_FIELDS}
    found = {
        field
        for model in (RiskSettings, FeatureSettings, SelfTuningSettings)
        for field in model.model_fields
        if field.endswith("_pct")
    }
    assert found <= classified, f"unclassified *_pct settings: {sorted(found - classified)}"
