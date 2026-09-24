"""
The control surface must not misrepresent what it controls.

An operator who believes a slider moved a position-size cap, and is wrong, is
in a more dangerous position than one who knows the value is only editable in
config. So these tests are mostly about honesty rather than function: the
tier labels, the bounds, and the things that must never appear.

The bounds test is the one that earns its place. The first version of
control_surface.py hardcoded fractions (0.001-0.50) while the endpoint's
validator takes percentages (0.1-50.0), so a live stop_loss_pct of 2.0 would
have been reported as below its own minimum. Deriving the bounds from the
model removed the second source of truth; this asserts it stays removed.
"""

from __future__ import annotations

import pytest

from src.api.control_surface import (
    _OPERATOR_SETTABLE,
    Control,
    _bounds_from_model,
    build_control_surface,
    protected_controls,
)
from src.api.main import SetRiskControlsRequest
from src.tuning.registry import EXCLUDED_PARAMS

_CREDENTIAL_MARKERS = ("api_key", "api_secret", "passphrase")


@pytest.fixture(scope="module")
def surface_fields():
    """The model whose ge/le the surface must mirror."""
    return SetRiskControlsRequest


class TestBoundsComeFromTheValidator:
    @pytest.mark.parametrize(
        ("field_name", "expected"),
        [
            ("stop_loss_pct", (0.1, 50.0)),
            ("take_profit_pct", (0.1, 200.0)),
            ("trailing_stop_pct", (0.1, 50.0)),
            ("max_holding_period_s", (60.0, None)),
        ],
    )
    def test_the_declared_bounds_are_the_enforced_bounds(
        self, surface_fields, field_name, expected
    ):
        assert _bounds_from_model(surface_fields, field_name) == expected

    def test_an_unknown_field_is_unbounded_rather_than_guessed(self, surface_fields):
        assert _bounds_from_model(surface_fields, "not_a_field") == (None, None)

    async def test_every_live_value_sits_inside_its_own_bounds(self, surface_fields):
        """
        The bug this file was written for: a control reporting a current value
        outside the range it advertises.
        """
        surface = await build_control_surface(surface_fields)
        for control in surface["controls"]:
            if not control["live"] or not isinstance(control["value"], (int, float)):
                continue
            if control["min"] is not None:
                assert control["value"] >= control["min"], control["name"]
            if control["max"] is not None:
                assert control["value"] <= control["max"], control["name"]


class TestTheTiersAreHonest:
    async def test_live_controls_are_marked_live_and_need_no_restart(self, surface_fields):
        surface = await build_control_surface(surface_fields)
        live = [c for c in surface["controls"] if c["live"]]

        assert live, "the surface claims nothing is adjustable"
        assert all(c["requires_restart"] is False for c in live)
        assert all(c["protected"] is False for c in live)

    async def test_execution_mode_offers_only_real_modes(self, surface_fields):
        from src.config import ExecutionMode

        surface = await build_control_surface(surface_fields)
        mode = next(c for c in surface["controls"] if c["name"] == "execution_mode")

        assert set(mode["options"]) == {m.value for m in ExecutionMode}
        assert mode["value"] in mode["options"]

    async def test_the_counts_describe_the_payload(self, surface_fields):
        surface = await build_control_surface(surface_fields)
        controls = surface["controls"]

        assert surface["counts"]["total"] == len(controls)
        assert surface["counts"]["live"] == sum(1 for c in controls if c["live"])
        assert surface["counts"]["protected"] == sum(1 for c in controls if c["protected"])


class TestNoControlAppearsTwice:
    async def test_a_control_has_exactly_one_tier(self, surface_fields):
        """
        The bug this caught: EXCLUDED_PARAMS answers "what may the autotuner
        move", not "what may an operator move". execution_mode is excluded from
        self-tuning *and* deliberately operator-switchable, so it was emitted
        twice -- once live, once protected -- by the very surface whose job is
        not to misrepresent what it controls. A duplicate name is that class of
        bug whatever causes it, so the assertion is on the names.
        """
        surface = await build_control_surface(surface_fields)
        names = [c["name"] for c in surface["controls"]]

        assert len(names) == len(set(names)), "a control is listed under two tiers"

    async def test_execution_mode_is_live_and_not_protected(self, surface_fields):
        surface = await build_control_surface(surface_fields)
        rows = [c for c in surface["controls"] if c["name"] == "execution_mode"]

        assert len(rows) == 1
        assert rows[0]["live"] is True
        assert rows[0]["protected"] is False

    def test_a_parameter_with_no_setter_stays_protected(self):
        """
        trading_mode is excluded from tuning and nothing exposes a setter for
        it, so protected is the truthful tier -- the fix for execution_mode
        must not quietly promote every excluded parameter.
        """
        names = {c.name for c in protected_controls()}
        assert "trading_mode" in names
        assert "execution_mode" not in names


class TestProtectedParametersAreShownButNeverOffered:
    def test_every_excluded_parameter_without_a_setter_is_present(self):
        """
        Present, not hidden: an operator needs to see that a drawdown halt
        exists. Absent, they cannot tell it from a setting nobody implemented.
        """
        names = {c.name for c in protected_controls()}
        assert names == set(EXCLUDED_PARAMS) - _OPERATOR_SETTABLE

    def test_none_of_them_is_offered_as_a_control(self):
        for control in protected_controls():
            assert control.kind == "readonly"
            assert control.protected is True
            assert control.live is False

    def test_each_one_says_why(self):
        for control in protected_controls():
            assert control.reason, control.name

    @pytest.mark.parametrize(
        "name", sorted(n for n in EXCLUDED_PARAMS if any(m in n for m in _CREDENTIAL_MARKERS))
    )
    def test_a_credential_is_named_but_never_valued(self, name):
        """
        The point of listing a credential is to say it is not tunable.
        Printing it to say so would defeat the exercise.
        """
        control = next(c for c in protected_controls() if c.name == name)
        assert control.value is None
        assert "credential" in control.reason

    async def test_no_credential_value_reaches_the_payload(self, surface_fields):
        surface = await build_control_surface(surface_fields)
        for control in surface["controls"]:
            if any(marker in control["name"] for marker in _CREDENTIAL_MARKERS):
                assert control["value"] is None


class TestTheRowShape:
    def test_as_dict_carries_every_field_the_frontend_reads(self):
        row = Control(name="x", group="g", kind="toggle", value=True, live=True).as_dict()

        assert set(row) == {
            "name",
            "group",
            "kind",
            "value",
            "live",
            "requires_restart",
            "protected",
            "reason",
            "min",
            "max",
            "options",
        }


class TestTheDefensivePaths:
    async def test_a_field_runtime_config_does_not_track_is_omitted(self, monkeypatch):
        """
        set_risk_controls() accepts more fields than get_risk_controls()
        necessarily reports. Omitting an untracked one beats emitting a row
        whose value is not the live value -- the whole point of the surface.
        """
        from src.api import control_surface as cs

        async def _partial():
            return {"stop_loss_enabled": True, "stop_loss_pct": 2.0}

        monkeypatch.setattr(cs.runtime_config, "get_risk_controls", _partial)

        names = {c.name for c in await cs.risk_controls(SetRiskControlsRequest)}

        assert names == {"risk_controls.stop_loss_enabled", "risk_controls.stop_loss_pct"}

    def test_an_unresolvable_dotted_path_reads_as_absent_not_as_a_crash(self):
        """
        EXCLUDED_PARAMS names parameters, not settings fields, and the two sets
        are not required to coincide -- 'trading_mode' resolves, a future entry
        might not. An unresolvable one is reported with no value rather than
        taking the whole endpoint down.
        """
        from src.api.control_surface import _resolve_dotted
        from src.config import get_settings

        assert _resolve_dotted(get_settings(), "risk.no_such_field") is None
        assert _resolve_dotted(get_settings(), "nope.nope.nope") is None
