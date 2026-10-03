"""
Grover's algorithm and symmetric key margins.

Owns the ``grover-hash-margins`` registry entry.

Grover's algorithm finds a marked item among ``N`` in ``O(sqrt(N))`` queries
instead of ``O(N)``. Against a symmetric key or a hash preimage that is a
quadratic speedup: a ``b``-bit search costs ``2**(b/2)`` queries rather than
``2**b``, which is the sense in which a quantum adversary "halves" symmetric
strength, and the reason AES-256 rather than AES-128 is the post-quantum
recommendation.

**That halving is an upper bound on the adversary, not a description of one,
and this module says so in its return value.** Grover is a sequential
algorithm: its speedup comes from ``sqrt(N)`` *successive* iterations, and it
parallelises badly -- splitting the search across ``M`` machines recovers only
``sqrt(M)``, where classical brute force recovers ``M``. So the full quadratic
speedup assumes one coherent machine running ``2**(b/2)`` sequential
error-corrected operations, which for ``b = 128`` is far beyond any depth
budget anyone has proposed. NIST IR 8547 accordingly does not treat AES-128 as
broken.

Both numbers are therefore reported: the query-count bound, which is what the
mathematics gives, and the effective-strength figure under the halving
convention, which is what a policy is written against. A function returning
only one of them would be either alarmist or complacent depending on which,
and the caller cannot tell which they got.

This module computes no cryptography and holds no secret. It is arithmetic over
declared parameters, and its purpose is to make "we are post-quantum ready"
into a statement something can fail.

References: Grover 1996; Zalka 1999 (Grover is optimal, and the parallelisation
penalty); NIST IR 8547 (transition to post-quantum standards); Bernstein,
"Cost analysis of hash collisions" (why the depth constraint matters).
"""

from __future__ import annotations

from dataclasses import dataclass


class QuantumAssessmentError(ValueError):
    """
    A parameter that makes an assessment meaningless rather than negative.

    A key size of zero or a negative target is not a weak configuration, it is
    a malformed question, and answering it with a number would give a caller a
    verdict about nothing.
    """


@dataclass(frozen=True)
class GroverAssessment:
    """
    What Grover does to one primitive, with the caveat attached to the answer.

    ``effective_bits`` is the halving convention a policy is written against.
    ``query_cost_bits`` is the same number -- they coincide by construction --
    but named separately because ``sequential_depth_bits`` is what makes the
    difference between them matter: it is the number of *successive* quantum
    operations the attack requires, and it is the quantity that makes the
    halving pessimistic rather than predictive.

    Carrying the caveat in the dataclass rather than in a docstring is
    deliberate. A caller who reads only ``effective_bits`` gets a defensible
    conservative number; a caller who wants to argue that AES-128 is fine has
    to reach for ``sequential_depth_bits`` and say so out loud.
    """

    classical_bits: int
    effective_bits: float
    query_cost_bits: float
    sequential_depth_bits: float
    meets_target: bool
    target_bits: int

    @property
    def shortfall_bits(self) -> float:
        """Bits by which the primitive misses the target; 0.0 when it meets it."""
        return max(0.0, self.target_bits - self.effective_bits)


def _require_positive_bits(bits: int, what: str) -> None:
    if not isinstance(bits, int) or isinstance(bits, bool):
        raise QuantumAssessmentError(f"{what} must be an integer, got {type(bits).__name__}")
    if bits <= 0:
        raise QuantumAssessmentError(f"{what} must be positive, got {bits}")


def grover_query_cost_bits(classical_bits: int) -> float:
    """
    Log2 of the number of Grover queries to search a ``classical_bits`` space.

    ``b / 2``. This is the mathematical content of the quadratic speedup and
    nothing more: it counts queries, and says nothing about whether a machine
    can run that many in sequence within a coherence or depth budget.
    """
    _require_positive_bits(classical_bits, "classical_bits")
    return classical_bits / 2


def grover_effective_bits(classical_bits: int) -> float:
    """
    Effective post-quantum strength under the halving convention.

    Numerically identical to :func:`grover_query_cost_bits`; a separate name
    because it answers a different question. This is the figure a migration
    policy is written against -- "treat AES-128 as 64-bit" -- and is
    deliberately the conservative reading.

    Use :func:`assess_symmetric` in preference: it returns the depth caveat
    alongside, so the number cannot be quoted without it.
    """
    return grover_query_cost_bits(classical_bits)


def assess_symmetric(
    classical_bits: int,
    target_bits: int = 128,
) -> GroverAssessment:
    """
    Assess one symmetric primitive against a post-quantum target.

    ``target_bits`` defaults to 128 because that is the level the post-quantum
    recommendations are pitched at, and it is the caller's to change -- this
    module does not decide a security policy, it checks one.

    The returned assessment carries the sequential-depth figure precisely so
    that ``effective_bits`` cannot be quoted without the caveat that makes it
    conservative.
    """
    _require_positive_bits(classical_bits, "classical_bits")
    _require_positive_bits(target_bits, "target_bits")

    effective = grover_query_cost_bits(classical_bits)
    return GroverAssessment(
        classical_bits=classical_bits,
        effective_bits=effective,
        query_cost_bits=effective,
        # The attack's queries are successive, so its depth is its query count.
        # Stated as its own field because this is the number that has to be
        # believed physically achievable for the halving to be predictive.
        sequential_depth_bits=effective,
        meets_target=effective >= target_bits,
        target_bits=target_bits,
    )


def meets_post_quantum_target(classical_bits: int, target_bits: int = 128) -> bool:
    """Whether one primitive's halved strength clears ``target_bits``."""
    return assess_symmetric(classical_bits, target_bits).meets_target


def assess_suite(
    primitives: dict[str, int],
    target_bits: int = 128,
) -> dict[str, GroverAssessment]:
    """
    Assess a named set of primitives, so a suite can be checked in one call.

    Exists because the failure the registry entry names is not a wrong number
    about one cipher -- it is "migrating asymmetric primitives to post-quantum
    schemes while leaving 128-bit symmetric keys in place", which is a property
    of the whole suite and invisible while each primitive is examined alone.
    """
    if not primitives:
        raise QuantumAssessmentError("no primitives given; there is nothing to assess")
    return {name: assess_symmetric(bits, target_bits) for name, bits in sorted(primitives.items())}


def weakest_link(
    primitives: dict[str, int],
    target_bits: int = 128,
) -> tuple[str, GroverAssessment]:
    """
    The primitive in a suite with the lowest effective strength.

    The answer the entry's risk actually asks for. A suite is exactly as strong
    as this, so upgrading anything else first is motion without progress --
    which is what "leaves the weakest link untouched" describes.

    Ties break on the name, so the answer is deterministic rather than
    dependent on dict ordering.
    """
    assessments = assess_suite(primitives, target_bits)
    name = min(assessments, key=lambda key: (assessments[key].effective_bits, key))
    return name, assessments[name]
