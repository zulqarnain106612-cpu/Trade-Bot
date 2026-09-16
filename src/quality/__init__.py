"""
Quality and security governance surface.

``registry`` loads ``config/quality_registry.json`` -- the single source of
truth for every requirement, trading invariant, regression and security
regression this project holds itself to, and for which test verifies each one.
"""

from src.quality.registry import (
    QualityRegistry,
    RegistryEntry,
    RegistryError,
    Verification,
    Waiver,
    default_registry,
    load_registry,
)

__all__ = [
    "QualityRegistry",
    "RegistryEntry",
    "RegistryError",
    "Verification",
    "Waiver",
    "default_registry",
    "load_registry",
]
