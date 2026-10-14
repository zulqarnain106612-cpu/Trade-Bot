"""Name -> architecture resolution for the per-horizon model heads.

``config/horizons.yaml`` declares each horizon's models by name (``[tft, cnn]``).
Nothing mapped those names onto the head classes, so every architecture module
was unreachable and a typo in the config was silent. This module is that map.

Imports are deferred to call time: resolving a name must not drag torch into
processes that only read the horizon config (the API, the config validators).
"""

from __future__ import annotations

from importlib import import_module
from pathlib import Path
from typing import Any

import yaml

# name -> (module, class). Keep in sync with the ``models:`` values that
# ``config/horizons.yaml`` is allowed to use.
_ARCHITECTURES: dict[str, tuple[str, str]] = {
    "bert": ("src.models.bert_head", "BERTHead"),
    "cnn": ("src.models.cnn", "CNNHead"),
    "conformer": ("src.models.conformer", "ConformerHead"),
    "ecc": ("src.models.ecc_head", "ECCHead"),
    "gnn": ("src.models.gnn_head", "GNNHead"),
    "gru": ("src.models.gru", "GRUHead"),
    "lstm": ("src.models.lstm", "LSTMHead"),
    "mlp": ("src.models.mlp", "MLPHead"),
    "nbeats": ("src.models.nbeats", "NBEATSHead"),
    "patchtst": ("src.models.patchtst", "PatchTSTHead"),
    "tcn": ("src.models.tcn", "TCNHead"),
    "tft": ("src.models.tft", "TFTHead"),
}


# Names that may appear in a horizon's ``models:`` list but are training
# strategies rather than architectures. ``maml`` is the meta-learning adapter
# applied to h8/h10 on drift (see src/upgrade/maml.py); it wraps whichever head
# the horizon also declares, so it must validate but never resolve to a class.
_TRAINING_MODIFIERS: frozenset[str] = frozenset({"maml"})


class UnknownArchitectureError(KeyError):
    """A horizon named a model this build cannot construct."""


def available_architectures() -> tuple[str, ...]:
    """Every architecture name a horizon config may reference, sorted."""
    return tuple(sorted(_ARCHITECTURES))


def is_training_modifier(name: str) -> bool:
    """True if ``name`` is a training strategy rather than an architecture."""
    return name.strip().lower() in _TRAINING_MODIFIERS


def resolve_architecture(name: str) -> type:
    """Return the head class registered under ``name``.

    Raises ``UnknownArchitectureError`` rather than falling back to a default:
    silently substituting an architecture would train the wrong model for a
    horizon and the mistake would only surface as degraded live performance.
    """
    key = name.strip().lower()
    try:
        module_path, class_name = _ARCHITECTURES[key]
    except KeyError as exc:
        raise UnknownArchitectureError(
            f"unknown model architecture {name!r}; known: {', '.join(available_architectures())}"
        ) from exc
    return getattr(import_module(module_path), class_name)  # type: ignore[no-any-return]


def build_architecture(name: str, **overrides: Any) -> Any:
    """Instantiate the head registered under ``name``.

    Every head takes all-default constructor arguments, so ``overrides`` is only
    needed when the horizon pins a non-default width or lookback.
    """
    return resolve_architecture(name)(**overrides)


def horizon_model_names(config: dict[str, Any]) -> dict[str, list[str]]:
    """Map each horizon key to the model names it declares, modifiers included."""
    horizons = config.get("horizons") or {}
    return {key: list((spec or {}).get("models") or []) for key, spec in horizons.items()}


def horizon_architecture_names(config: dict[str, Any]) -> dict[str, list[str]]:
    """Map each horizon key to only the names that are buildable architectures."""
    return {
        key: [name for name in names if not is_training_modifier(name)]
        for key, names in horizon_model_names(config).items()
    }


def validate_horizon_architectures(config: dict[str, Any]) -> dict[str, list[str]]:
    """Check every model name in a horizon config resolves; return the mapping.

    Called at startup so a config typo fails immediately instead of at the first
    retrain, hours into a session.
    """
    names = horizon_model_names(config)
    unknown = {
        f"{horizon}:{model}"
        for horizon, models in names.items()
        for model in models
        if model.strip().lower() not in _ARCHITECTURES and not is_training_modifier(model)
    }
    if unknown:
        raise UnknownArchitectureError(
            f"horizon config references unknown architectures: {', '.join(sorted(unknown))}; "
            f"known: {', '.join(available_architectures())}"
        )
    return names


def load_horizon_architectures(path: Path) -> dict[str, list[str]]:
    """Load a horizon config file and validate the architectures it names.

    A missing file yields an empty mapping — horizons are optional, but a file
    that exists and names a bad architecture is an error.
    """
    try:
        with open(path) as fh:
            config = yaml.safe_load(fh) or {}
    except FileNotFoundError:
        return {}
    return validate_horizon_architectures(config)
