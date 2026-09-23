"""
Shor's algorithm, and which secrets have to be migrated before it exists.

Owns the ``quantum-fourier-shor`` registry entry.

Shor's algorithm finds the period of a modular exponentiation in polynomial
time using the quantum Fourier transform. Factoring and discrete logarithms
both reduce to period finding, so a capable machine breaks RSA, finite-field
Diffie-Hellman, DSA, ECDH and ECDSA alike -- every asymmetric primitive
currently deployed, including the secp256k1 signatures on every chain this
project reads. Larger keys do not help: the cost is polynomial in the key
length, so 4096-bit RSA buys a constant factor, not a margin.

**This module implements no quantum algorithm and no resource estimate.**
There is nothing to run an algorithm on, and a simulator would say nothing
about a 256-bit curve. Resource estimates -- logical qubit counts, Toffoli
depths -- exist in the literature, are model-dependent, and would have to be
transcribed. This project has shipped a fabricated constant three times, and a
qubit count copied approximately from memory would be exactly that, with the
additional property that nobody can check it by running anything. So the
numbers are not here.

What *is* actionable, and is here, is the arithmetic the registry entry's risk
statement describes: **harvest-now-decrypt-later**. Traffic and on-chain
signatures captured today are decryptable once a capable machine exists, so a
secret's exposure is decided not by its strength but by whether its
confidentiality lifetime outlasts the date. That is a comparison of three
numbers the caller knows and one they must assume, and it is checkable.

The assumed date is a parameter, never a module constant. Dressing an
assumption up as a library value is how it stops being questioned, and the
year a cryptographically relevant quantum computer arrives is the most
consequential assumption in any migration plan.

References: Shor 1997; Proos-Zalka 2003 (ECDLP via Shor); NIST IR 8547
(transition timeline); Mosca's theorem (the X + Y > Z inequality this module
implements).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from src.mathcore.quantum.grover import QuantumAssessmentError


class SchemeFamily(Enum):
    """
    What a primitive rests on, which is what decides its quantum fate.

    Grouped by hard problem rather than by name, because that is the level at
    which Shor applies: ECDSA and ECDH share a fate not because they are
    similar protocols but because both reduce to the same discrete log.
    """

    FACTORING = "factoring"
    FINITE_FIELD_DLP = "finite_field_dlp"
    ELLIPTIC_CURVE_DLP = "elliptic_curve_dlp"
    SYMMETRIC = "symmetric"
    HASH = "hash"
    LATTICE = "lattice"
    CODE_BASED = "code_based"
    HASH_BASED_SIGNATURE = "hash_based_signature"
    UNKNOWN = "unknown"


# The families Shor breaks outright. Everything else is either merely weakened
# by Grover (see the grover module) or believed quantum-resistant.
_SHOR_BREAKS = frozenset(
    {
        SchemeFamily.FACTORING,
        SchemeFamily.FINITE_FIELD_DLP,
        SchemeFamily.ELLIPTIC_CURVE_DLP,
    }
)

# Scheme names to families. Deliberately a mapping of *names in use in this
# repository* rather than an attempt at a complete registry of cryptography:
# an unknown name returns UNKNOWN and is treated as needing migration, which
# is the safe direction for a name nobody has classified.
_FAMILIES: dict[str, SchemeFamily] = {
    "rsa": SchemeFamily.FACTORING,
    "rsa-oaep": SchemeFamily.FACTORING,
    "rsa-pss": SchemeFamily.FACTORING,
    "dh": SchemeFamily.FINITE_FIELD_DLP,
    "ffdh": SchemeFamily.FINITE_FIELD_DLP,
    "dsa": SchemeFamily.FINITE_FIELD_DLP,
    "elgamal": SchemeFamily.FINITE_FIELD_DLP,
    "ecdh": SchemeFamily.ELLIPTIC_CURVE_DLP,
    "ecdsa": SchemeFamily.ELLIPTIC_CURVE_DLP,
    "eddsa": SchemeFamily.ELLIPTIC_CURVE_DLP,
    "ed25519": SchemeFamily.ELLIPTIC_CURVE_DLP,
    "secp256k1": SchemeFamily.ELLIPTIC_CURVE_DLP,
    "schnorr": SchemeFamily.ELLIPTIC_CURVE_DLP,
    "bls12-381": SchemeFamily.ELLIPTIC_CURVE_DLP,
    "x25519": SchemeFamily.ELLIPTIC_CURVE_DLP,
    "aes": SchemeFamily.SYMMETRIC,
    "aes-gcm": SchemeFamily.SYMMETRIC,
    "chacha20": SchemeFamily.SYMMETRIC,
    "chacha20-poly1305": SchemeFamily.SYMMETRIC,
    "hmac": SchemeFamily.SYMMETRIC,
    "sha256": SchemeFamily.HASH,
    "sha512": SchemeFamily.HASH,
    "sha3-256": SchemeFamily.HASH,
    "blake2b": SchemeFamily.HASH,
    "ml-kem": SchemeFamily.LATTICE,
    "kyber": SchemeFamily.LATTICE,
    "ml-dsa": SchemeFamily.LATTICE,
    "dilithium": SchemeFamily.LATTICE,
    "falcon": SchemeFamily.LATTICE,
    "classic-mceliece": SchemeFamily.CODE_BASED,
    "slh-dsa": SchemeFamily.HASH_BASED_SIGNATURE,
    "sphincs+": SchemeFamily.HASH_BASED_SIGNATURE,
}


def classify_scheme(name: str) -> SchemeFamily:
    """
    The hard problem a named scheme rests on, or UNKNOWN.

    Case- and separator-insensitive, because the same primitive is spelled
    ``ML-KEM``, ``ml_kem`` and ``mlkem`` across specifications and config
    files, and a migration audit that missed one because of a hyphen would
    report a clean suite.
    """
    if not isinstance(name, str) or not name.strip():
        raise QuantumAssessmentError("a scheme name is required")
    key = name.strip().lower().replace("_", "-").replace(" ", "-")
    if key in _FAMILIES:
        return _FAMILIES[key]
    # Try the un-hyphenated spelling against un-hyphenated keys.
    flat = key.replace("-", "")
    for candidate, family in _FAMILIES.items():
        if candidate.replace("-", "") == flat:
            return family
    return SchemeFamily.UNKNOWN


def is_broken_by_shor(name: str) -> bool:
    """
    Whether Shor's algorithm breaks the named scheme outright.

    An unclassified name returns True. That is the safe direction: a name
    nobody has classified is more likely to be a deployed asymmetric primitive
    somebody forgot than a post-quantum scheme nobody added to the table, and
    a migration audit should surface it rather than pass it.
    """
    family = classify_scheme(name)
    return family in _SHOR_BREAKS or family is SchemeFamily.UNKNOWN


@dataclass(frozen=True)
class MigrationVerdict:
    """
    Whether one secret is exposed by harvest-now-decrypt-later.

    Mosca's inequality: if a secret must stay confidential for ``X`` years, and
    migrating the system takes ``Y`` years, and a capable machine arrives in
    ``Z`` years, then ``X + Y > Z`` means the secret is already exposed --
    migration finishes too late to protect data being captured now.

    ``years_of_exposure`` is ``X + Y - Z``: positive means already too late,
    which is a different and more urgent statement than "not yet compliant".
    """

    scheme: str
    family: SchemeFamily
    broken_by_shor: bool
    confidentiality_years: float
    migration_years: float
    years_until_capable_machine: float
    exposed: bool
    years_of_exposure: float
    reason: str


def migration_verdict(
    scheme: str,
    *,
    confidentiality_years: float,
    migration_years: float,
    years_until_capable_machine: float,
) -> MigrationVerdict:
    """
    Apply Mosca's inequality to one secret protected by one scheme.

    ``years_until_capable_machine`` is required and has no default on purpose.
    It is the assumption the whole answer turns on, and a default would let a
    caller obtain a verdict without ever deciding what they believe. There is
    no consensus value to supply.

    A scheme Shor does not break is not exposed by this mechanism whatever the
    timings, so it returns early with that as the reason -- the arithmetic is
    only meaningful for a primitive that is going to fall.
    """
    for label, value in (
        ("confidentiality_years", confidentiality_years),
        ("migration_years", migration_years),
        ("years_until_capable_machine", years_until_capable_machine),
    ):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise QuantumAssessmentError(f"{label} must be a number, got {type(value).__name__}")
        if value < 0:
            raise QuantumAssessmentError(f"{label} must not be negative, got {value}")

    family = classify_scheme(scheme)
    broken = is_broken_by_shor(scheme)

    if not broken:
        return MigrationVerdict(
            scheme=scheme,
            family=family,
            broken_by_shor=False,
            confidentiality_years=float(confidentiality_years),
            migration_years=float(migration_years),
            years_until_capable_machine=float(years_until_capable_machine),
            exposed=False,
            years_of_exposure=0.0,
            reason=(
                f"{scheme} rests on {family.value}, which Shor's algorithm does "
                "not break. Grover may still weaken it -- see "
                "mathcore.quantum.grover for symmetric margins."
            ),
        )

    overshoot = confidentiality_years + migration_years - years_until_capable_machine
    exposed = overshoot > 0
    if exposed:
        reason = (
            f"{scheme} is broken by Shor and X+Y > Z by {overshoot:g} years: a "
            "secret captured today is decryptable before migration completes. "
            "This is already-exposed, not not-yet-compliant."
        )
    else:
        reason = (
            f"{scheme} is broken by Shor, but X+Y <= Z with {-overshoot:g} years "
            "of headroom. Headroom shrinks as the estimate of Z moves in."
        )

    return MigrationVerdict(
        scheme=scheme,
        family=family,
        broken_by_shor=True,
        confidentiality_years=float(confidentiality_years),
        migration_years=float(migration_years),
        years_until_capable_machine=float(years_until_capable_machine),
        exposed=exposed,
        years_of_exposure=max(0.0, overshoot),
        reason=reason,
    )


def requires_migration(
    scheme: str,
    *,
    confidentiality_years: float,
    migration_years: float,
    years_until_capable_machine: float,
) -> bool:
    """Whether the named scheme leaves a secret exposed under these assumptions."""
    return migration_verdict(
        scheme,
        confidentiality_years=confidentiality_years,
        migration_years=migration_years,
        years_until_capable_machine=years_until_capable_machine,
    ).exposed


def exposed_schemes(
    schemes: dict[str, float],
    *,
    migration_years: float,
    years_until_capable_machine: float,
) -> list[MigrationVerdict]:
    """
    Every scheme in a suite whose secrets are already exposed, worst first.

    ``schemes`` maps a scheme name to that secret's confidentiality lifetime,
    because the lifetime is a property of what the scheme protects rather than
    of the scheme: the same ECDSA protects a session that matters for minutes
    and a custody key that matters for a decade.

    Ordered by exposure so a migration plan has a first item. Ties break on
    the scheme name, so the order is deterministic.
    """
    if not schemes:
        raise QuantumAssessmentError("no schemes given; there is nothing to assess")
    verdicts = [
        migration_verdict(
            name,
            confidentiality_years=years,
            migration_years=migration_years,
            years_until_capable_machine=years_until_capable_machine,
        )
        for name, years in schemes.items()
    ]
    exposed = [v for v in verdicts if v.exposed]
    return sorted(exposed, key=lambda v: (-v.years_of_exposure, v.scheme))
