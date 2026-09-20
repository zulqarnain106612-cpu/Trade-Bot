"""
BLS12-381 pairing: the bilinear map behind aggregation and KZG commitments.

Owns the ``bilinear-pairings`` registry entry.

A pairing ``e: G1 x G2 -> GT`` is bilinear -- ``e(aP, bQ) = e(P, Q)^(ab)`` -- and
that identity turns a multiplicative relation in the curve groups into an
equality check in the target group. It is why BLS signatures aggregate and why
KZG polynomial commitments are constant size. BLS12-381 has embedding degree 12,
large enough that the MOV reduction (which collapses a small-embedding-degree
curve's discrete log into a finite-field one) does not apply -- the risk a naive
pairing-friendly curve runs.

This is a complete, from-scratch optimal-ate pairing built on a generic
extension-field type ``FQP``: the Fp -> Fp2 -> Fp12 tower by direct extension,
G1 over Fp and G2 over Fp2, the twist into Fp12, the Miller loop and the final
exponentiation. It is a **correctness reference** -- pure-Python big-integer
arithmetic, variable-time, slow, and not for a latency- or side-channel-
sensitive path. Its warrant is bilinearity, checked against random scalars, and
non-degeneracy against the group order; the tests additionally pin it to an
independent implementation.

Points that come from outside are validated where they enter --
:func:`g1_point`, :func:`g2_point` and :func:`pairing` -- and not again after
that. Both groups have a cofactor, so being on the curve is only half the
check: :func:`validate_g1` and :func:`validate_g2` also require prime order
``r``, without which a caller can be handed a small-subgroup point and made to
leak a secret scalar modulo its order. The identity is a member of every one of
these sets and is refused only by :func:`pairing`, identically for G1 and G2.

References: Bowe, *BLS12-381* (2017); the IETF pairing-friendly-curves draft;
Costello, *Pairings for Beginners*; the algorithm follows the standard tower
construction used by every pairing library.
"""

from __future__ import annotations

from collections.abc import Sequence

__all__ = [
    "B1",
    "B2",
    "COFACTOR_G1",
    "COFACTOR_G2",
    "CURVE_ORDER",
    "FIELD_MODULUS",
    "FQ2",
    "FQ12",
    "G1",
    "G2",
    "g1_multiply",
    "g1_point",
    "g2_multiply",
    "g2_point",
    "is_in_subgroup_g1",
    "is_in_subgroup_g2",
    "is_on_curve_g1",
    "is_on_curve_g2",
    "pairing",
    "validate_g1",
    "validate_g2",
]

FIELD_MODULUS = 0x1A0111EA397FE69A4B1BA7B6434BACD764774B84F38512BF6730D2A0F6B0F6241EABFFFEB153FFFFB9FEFFFFFFFFAAAB
CURVE_ORDER = 0x73EDA753299D7D483339D80809A1D80553BDA402FFFE5BFEFFFFFFFF00000001

#: ``b`` of the base curve ``E/Fq: y^2 = x^3 + 4``. The twist's ``b`` is
#: :data:`B2`, defined below because it needs :func:`FQ2`.
B1 = 4

#: ``#E(Fq) / r`` and ``#E'(Fq2) / r``. Both are far from 1, which is the whole
#: reason :func:`is_on_curve_g1` is not a sufficient check on its own: the curve
#: groups are strictly larger than the prime-order groups the pairing is defined
#: on, and the difference is where a small-subgroup attack lives.
COFACTOR_G1 = 0x396C8C005555E1568C00AAAB0000AAAB
COFACTOR_G2 = 0x5D543A95414E7F1091D50792876A202CD91DE4547085ABAA68A205B2E5A7DDFA628F1CB4D9E82EF21537E293A6691AE1616EC6E786F0C70CF1C38E31C7238E5

_ATE_LOOP_COUNT = 15132376222941642752  # |x| for BLS12-381 (x is negative)
_LOG_ATE_LOOP_COUNT = 62


class FQP:
    """
    An element of an extension field ``Fp[t]/(modulus)``, coefficients mod p.

    Generic over the modulus polynomial (given as its non-leading coefficients),
    so one implementation serves both Fp2 and Fp12. Kept deliberately small and
    literal -- multiplication is schoolbook then reduced -- because this is a
    reference whose value is being obviously correct, not fast.
    """

    __slots__ = ("coeffs", "modulus_coeffs")

    def __init__(self, coeffs: Sequence[int], modulus_coeffs: Sequence[int]) -> None:
        if len(coeffs) != len(modulus_coeffs):
            raise ValueError("coefficient count must match the field degree")
        self.coeffs = [c % FIELD_MODULUS for c in coeffs]
        self.modulus_coeffs = tuple(modulus_coeffs)

    @property
    def degree(self) -> int:
        return len(self.modulus_coeffs)

    def _new(self, coeffs: Sequence[int]) -> FQP:
        return FQP(coeffs, self.modulus_coeffs)

    def __add__(self, other: FQP) -> FQP:
        return self._new(
            [(a + b) % FIELD_MODULUS for a, b in zip(self.coeffs, other.coeffs, strict=True)]
        )

    def __sub__(self, other: FQP) -> FQP:
        return self._new(
            [(a - b) % FIELD_MODULUS for a, b in zip(self.coeffs, other.coeffs, strict=True)]
        )

    def __mul__(self, other: FQP | int) -> FQP:
        if isinstance(other, int):
            return self._new([c * other % FIELD_MODULUS for c in self.coeffs])
        degree = self.degree
        product = [0] * (2 * degree - 1)
        for i, a in enumerate(self.coeffs):
            if a:
                for j, b in enumerate(other.coeffs):
                    product[i + j] += a * b
        # Reduce modulo the field polynomial from the top down.
        for exp in range(len(product) - 1, degree - 1, -1):
            top = product[exp] % FIELD_MODULUS
            if top:
                for i, m in enumerate(self.modulus_coeffs):
                    product[exp - degree + i] -= top * m
            product[exp] = 0
        return self._new([c % FIELD_MODULUS for c in product[:degree]])

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, FQP):
            return NotImplemented
        return self.coeffs == other.coeffs

    def __neg__(self) -> FQP:
        return self._new([-c for c in self.coeffs])

    def is_zero(self) -> bool:
        return all(c == 0 for c in self.coeffs)

    def inv(self) -> FQP:
        """
        Inverse by the extended Euclidean algorithm on polynomials over Fp.

        The one non-trivial field operation; done with a full poly-gcd rather
        than a tower-specific shortcut, so it is correct for any modulus this
        class is instantiated with.
        """
        degree = self.degree
        lm, hm = [1] + [0] * degree, [0] * (degree + 1)
        low, high = [*self.coeffs, 0], [*self.modulus_coeffs, 1]
        while _poly_deg(low) > 0:
            r = _poly_round_div(high, low)
            r += [0] * (degree + 1 - len(r))
            nm = [x for x in hm]
            new = [x for x in high]
            for i in range(degree + 1):
                for j in range(degree + 1 - i):
                    nm[i + j] -= lm[i] * r[j]
                    new[i + j] -= low[i] * r[j]
            nm = [x % FIELD_MODULUS for x in nm]
            new = [x % FIELD_MODULUS for x in new]
            lm, low, hm, high = nm, new, lm, low
        factor = _inv(low[0])
        return self._new([c * factor % FIELD_MODULUS for c in lm[:degree]])

    def __pow__(self, exponent: int) -> FQP:
        result = one_like(self)
        base = self
        e = exponent
        while e:
            if e & 1:
                result = result * base
            base = base * base
            e >>= 1
        return result


def _poly_deg(coeffs: Sequence[int]) -> int:
    d = len(coeffs) - 1
    while d and coeffs[d] == 0:
        d -= 1
    return d


def _poly_round_div(a: Sequence[int], b: Sequence[int]) -> list[int]:
    a = list(a)
    degb = _poly_deg(b)
    temp = list(a)
    out = [0] * (len(a) - degb)
    for i in range(len(a) - degb - 1, -1, -1):
        out[i] = (out[i] + temp[degb + i] * _inv(b[degb])) % FIELD_MODULUS
        for c in range(degb + 1):
            temp[c + i] -= out[i] * b[c]
    return [x % FIELD_MODULUS for x in out[: _poly_deg(out) + 1]]


def _inv(a: int) -> int:
    return pow(a % FIELD_MODULUS, -1, FIELD_MODULUS)


def FQ2(coeffs: Sequence[int]) -> FQP:  # noqa: N802 - a constructor, capitalised on purpose
    """An Fp2 element ``c0 + c1 u`` with ``u^2 + 1 = 0``."""
    return FQP(coeffs, (1, 0))


def FQ12(coeffs: Sequence[int]) -> FQP:  # noqa: N802
    """An Fp12 element in the direct extension ``w^12 - 2 w^6 + 2 = 0``."""
    return FQP(coeffs, (2, 0, 0, 0, 0, 0, -2, 0, 0, 0, 0, 0))


def one_like(element: FQP) -> FQP:
    return FQP([1] + [0] * (element.degree - 1), element.modulus_coeffs)


def _fq12_one() -> FQP:
    return FQ12([1] + [0] * 11)


# ---- generators -----------------------------------------------------------

G1 = (
    3685416753713387016781088315183077757961620795782546409894578378688607592378376318836054947676345821548104185464507,
    1339506544944476473020471379941921221584933875938349620426543736416511423956333506472724655353366534992391756441569,
)
G2 = (
    FQ2(
        [
            352701069587466618187139116011060144890029952792775240219908644239793785735715026873347600343865175952761926303160,
            3059144344244213709971259814753781636986470325476647558659373206291635324768958432433509563104347017837885763365758,
        ]
    ),
    FQ2(
        [
            1985150602287291935568054521177171638300868978215655730859378665066344726373823718423869104263333984641494340347905,
            927553665492332455747201965776037880757740193453592970025027978793976877002675564980949289727957565575433344219582,
        ]
    ),
)


# ---- curve arithmetic (generic over the coordinate field) ------------------


def _is_inf(pt) -> bool:
    return pt is None


def _double(pt):
    if _is_inf(pt):
        return None
    x, y = pt
    if isinstance(x, FQP):
        if y.is_zero():
            return None
        slope = (x * x * 3) * (y * 2).inv()
        nx = slope * slope - x - x
        ny = slope * (x - nx) - y
        return (nx, ny)
    if y == 0:
        return None
    slope = (3 * x * x) * pow(2 * y, -1, FIELD_MODULUS) % FIELD_MODULUS
    nx = (slope * slope - 2 * x) % FIELD_MODULUS
    ny = (slope * (x - nx) - y) % FIELD_MODULUS
    return (nx, ny)


def _add(p, q):
    if _is_inf(p):
        return q
    if _is_inf(q):
        return p
    x1, y1 = p
    x2, y2 = q
    if isinstance(x1, FQP):
        if x1 == x2 and y1 == y2:
            return _double(p)
        if x1 == x2:
            return None
        slope = (y2 - y1) * (x2 - x1).inv()
        nx = slope * slope - x1 - x2
        ny = slope * (x1 - nx) - y1
        return (nx, ny)
    if x1 == x2 and y1 == y2:
        return _double(p)
    if x1 == x2:
        return None
    slope = (y2 - y1) * pow(x2 - x1, -1, FIELD_MODULUS) % FIELD_MODULUS
    nx = (slope * slope - x1 - x2) % FIELD_MODULUS
    ny = (slope * (x1 - nx) - y1) % FIELD_MODULUS
    return (nx, ny)


def _multiply(pt, scalar: int):
    if scalar == 0:
        return None
    if scalar == 1:
        return pt
    if scalar % 2 == 0:
        return _multiply(_double(pt), scalar // 2)
    return _add(_multiply(_double(pt), scalar // 2), pt)


def g1_multiply(scalar: int):
    """``scalar * G1`` on the base curve. Not constant time."""
    return _multiply(G1, scalar % CURVE_ORDER)


def g2_multiply(scalar: int):
    """``scalar * G2`` on the twist. Not constant time."""
    return _multiply(G2, scalar % CURVE_ORDER)


# ---- point validation ------------------------------------------------------

#: ``b`` of the twist ``E'/Fq2: y^2 = x^3 + 4(1 + u)``.
B2 = FQ2([4, 4])


def _coerce_fq2(value: FQP | Sequence[int]) -> FQP:
    """An Fp2 coordinate from either an ``FQP`` or a pair of integers."""
    if isinstance(value, FQP):
        if value.degree != 2:
            raise ValueError("an Fp2 coordinate has two coefficients")
        return value
    coeffs = list(value)
    if len(coeffs) != 2:
        raise ValueError("an Fp2 coordinate has two coefficients")
    if not all(0 <= c < FIELD_MODULUS for c in coeffs):
        raise ValueError("Fp2 coefficients must be reduced into [0, p)")
    return FQ2(coeffs)


def is_on_curve_g1(pt) -> bool:
    """
    Whether ``pt`` satisfies ``y^2 == x^3 + 4`` over Fq.

    The identity -- ``None`` here -- is on the curve, being the identity of the
    group the curve defines. That answer is the same for :func:`is_on_curve_g2`,
    :func:`is_in_subgroup_g1` and :func:`is_in_subgroup_g2`: the identity is a
    member of every one of these sets, and the one place it is refused is
    :func:`pairing`, where the value is undefined rather than merely degenerate.

    Coordinates outside ``[0, p)`` are **not** reduced first. An out-of-range
    coordinate is a malformed point, not an unreduced one, and accepting it here
    would let two encodings of one point past the checks that call this. Not
    constant time.
    """
    if _is_inf(pt):
        return True
    x, y = pt
    if not (isinstance(x, int) and isinstance(y, int)):
        return False
    if not (0 <= x < FIELD_MODULUS and 0 <= y < FIELD_MODULUS):
        return False
    return (y * y - x * x * x - B1) % FIELD_MODULUS == 0


def is_on_curve_g2(pt) -> bool:
    """
    Whether ``pt`` satisfies ``y^2 == x^3 + 4(1 + u)`` over Fq2.

    The twist has its own ``b``; checking a G2 point against the base curve's
    ``4`` would reject every honest point and is the mistake :data:`B2` exists
    to prevent. Identity handling is as in :func:`is_on_curve_g1`. Not constant
    time.
    """
    if _is_inf(pt):
        return True
    x, y = pt
    if not (isinstance(x, FQP) and isinstance(y, FQP)):
        return False
    if x.degree != 2 or y.degree != 2:
        return False
    return (y * y - x * x * x - B2).is_zero()


def _has_order_r(pt) -> bool:
    return _is_inf(_multiply(pt, CURVE_ORDER))


def is_in_subgroup_g1(pt) -> bool:
    """
    Whether ``pt`` has order dividing ``r`` -- that is, lies in G1 proper.

    On-curve is not enough. BLS12-381's G1 has cofactor :data:`COFACTOR_G1`, so
    ``E(Fq)`` contains points of small order that satisfy the curve equation
    perfectly well; feeding one to a scalar multiplication leaks the secret
    scalar modulo that small order, and a handful of such queries recover it.

    Computed the obvious way, ``r * pt == O``, which is a full scalar
    multiplication. This module is a correctness reference, so the clear check
    is the right one; a performance-sensitive implementation would use the
    endomorphism-based test instead. Not constant time.
    """
    return is_on_curve_g1(pt) and _has_order_r(pt)


def is_in_subgroup_g2(pt) -> bool:
    """
    Whether ``pt`` has order dividing ``r`` -- that is, lies in G2 proper.

    Same reasoning as :func:`is_in_subgroup_g1`, and more urgently: G2's
    cofactor :data:`COFACTOR_G2` is enormous, so an on-curve twist point drawn
    at random is essentially never in G2. Not constant time.
    """
    return is_on_curve_g2(pt) and _has_order_r(pt)


def validate_g1(pt) -> None:
    """
    Raise ``ValueError`` unless ``pt`` is a usable G1 element.

    On the curve **and** in the prime-order subgroup. The identity passes both;
    callers for which the identity is separately meaningless say so themselves,
    as :func:`pairing` does.
    """
    if not is_on_curve_g1(pt):
        raise ValueError("point is not on the BLS12-381 curve E/Fq")
    if not _has_order_r(pt):
        raise ValueError("point is on E/Fq but not in the prime-order subgroup G1")


def validate_g2(pt) -> None:
    """Raise ``ValueError`` unless ``pt`` is a usable G2 element. See :func:`validate_g1`."""
    if not is_on_curve_g2(pt):
        raise ValueError("point is not on the BLS12-381 twist E'/Fq2")
    if not _has_order_r(pt):
        raise ValueError("point is on E'/Fq2 but not in the prime-order subgroup G2")


def g1_point(x: int, y: int):
    """
    A validated G1 point from affine coordinates.

    This is the constructor external data goes through: anything parsed from
    bytes, read from a peer, or lifted out of a config becomes a point *here*,
    where it is checked once, rather than deep inside the Miller loop where a
    bad point is indistinguishable from a good one. Raises ``ValueError`` if the
    coordinates are not a G1 element. The identity is not expressible as a pair
    of coordinates; it is ``None``.
    """
    validate_g1((x, y))
    return (x, y)


def g2_point(x: FQP | Sequence[int], y: FQP | Sequence[int]):
    """
    A validated G2 point from affine Fp2 coordinates.

    Coordinates may be ``FQP`` elements or ``(c0, c1)`` integer pairs. The
    integer form is range-checked rather than reduced, for the reason given in
    :func:`is_on_curve_g1`. See :func:`g1_point`.
    """
    point = (_coerce_fq2(x), _coerce_fq2(y))
    validate_g2(point)
    return point


# ---- twist and pairing -----------------------------------------------------

_W = FQ12([0, 1] + [0] * 10)


def _twist(pt):
    """Map a G2 point over Fp2 into Fp12, per the standard sextic twist."""
    if _is_inf(pt):
        return None
    x, y = pt
    xc = [x.coeffs[0] - x.coeffs[1], x.coeffs[1]]
    yc = [y.coeffs[0] - y.coeffs[1], y.coeffs[1]]
    nx = FQ12([xc[0]] + [0] * 5 + [xc[1]] + [0] * 5)
    ny = FQ12([yc[0]] + [0] * 5 + [yc[1]] + [0] * 5)
    # Divide x by w^2 and y by w^3 (multiply by the inverse) -- the sextic
    # twist maps into the subfield where w^6 = x.
    return (nx * (_W * _W).inv(), ny * (_W * _W * _W).inv())


def _cast_g1(pt):
    x, y = pt
    return (FQ12([x] + [0] * 11), FQ12([y] + [0] * 11))


def _linefunc(p1, p2, t):
    """The line through ``p1, p2`` evaluated at ``t``, all over Fp12."""
    x1, y1 = p1
    x2, y2 = p2
    xt, yt = t
    if x1 != x2:
        slope = (y2 - y1) * (x2 - x1).inv()
        return slope * (xt - x1) - (yt - y1)
    if y1 == y2:
        slope = (x1 * x1 * 3) * (y1 * 2).inv()
        return slope * (xt - x1) - (yt - y1)
    return xt - x1


def _miller_loop(q, p):
    if _is_inf(q) or _is_inf(p):
        return _fq12_one()
    r = q
    f = _fq12_one()
    for i in range(_LOG_ATE_LOOP_COUNT, -1, -1):
        f = f * f * _linefunc(r, r, p)
        r = _double(r)
        if _ATE_LOOP_COUNT & (1 << i):
            f = f * _linefunc(r, q, p)
            r = _add(r, q)
    return f ** ((FIELD_MODULUS**12 - 1) // CURVE_ORDER)


def pairing(q, p, *, validate: bool = True) -> FQP:
    """
    The optimal-ate pairing ``e(q, p)`` with ``q`` in G2 and ``p`` in G1.

    Returns an Fp12 element. Bilinear -- ``e(a q, b p) = e(q, p)^(ab)`` -- and
    non-degenerate, which are the two properties every use of a pairing relies
    on and the two the tests check. Rejects the identity in either argument,
    where the pairing is undefined as a useful value. Not constant time.

    Both arguments are validated -- on the right curve, in the right
    prime-order subgroup -- because this is a boundary: an attacker who can
    choose a pairing argument and see the result is the classic setting for an
    invalid-curve or small-subgroup attack. ``validate=False`` skips the checks
    and is for a caller that has *already* validated the same points, typically
    in a loop over one fixed key; passing it on attacker-supplied data defeats
    the point of the parameter existing.
    """
    if validate:
        validate_g2(q)
        validate_g1(p)
    if _is_inf(p) or _is_inf(q):
        raise ValueError("pairing is not defined on the identity element")
    return _miller_loop(_twist(q), _cast_g1(p))
