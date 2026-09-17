"""
Probability for settlement risk, consensus safety, and randomness auditing.

``confirmations`` owns ``poisson-block-arrival`` and
``gamblers-ruin-doublespend``; ``birthday`` owns ``birthday-bound``;
``entropy`` owns ``shannon-entropy``. The registry, not this file list, is the
record of what exists.
"""

from .birthday import birthday_bound, birthday_collision_probability
from .committee import (
    chernoff_committee_bound,
    committee_failure_probability,
    min_committee_size,
)
from .confirmations import (
    double_spend_probability,
    poisson_pmf,
    required_depth,
)
from .entropy import audit_rng, min_entropy, shannon_entropy
from .reorg import (
    honest_revenue_share,
    selfish_mining_revenue,
    selfish_mining_threshold,
)

__all__ = [
    "audit_rng",
    "birthday_bound",
    "birthday_collision_probability",
    "chernoff_committee_bound",
    "committee_failure_probability",
    "double_spend_probability",
    "honest_revenue_share",
    "min_committee_size",
    "min_entropy",
    "poisson_pmf",
    "required_depth",
    "selfish_mining_revenue",
    "selfish_mining_threshold",
    "shannon_entropy",
]
