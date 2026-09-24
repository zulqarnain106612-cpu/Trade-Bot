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
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ValidationError

from src.config import (
    ExecutionMode,
    get_settings,
    runtime_config,
    set_setting_override,
    settings_override_names,
)
from src.tuning.registry import parameter_registry

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


def _resolve_dotted(settings: Any, dotted: str) -> Any:
    """Read 'risk.kelly_multiplier' off the settings tree, or None."""
    current = settings
    for part in dotted.split("."):
        current = getattr(current, part, None)
        if current is None:
            return None
    return current if isinstance(current, (int, float, str, bool)) else None


# ---------------------------------------------------------------------------
# The settings tier: every configuration leaf, live.
# ---------------------------------------------------------------------------

# Bound or opened once, at startup, and not consulted again. An override does
# change the value a later read returns, so the write is real -- but uvicorn is
# already listening on the old port and the storage connection is already open,
# so the *system* does not change until a restart. Reporting these as live
# would be the exact lie this module exists to prevent.
_STARTUP_ONLY: frozenset[str] = frozenset(
    {
        "api.host",
        "api.port",
        "api.reload",
        "storage.backend",
        "storage.db_path",
        "storage.timescale_dsn",
    }
)

# Never rendered with a value and never writable here. A credential does not
# belong in a payload that is broadcast to every connected dashboard, and an
# endpoint that accepts one is a place to plant one.
_CREDENTIAL_MARKERS: tuple[str, ...] = ("api_key", "api_secret", "passphrase", "secret_key")

# Already surfaced by a dedicated tier with its own setter and semantics;
# listing them twice is the duplicate-row bug this module already fixed once.
_HANDLED_ELSEWHERE: frozenset[str] = frozenset({"execution_mode"})


def _is_credential(dotted: str) -> bool:
    return any(marker in dotted for marker in _CREDENTIAL_MARKERS)


def _field_bounds(info: Any) -> tuple[float | None, float | None]:
    low = high = None
    for meta in info.metadata:
        low = getattr(meta, "ge", low if low is not None else None) or low
        high = getattr(meta, "le", high if high is not None else None) or high
    return low, high


def _kind_for(annotation: Any, low: float | None, high: float | None) -> tuple[ControlKind, tuple]:
    """Pick the widget from the field's own type, not from its name."""
    options: tuple = ()
    if annotation is bool:
        return "toggle", options
    if isinstance(annotation, type) and issubclass(annotation, Enum):
        return "select", tuple(str(member.value) for member in annotation)
    if annotation in (int, float):
        # A slider needs both ends; one-sided or unbounded numerics get a
        # number field rather than an invented range.
        return ("slider" if low is not None and high is not None else "number"), options
    return "text", options


def _settings_leaves(model: Any, prefix: str = "") -> list[tuple[str, Any]]:
    leaves: list[tuple[str, Any]] = []
    for name, info in model.model_fields.items():
        annotation = info.annotation
        if isinstance(annotation, type) and hasattr(annotation, "model_fields"):
            leaves.extend(_settings_leaves(annotation, f"{prefix}{name}."))
        else:
            leaves.append((f"{prefix}{name}", info))
    return leaves


def settings_controls() -> list[Control]:
    """
    Every configuration leaf, with the tier it actually belongs to.

    This is what makes the hub cover the configuration rather than a corner of
    it: get_settings() returns live values, and all but a handful of reads in
    src/ go through it at use time, so a write here reaches the running system.
    """
    settings = get_settings()
    overridden = settings_override_names()
    controls: list[Control] = []

    for dotted, info in _settings_leaves(type(settings)):
        if dotted in _HANDLED_ELSEWHERE:
            continue

        group = dotted.split(".")[0] if "." in dotted else "general"

        if _is_credential(dotted):
            controls.append(
                Control(
                    name=dotted,
                    group="protected",
                    kind="readonly",
                    value=None,
                    live=False,
                    protected=True,
                    reason="credential -- never shown and never set from here",
                )
            )
            continue

        value = _resolve_dotted(settings, dotted)
        low, high = _field_bounds(info)
        startup_only = dotted in _STARTUP_ONLY
        kind, options = _kind_for(info.annotation, low, high)
        controls.append(
            Control(
                name=dotted,
                group=group,
                kind="readonly" if startup_only else kind,
                value=value,
                live=not startup_only,
                requires_restart=startup_only,
                reason=(
                    "bound at startup -- the value changes, the running server does not"
                    if startup_only
                    else ("overridden at runtime" if dotted in overridden else None)
                ),
                minimum=low,
                maximum=high,
                options=options,
            )
        )
    return controls


async def build_control_surface(risk_validator: type[BaseModel]) -> dict[str, Any]:
    """Every control the operator has, in one payload, tier-labelled."""
    controls = [await execution_mode_control()]
    controls.extend(await risk_controls(risk_validator))
    controls.extend(tunable_controls())
    controls.extend(settings_controls())

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
    # EXCLUDED_PARAMS answers what the *autotuner* may move. It does not
    # answer what the operator may move, and conflating the two locked the
    # owner out of their own risk limits: a position cap is excluded from
    # self-tuning precisely so that a human decides it, not so that nobody
    # can. ParameterRegistry still refuses to register any of them, so the
    # tuner remains barred; this path is the human with the second factor.
    #
    # Credentials are the real never. They are not shown and not settable:
    # an endpoint that accepts one is a place to plant one.
    if _is_credential(name):
        raise ControlWriteError(
            403, f"{name} is a credential and is never settable through this API."
        )
    if name in _STARTUP_ONLY:
        raise ControlWriteError(
            409,
            f"{name} is bound at startup; setting it here would change the value "
            "without changing the running server.",
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

    return _apply_setting(name, value)


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


def _apply_setting(name: str, value: Any) -> dict[str, Any]:
    """
    Write one configuration leaf through the live-override layer.

    set_setting_override re-validates the whole settings tree, so a value this
    accepts is one the environment could have supplied -- the field's own
    validators decide, exactly as at startup. A rejected value leaves the
    previous one in force.
    """
    try:
        updated = set_setting_override(name, value)
    except KeyError as exc:
        raise ControlWriteError(404, f"unknown control: {name}") from exc
    except ValidationError as exc:
        leaf = name.split(".")[-1]
        raise ControlWriteError(422, _first_validation_message(exc, leaf)) from exc
    return {"name": name, "value": _resolve_dotted(updated, name)}


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
