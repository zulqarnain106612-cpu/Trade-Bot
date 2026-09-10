"""
Harmonic analysis on lattices for :mod:`mathcore`.

``gaussian_lattice`` owns ``lattices-module-algebra`` and
``poisson-summation-smoothing``; ``walsh_hadamard`` owns ``walsh-hadamard`` (the
S-box nonlinearity metrics); ``fft_spectral`` owns ``fft-side-channel`` (the
frequency-domain view of a side-channel trace).
"""

from .fft_spectral import (
    dft,
    dominant_frequency,
    fft,
    ifft,
    power_spectrum,
)
from .gaussian_lattice import (
    ModuleElement,
    assert_width_above_smoothing,
    is_above_smoothing,
    module_matrix_vector,
    smoothing_parameter,
)
from .walsh_hadamard import (
    correlation_immunity,
    is_bent,
    nonlinearity,
    walsh_hadamard_transform,
)

__all__ = [
    "ModuleElement",
    "assert_width_above_smoothing",
    "correlation_immunity",
    "dft",
    "dominant_frequency",
    "fft",
    "ifft",
    "is_above_smoothing",
    "is_bent",
    "module_matrix_vector",
    "nonlinearity",
    "power_spectrum",
    "smoothing_parameter",
    "walsh_hadamard_transform",
]
