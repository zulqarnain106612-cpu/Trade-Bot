"""
Selfish mining and the profitability threshold for withholding blocks.

Owns the ``markov-selfish-mining`` registry entry.

A miner who withholds freshly found blocks and releases them strategically can,
above a certain hash-rate share, earn more than its fair share -- which breaks
the assumption that honest mining is the rational strategy and makes deep
reorganisations profitable. Eyal and Sirer's Markov-chain analysis gives the
exact relative revenue as a function of the attacker's hash-rate ``alpha`` and
``gamma``, the fraction of honest miners who mine on the attacker's block during
a tie. This module computes that revenue and finds the threshold ``alpha`` above
which selfish mining wins, so a chain's incentive assumptions can be checked
rather than asserted.

Deterministic float arithmetic on public parameters; constant-time does not
apply.

References: Eyal & Sirer, *Majority is not Enough: Bitcoin Mining is
Vulnerable* (2014); Sapirshtein, Sompolinsky & Zohar (2016) on optimal selfish
mining.
"""

from __future__ import annotations

__all__ = [
    "honest_revenue_share",
    "selfish_mining_revenue",
    "selfish_mining_threshold",
]


def selfish_mining_revenue(alpha: float, gamma: float) -> float:
    """
    The selfish miner's relative revenue under the Eyal-Sirer model.

    ``alpha`` is the attacker's hash-rate share and ``gamma`` the fraction of
    honest miners that build on the attacker's block in a tie. Returns the
    attacker's share of all blocks that end up on the main chain -- the closed
    form from the paper's Markov chain:

        R = [ alpha(1-alpha)^2 (4alpha + gamma(1-2alpha)) - alpha^3 ]
            / [ 1 - alpha(1 + (2-alpha)alpha) ]

    When this exceeds ``alpha`` the attacker earns more than its fair share.
    ``alpha`` must be in ``[0, 0.5)`` -- at half the attacker simply controls
    the chain and the model no longer applies -- and ``gamma`` in ``[0, 1]``.
    Not constant time.
    """
    if not 0 <= alpha < 0.5:
        raise ValueError(f"alpha must be in [0, 0.5), got {alpha}")
    if not 0 <= gamma <= 1:
        raise ValueError(f"gamma must be in [0, 1], got {gamma}")
    numerator = alpha * (1 - alpha) ** 2 * (4 * alpha + gamma * (1 - 2 * alpha)) - alpha**3
    denominator = 1 - alpha * (1 + (2 - alpha) * alpha)
    return numerator / denominator


def honest_revenue_share(alpha: float) -> float:
    """
    The revenue share an honest miner of hash-rate ``alpha`` earns: ``alpha``.

    Trivial, but named so a caller compares selfish revenue against the right
    baseline -- honest mining earns exactly its hash-rate share, and selfish
    mining is an attack only where it beats this. Not constant time.
    """
    if not 0 <= alpha < 0.5:
        raise ValueError(f"alpha must be in [0, 0.5), got {alpha}")
    return alpha


def selfish_mining_threshold(gamma: float, *, tolerance: float = 1e-9) -> float:
    """
    The smallest ``alpha`` at which selfish mining out-earns honest mining, for
    a given ``gamma``.

    Selfish revenue crosses ``alpha`` at a threshold that depends on ``gamma``:
    ``1/3`` for ``gamma = 0`` (the famous result), falling to ``0`` as
    ``gamma`` approaches ``1`` (a well-connected attacker needs almost no
    hash-rate). Found by bisection on ``selfish_mining_revenue(alpha) - alpha``,
    which is negative below the threshold and positive above it. Not constant
    time.

    For ``gamma = 1`` the threshold is ``0`` -- any share suffices -- and that
    is returned directly rather than bisected to a rounding artefact.
    """
    if not 0 <= gamma <= 1:
        raise ValueError(f"gamma must be in [0, 1], got {gamma}")
    if gamma >= 1.0:
        return 0.0

    def advantage(alpha: float) -> float:
        return selfish_mining_revenue(alpha, gamma) - alpha

    low, high = 0.0, 0.5 - tolerance
    # advantage(low) <= 0 and advantage(high) > 0; bisect for the crossing.
    for _ in range(200):
        mid = (low + high) / 2
        if advantage(mid) > 0:
            high = mid
        else:
            low = mid
        if high - low < tolerance:
            break
    return (low + high) / 2
