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

References: Bowe, *BLS12-381* (2017); the IETF pairing-friendly-curves draft;
Costello, *Pairings for Beginners*; the algorithm follows the standard tower
construction used by every pairing library.
"""

from __future__ import annotations

from collections.abc import Sequence

__all__ = [
    "CURVE_ORDER",
    "FIELD_MODULUS",
    "FQ2",
    "FQ12",
    "G1",
    "G2",
    "g1_multiply",
    "g2_multiply",
    "pairing",
]

FIELD_MODULUS = 0x1A0111EA397FE69A4B1BA7B6434BACD764774B84F38512BF6730D2A0F6B0F6241EABFFFEB153FFFFB9FEFFFFFFFFAAAB
CURVE_ORDER = 0x73EDA753299D7D483339D80809A1D80553BDA402FFFE5BFEFFFFFFFF00000001
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
_B2 = FQ2([4, 4])


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


def pairing(q, p) -> FQP:
    """
    The optimal-ate pairing ``e(q, p)`` with ``q`` in G2 and ``p`` in G1.

    Returns an Fp12 element. Bilinear -- ``e(a q, b p) = e(q, p)^(ab)`` -- and
    non-degenerate, which are the two properties every use of a pairing relies
    on and the two the tests check. Rejects the identity in either argument,
    where the pairing is undefined as a useful value. Not constant time.
    """
    if _is_inf(p) or _is_inf(q):
        raise ValueError("pairing is not defined on the identity element")
    return _miller_loop(_twist(q), _cast_g1(p))
