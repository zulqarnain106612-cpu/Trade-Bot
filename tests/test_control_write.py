"""
One entry point for writes, and not a second way in.

POST /controls/{name} exists so the dashboard has one way to change one thing.
The risk is that a unified endpoint becomes a bypass: somewhere to set a value
the dedicated endpoint would have refused. So apply_control is a router --
every write goes through the setter and the validator that already owned it --
and these tests assert it cannot write what those would reject.
"""

from __future__ import annotations

import pytest

from src.api.control_surface import ControlWriteError, apply_control
from src.api.main import SetRiskControlsRequest
from src.config import ExecutionMode
from src.tuning.registry import TunableParameter, parameter_registry

_OPERATOR = {"operator": "alice", "operator_secret": "irrelevant-here"}


async def _apply(name, value):
    return await apply_control(name, value, SetRiskControlsRequest, **_OPERATOR)


@pytest.fixture
def a_tunable():
    """A registered parameter to write to; the registry is empty at rest."""
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


class TestTheLivePathsWrite:
    async def test_execution_mode_is_settable(self):
        applied = await _apply("execution_mode", ExecutionMode.MANUAL.value)

        assert applied["value"] == ExecutionMode.MANUAL.value

    async def test_a_risk_control_is_settable_and_reports_what_landed(self):
        applied = await _apply("risk_controls.stop_loss_pct", 3.5)

        assert applied["name"] == "risk_controls.stop_loss_pct"
        assert applied["value"] == 3.5

    async def test_a_registered_tunable_is_settable(self, a_tunable):
        applied = await _apply(a_tunable.name, 0.75)

        assert applied["value"] == 0.75
        assert parameter_registry.get(a_tunable.name).current == 0.75


class TestItCannotBecomeABypass:
    @pytest.mark.parametrize(
        "name",
        ["risk.kelly_multiplier", "risk.max_position_size_pct", "trading_mode", "binance.api_key"],
    )
    async def test_a_protected_parameter_is_refused(self, name):
        with pytest.raises(ControlWriteError) as exc_info:
            await _apply(name, 1.0)

        assert exc_info.value.status == 403

    async def test_execution_mode_is_not_refused_as_protected(self):
        """
        Regression on the tier confusion: execution_mode sits in
        EXCLUDED_PARAMS (the autotuner may not move it) while being the one
        runtime switch the system documents. Checking exclusion before the
        operator-settable set refused it outright.
        """
        applied = await _apply("execution_mode", ExecutionMode.MANUAL.value)

        assert applied["value"] == ExecutionMode.MANUAL.value

    async def test_a_value_outside_the_validator_is_refused(self):
        with pytest.raises(ControlWriteError) as exc_info:
            await _apply("risk_controls.stop_loss_pct", 999.0)

        assert exc_info.value.status == 422

    async def test_a_tunable_outside_its_registry_bounds_is_refused(self, a_tunable):
        with pytest.raises(ControlWriteError) as exc_info:
            await _apply(a_tunable.name, 5.0)

        assert exc_info.value.status == 422
        assert parameter_registry.get(a_tunable.name).current == 0.5, "the write must not land"

    @pytest.mark.parametrize("name", ["nope", "risk_controls.no_such_field"])
    async def test_an_unknown_control_is_404_not_a_silent_no_op(self, name):
        with pytest.raises(ControlWriteError) as exc_info:
            await _apply(name, 1.0)

        assert exc_info.value.status == 404


class TestTheErrorSaysWhatIsActuallyWrong:
    async def test_an_out_of_range_value_names_the_real_constraint(self):
        with pytest.raises(ControlWriteError) as exc_info:
            await _apply("risk_controls.stop_loss_pct", 999.0)

        assert "50" in exc_info.value.detail

    async def test_a_non_numeric_value_says_so(self):
        with pytest.raises(ControlWriteError) as exc_info:
            await _apply("risk_controls.stop_loss_pct", "abc")

        assert "number" in exc_info.value.detail

    async def test_a_bad_enum_lists_the_real_options(self):
        with pytest.raises(ControlWriteError) as exc_info:
            await _apply("execution_mode", "bogus")

        assert exc_info.value.status == 422
        for mode in ExecutionMode:
            assert mode.value in exc_info.value.detail

    async def test_a_tunable_given_a_string_says_it_takes_a_number(self, a_tunable):
        with pytest.raises(ControlWriteError) as exc_info:
            await _apply(a_tunable.name, "not-a-number")

        assert exc_info.value.status == 422
        assert "number" in exc_info.value.detail

    async def test_a_missing_required_field_is_not_reported_as_a_bad_value(self):
        """
        The masking bug: _first_validation_message fell back to
        "<field>: invalid value" for any error, so a *missing required sibling*
        read as a problem with the value the caller sent -- sending the reader
        to the wrong place. A model missing operator/operator_secret must name
        those, not the field being written.
        """
        from src.api.control_surface import _apply_risk_control

        with pytest.raises(ControlWriteError) as exc_info:
            await _apply_risk_control(
                "stop_loss_pct",
                3.5,
                SetRiskControlsRequest,
                operator="",
                operator_secret=None,  # type: ignore[arg-type]
            )

        detail = exc_info.value.detail
        # The property: it names the sibling that is actually wrong, not the
        # field being written, whose value was perfectly valid.
        assert detail.startswith(("operator:", "operator_secret:")), detail
        assert "stop_loss_pct" not in detail
