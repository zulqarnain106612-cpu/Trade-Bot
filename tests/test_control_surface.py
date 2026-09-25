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
    _CREDENTIAL_MARKERS,
    Control,
    _bounds_from_model,
    build_control_surface,
)
from src.api.main import SetRiskControlsRequest


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

    async def test_trading_mode_is_the_operators_to_set(self, surface_fields):
        """
        It was listed protected because it is in EXCLUDED_PARAMS. That list
        keeps the *autotuner* away from it, which is right -- nothing should
        self-tune its way from paper into live. The owner switching their own
        bot is a different act, and it is theirs to make, behind the operator
        secret like every other write.
        """
        surface = await build_control_surface(surface_fields)
        row = next(c for c in surface["controls"] if c["name"] == "trading_mode")

        assert row["live"] is True
        assert row["protected"] is False
        assert row["kind"] == "select"


class TestCredentialsAreTheOnlyThingNeverShown:
    """
    The protected tier used to hold every EXCLUDED_PARAMS entry, because that
    list was read as "what may never be changed at runtime". It answers what
    the *autotuner* may move. Risk limits are now the operator's to set, and
    the only permanent exclusion is a credential: it is not shown, not
    settable, and would otherwise be broadcast to every dashboard on the next
    control_changed frame.
    """

    async def test_a_credential_is_present_by_name_but_never_valued(self, surface_fields):
        surface = await build_control_surface(surface_fields)
        credentials = [
            c for c in surface["controls"] if any(m in c["name"] for m in _CREDENTIAL_MARKERS)
        ]

        assert credentials, "the surface hides credentials entirely rather than naming them"
        for control in credentials:
            assert control["value"] is None
            assert control["protected"] is True
            assert control["live"] is False
            assert "credential" in control["reason"]

    async def test_a_risk_limit_is_no_longer_protected(self, surface_fields):
        surface = await build_control_surface(surface_fields)
        row = next(c for c in surface["controls"] if c["name"] == "risk.max_position_size_pct")

        assert row["protected"] is False
        assert row["live"] is True

    async def test_a_startup_bound_field_is_marked_rather_than_claimed_live(self, surface_fields):
        surface = await build_control_surface(surface_fields)
        row = next(c for c in surface["controls"] if c["name"] == "api.port")

        assert row["live"] is False
        assert row["requires_restart"] is True
        assert "startup" in row["reason"]


class TestTheWidgetComesFromTheType:
    @pytest.mark.parametrize(
        ("name", "kind"),
        [
            ("binance.testnet", "toggle"),
            ("features.atr_window", "number"),
            ("binance.base_url", "text"),
        ],
    )
    async def test_the_kind_matches_the_field(self, surface_fields, name, kind):
        """
        Derived from the annotation, not from the name: a field called
        *_enabled that is an int is still a number, and the hub must not offer
        a toggle for it.
        """
        surface = await build_control_surface(surface_fields)
        row = next(c for c in surface["controls"] if c["name"] == name)

        assert row["kind"] == kind

    async def test_the_whole_configuration_is_present(self, surface_fields):
        """
        The point of the tier. 9 controls out of 188 fields was the gap; this
        asserts the surface covers the settings tree rather than a corner.
        """
        from src.config import Settings

        def leaves(model, prefix=""):
            for fname, info in model.model_fields.items():
                ann = info.annotation
                if isinstance(ann, type) and hasattr(ann, "model_fields"):
                    yield from leaves(ann, f"{prefix}{fname}.")
                else:
                    yield f"{prefix}{fname}"

        surface = await build_control_surface(surface_fields)
        names = {c["name"] for c in surface["controls"]}
        missing = {leaf for leaf in leaves(Settings)} - names - {"execution_mode"}

        assert not missing, f"settings absent from the control surface: {sorted(missing)[:8]}"


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


class TestARegisteredTunableIsRenderedCorrectly:
    """
    The registry is empty at rest, so every assertion above ran without a
    single tuning row. That is how `param.low` / `param.high` -- names
    TunableParameter does not have; it has floor and ceiling -- reached a
    reviewed commit: the code path was never executed. GET /controls would
    have raised AttributeError the moment the self-tuning scheduler
    registered anything, which is to say the first time it mattered.
    """

    @pytest.fixture
    def a_registered_param(self):
        from src.tuning.registry import TunableParameter, parameter_registry

        param = TunableParameter(
            name="risk.ensemble_blend_weight",
            description="blend weight between ensemble members",
            floor=0.0,
            ceiling=1.0,
            current=0.5,
            eval_strategy="cpcv_oos_sharpe",
        )
        parameter_registry.register(param)
        yield param
        parameter_registry.unregister(param.name)

    def test_a_tunable_renders_with_its_registry_bounds(self, a_registered_param):
        from src.api.control_surface import tunable_controls

        rows = [c for c in tunable_controls() if c.name == a_registered_param.name]

        assert len(rows) == 1
        row = rows[0]
        assert row.kind == "slider"
        assert row.live is True
        assert row.value == a_registered_param.current
        assert row.minimum == a_registered_param.floor
        assert row.maximum == a_registered_param.ceiling

    async def test_it_reaches_the_payload_and_sits_inside_its_bounds(
        self, surface_fields, a_registered_param
    ):
        surface = await build_control_surface(surface_fields)
        row = next(c for c in surface["controls"] if c["name"] == a_registered_param.name)

        assert row["min"] <= row["value"] <= row["max"]
