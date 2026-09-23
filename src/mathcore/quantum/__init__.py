"""
Quantum threat assessment for :mod:`mathcore`.

Two modules, one per registry entry, and neither implements a quantum
algorithm: there is no machine to run one on, and a simulator would say nothing
about a 256-bit key. What is actionable today is the *assessment* -- which
primitives a quantum adversary breaks, which are merely weakened, and which
secrets must be migrated before a capable machine exists.

``grover`` owns ``grover-hash-margins``: the quadratic speedup on unstructured
search, and what it does and does not do to symmetric strength.

``shor`` owns ``quantum-fourier-shor``: period finding, which breaks every
deployed asymmetric primitive outright, and the harvest-now-decrypt-later
arithmetic that follows from it.

Both refuse to invent the one number everything depends on -- the year a
cryptographically relevant quantum computer exists. The caller supplies it,
because it is an assumption and dressing an assumption up as a library
constant is how it stops being questioned.
"""

from .grover import (
    GroverAssessment,
    QuantumAssessmentError,
    assess_symmetric,
    grover_effective_bits,
    grover_query_cost_bits,
    meets_post_quantum_target,
)
from .shor import (
    MigrationVerdict,
    SchemeFamily,
    classify_scheme,
    is_broken_by_shor,
    migration_verdict,
    requires_migration,
)

__all__ = [
    "GroverAssessment",
    "MigrationVerdict",
    "QuantumAssessmentError",
    "SchemeFamily",
    "assess_symmetric",
    "classify_scheme",
    "grover_effective_bits",
    "grover_query_cost_bits",
    "is_broken_by_shor",
    "meets_post_quantum_target",
    "migration_verdict",
    "requires_migration",
]
