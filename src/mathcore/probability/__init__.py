"""
Probability for settlement risk and randomness auditing.

``confirmations`` owns ``poisson-block-arrival`` and
``gamblers-ruin-doublespend``; ``birthday`` owns ``birthday-bound``;
``entropy`` owns ``shannon-entropy``. The registry, not this file list, is the
record of what exists.
"""

from .birthday import birthday_bound, birthday_collision_probability
from .confirmations import (
    double_spend_probability,
    poisson_pmf,
    required_depth,
)
from .entropy import audit_rng, min_entropy, shannon_entropy

__all__ = [
    "audit_rng",
    "birthday_bound",
    "birthday_collision_probability",
    "double_spend_probability",
    "min_entropy",
    "poisson_pmf",
    "required_depth",
    "shannon_entropy",
]
