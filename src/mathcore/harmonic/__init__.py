"""
Harmonic analysis on lattices for :mod:`mathcore`.

``gaussian_lattice`` owns ``lattices-module-algebra`` and
``poisson-summation-smoothing`` -- the Module-LWE ring layer and the smoothing
parameter that every lattice security proof rests on.
"""

from .gaussian_lattice import (
    ModuleElement,
    assert_width_above_smoothing,
    is_above_smoothing,
    module_matrix_vector,
    smoothing_parameter,
)

__all__ = [
    "ModuleElement",
    "assert_width_above_smoothing",
    "is_above_smoothing",
    "module_matrix_vector",
    "smoothing_parameter",
]
