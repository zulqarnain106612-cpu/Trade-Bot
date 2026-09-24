"""
What the operator can actually change while the bot is running, and what only
looks changeable.

The dashboard needs one honest answer to "what can I control right now". There
are three tiers and conflating them is the failure worth preventing:

  live      -- a write takes effect on the next read, no restart. RuntimeConfig
               holds these (execution mode, the exit controls) and the tuning
               registry holds the promoted overrides that live_overrides.py
               overlays onto settings at use time.
  static    -- a real setting that is read once, at construction. Showing it is
               useful; offering a control for it is a lie, because the write
               would appear to succeed and change nothing until a restart.
  protected -- registry.EXCLUDED_PARAMS. Hard risk limits (Kelly sizing,
               drawdown halts, position and notional caps) and exchange
               credentials, which can never be tuned at runtime by design --
               TunableParameter refuses to register them at all. These are
               surfaced as read-only *with the reason*, never as a control.

A control surface that misrepresents any of this is worse than none: an
operator who believes a slider moved a position-size cap, and is wrong, is in a
more dangerous position than one who knows they must edit config and restart.
Hence `live`, `requires_restart` and `protected` are explicit fields rather
than something the frontend infers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, ValidationError

from src.config import ExecutionMode, get_settings, runtime_config
from src.tuning.registry import EXCLUDED_PARAMS, parameter_registry

ControlKind = Literal["toggle", "slider", "select", "readonly"]

# The exit controls RuntimeConfig.set_risk_controls() accepts, paired with the
# kind of widget each one is. Derived from that signature rather than from
# RiskSettings: what makes a field live is having a runtime setter, not being
# a risk field, and the two sets are deliberately different.
_RISK_CONTROL_KINDS: dict[str, ControlKind] = {
    "stop_loss_enabled": "toggle",
    "stop_loss_pct": "slider",
    "take_profit_enabled": "toggle",
    "take_profit_pct": "slider",
    "max_holding_period_s": "slider",
    "trailing_stop_enabled": "toggle",
    "trailing_stop_pct": "slider",
}


def _bounds_from_model(
    model: type[BaseModel], field_name: str
) -> tuple[float | None, float | None]:
    """
    Read a field's ge/le straight off the Pydantic model that validates it.

    Not a copy of them. The first version of this module hardcoded fractions
    (0.001-0.50) while the real validator takes percentages (0.1-50.0), so the
    surface would have reported a live stop_loss_pct of 2.0 as below its own
    minimum -- a slider that misrepresents the value it is showing, which is
    the exact failure this module exists to prevent. One source of truth, read
    at runtime, cannot drift from the validator.
    """
    info = model.model_fields.get(field_name)
    if info is None:
        return (None, None)
    low = high = None
    for meta in info.metadata:
        low = getattr(meta, "ge", None) if low is None else low
        high = getattr(meta, "le", None) if high is None else high
    return (low, high)


@dataclass(frozen=True)
class Control:
    """One row of the control surface."""

    name: str
    group: str
    kind: ControlKind
    value: Any
    live: bool
    requires_restart: bool = False
    protected: bool = False
    reason: str | None = None
    minimum: float | None = None
    maximum: float | None = None
    options: tuple[str, ...] = field(default_factory=tuple)

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "group": self.group,
            "kind": self.kind,
            "value": self.value,
            "live": self.live,
            "requires_restart": self.requires_restart,
            "protected": self.protected,
            "reason": self.reason,
            "min": self.minimum,
            "max": self.maximum,
            "options": list(self.options),
        }


async def execution_mode_control() -> Control:
    mode = await runtime_config.get_execution_mode()
    return Control(
        name="execution_mode",
        group="execution",
        kind="select",
        value=str(mode),
        live=True,
        options=tuple(m.value for m in ExecutionMode),
    )


async def risk_controls(validator: type[BaseModel]) -> list[Control]:
    """
    The exit controls, which are live because RuntimeConfig owns them.

    `validator` is the request model the write endpoint already validates
    against, injected rather than imported so the bounds shown are by
    construction the bounds enforced -- and so this module does not import the
    router that imports it.
    """
    current = await runtime_config.get_risk_controls()
    controls: list[Control] = []
    for name, kind in _RISK_CONTROL_KINDS.items():
        if name not in current:
            # set_risk_controls() accepts it but this build does not track it;
            # omitting beats reporting a value that is not the live one.
            continue
        low, high = _bounds_from_model(validator, name)
        controls.append(
            Control(
                name=f"risk_controls.{name}",
                group="risk_controls",
                kind=kind,
                value=current[name],
                live=True,
                minimum=low,
                maximum=high,
            )
        )
    return controls


def tunable_controls() -> list[Control]:
    """
    Self-tuning parameters currently registered, as sliders.

    The registry is populated at runtime by the tuning scheduler, so this is
    empty on a bot that has never started it -- which is the truth, not a gap.
    """
    return [
        Control(
            name=param.name,
            group="tuning",
            kind="slider",
            value=param.current,
            live=True,
            minimum=param.floor,
            maximum=param.ceiling,
        )
        for param in parameter_registry.list_all()
    ]


# EXCLUDED_PARAMS answers "what may the autotuner move", which is not the same
# question as "what may an operator move", and conflating the two was a bug:
# execution_mode is excluded from self-tuning *and* deliberately operator-
# switchable through POST /execution-mode, so it was emitted twice -- once live,
# once protected -- by a surface whose entire purpose is not to misrepresent
# what it controls. Anything listed here is excluded from tuning but keeps its
# own gated operator path. trading_mode is deliberately absent: nothing exposes
# a setter for it, so protected is the truthful tier.
_OPERATOR_SETTABLE: frozenset[str] = frozenset({"execution_mode"})


def protected_controls() -> list[Control]:
    """
    The hard risk limits and credentials, shown read-only with the reason.

    Credentials are named but never valued: the point of listing them is to
    say "this is not tunable", and printing a key to say so would defeat it.
    """
    controls: list[Control] = []
    settings = get_settings()
    for name in sorted(EXCLUDED_PARAMS - _OPERATOR_SETTABLE):
        is_credential = "api_key" in name or "api_secret" in name or "passphrase" in name
        value: Any = None
        if not is_credential:
            value = _resolve_dotted(settings, name)
        controls.append(
            Control(
                name=name,
                group="protected",
                kind="readonly",
                value=value,
                live=False,
                protected=True,
                reason=(
                    "exchange credential -- never exposed"
                    if is_credential
                    else "hard risk limit -- never tunable at runtime "
                    "(docs/SELF_TUNING_DESIGN.md §3)"
                ),
            )
        )
    return controls


def _resolve_dotted(settings: Any, dotted: str) -> Any:
    """Read 'risk.kelly_multiplier' off the settings tree, or None."""
    current = settings
    for part in dotted.split("."):
        current = getattr(current, part, None)
        if current is None:
            return None
    return current if isinstance(current, (int, float, str, bool)) else None


async def build_control_surface(risk_validator: type[BaseModel]) -> dict[str, Any]:
    """Every control the operator has, in one payload, tier-labelled."""
    controls = [await execution_mode_control()]
    controls.extend(await risk_controls(risk_validator))
    controls.extend(tunable_controls())
    controls.extend(protected_controls())

    return {
        "controls": [c.as_dict() for c in controls],
        "counts": {
            "live": sum(1 for c in controls if c.live),
            "protected": sum(1 for c in controls if c.protected),
            "total": len(controls),
        },
    }


class ControlWriteError(Exception):
    """A control write that must not proceed. Carries the HTTP status to use."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


async def apply_control(
    name: str,
    value: Any,
    risk_validator: type[BaseModel],
    *,
    operator: str,
    operator_secret: str,
) -> dict[str, Any]:
    """
    Write one control through the setter that already owns it.

    This is a router, deliberately not a writer. Each tier keeps its existing
    validated path -- a risk control goes through the same Pydantic model the
    /risk-controls endpoint validates against, a tunable through
    ParameterRegistry.update_current and its bounds check. A unified endpoint
    that wrote to RuntimeConfig directly would be a way around both, which is
    the opposite of what one entry point is for.
    """
    # _OPERATOR_SETTABLE first: EXCLUDED_PARAMS is about the autotuner, and
    # execution_mode sits in both -- excluded from tuning, deliberately
    # operator-switchable. Checking exclusion first would have refused the one
    # runtime switch the system documents.
    if name in EXCLUDED_PARAMS and name not in _OPERATOR_SETTABLE:
        raise ControlWriteError(
            403,
            f"{name} is protected and can never be set at runtime "
            "(hard risk limit or credential; docs/SELF_TUNING_DESIGN.md §3).",
        )

    if name == "execution_mode":
        return await _apply_execution_mode(value)
    if name.startswith("risk_controls."):
        return await _apply_risk_control(
            name.removeprefix("risk_controls."),
            value,
            risk_validator,
            operator=operator,
            operator_secret=operator_secret,
        )
    if parameter_registry.is_registered(name):
        return _apply_tunable(name, value)

    raise ControlWriteError(404, f"unknown control: {name}")


async def _apply_execution_mode(value: Any) -> dict[str, Any]:
    try:
        mode = ExecutionMode(value)
    except ValueError as exc:
        valid = ", ".join(m.value for m in ExecutionMode)
        raise ControlWriteError(
            422, f"invalid execution_mode {value!r}; expected one of: {valid}"
        ) from exc
    await runtime_config.set_execution_mode(mode)
    return {"name": "execution_mode", "value": str(mode)}


async def _apply_risk_control(
    field_name: str,
    value: Any,
    validator: type[BaseModel],
    *,
    operator: str,
    operator_secret: str,
) -> dict[str, Any]:
    """
    Validate through the write endpoint's own model, then hand to RuntimeConfig.

    The model's other *optional* fields default to None, which
    set_risk_controls() reads as "leave unchanged", so one field moves and the
    rest are untouched -- the same partial-update semantics POST /risk-controls
    already has. Its required fields, operator and operator_secret, are passed
    through rather than faked: validating with a placeholder would exercise a
    different model than the one the real endpoint validates, which is the
    whole thing this router is built not to do, and the operator's name belongs
    on the write anyway.
    """
    if field_name not in _RISK_CONTROL_KINDS:
        raise ControlWriteError(404, f"unknown risk control: {field_name}")
    try:
        validated = validator(
            operator=operator, operator_secret=operator_secret, **{field_name: value}
        )
    except ValidationError as exc:
        raise ControlWriteError(422, _first_validation_message(exc, field_name)) from exc

    applied = await runtime_config.set_risk_controls(**{field_name: getattr(validated, field_name)})
    return {"name": f"risk_controls.{field_name}", "value": applied.get(field_name)}


def _apply_tunable(name: str, value: Any) -> dict[str, Any]:
    try:
        numeric = float(value)
    except (TypeError, ValueError) as exc:
        raise ControlWriteError(422, f"{name} takes a number, got {value!r}") from exc

    param = parameter_registry.get(name)
    if not param.in_bounds(numeric):
        raise ControlWriteError(
            422, f"{name}={numeric} is outside its bounds [{param.floor}, {param.ceiling}]"
        )
    updated = parameter_registry.update_current(name, numeric)
    return {"name": name, "value": updated.current}


def _first_validation_message(exc: ValidationError, field_name: str) -> str:
    """
    The field's own error where there is one, the real first error otherwise.

    The fallback used to read "<field>: invalid value" for *any* failure, which
    reported a missing required sibling as a problem with the value the caller
    sent -- a diagnostic that sends the reader to the wrong place entirely.
    """
    errors = exc.errors()
    for error in errors:
        if error.get("loc", (None,))[0] == field_name:
            return f"{field_name}: {error.get('msg', 'invalid value')}"
    if not errors:  # pragma: no cover - pydantic never raises with an empty list
        return f"{field_name}: invalid value"
    location = ".".join(str(part) for part in errors[0].get("loc", ()))
    return f"{location or field_name}: {errors[0].get('msg', 'invalid value')}"
