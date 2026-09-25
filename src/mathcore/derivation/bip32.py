"""
BIP-32 hierarchical deterministic derivation, watch-only.

Owns the ``bip32-hd-derivation`` registry entry.

BIP-32 turns one seed into an unbounded tree of keys: child = parent + IL,
where IL is the left half of HMAC-SHA512(chain_code, parent_pubkey || index).
Because the offset IL depends only on public data, the same addition works on
public keys alone -- an extended *public* key can derive every non-hardened
descendant address without any private key existing in the process.

**This module derives public keys only, and cannot be made to derive private
ones.** That is a structural decision, not an omission. BIP-32's well-known
failure is that an extended public key plus any one non-hardened child private
key yields the parent private key by subtraction, and therefore the entire
branch: child_priv - IL = parent_priv, with IL computable by anyone holding the
xpub. A trading host needs deposit addresses, which is public derivation; it
does not need spending authority. Keeping private derivation out of this
process means an attacker who reads all of this module's memory learns which
addresses to watch, and nothing that lets them move funds.

Two consequences a caller must understand:

- **Hardened derivation is refused, not unimplemented.** Indices at or above
  2**31 hash the parent *private* key by definition, so no amount of public
  data can produce them. :func:`derive_child` raises instead of returning
  something; use hardened derivation at account level in the signing system
  that does hold the seed, and export the xpub from below that boundary.
- **An xpub is not a secret, but it is a privacy total.** One xpub links every
  address in its branch to one owner. Treat it as sensitive telemetry even
  though it cannot spend.

Not constant time. Every input here is public by construction, so there is no
secret for a timing adversary to recover -- and that is exactly why the module
refuses to accept one.

References: BIP-32, BIP-44, SLIP-0010; Bitcoin Core's ``xpub`` handling.

Registry: SECR-011, SECR-012 (config/quality_registry.json).
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
from dataclasses import dataclass

from src.mathcore.curves.secp256k1 import (
    CURVE_ORDER,
    GENERATOR,
    Point,
    add,
    parse_point,
    scalar_multiply,
    serialize_point,
    validate_public_key,
)

# Indices at or above this are hardened: BIP-32 defines them over the parent
# private key, so they are unreachable from public data by construction.
HARDENED_OFFSET = 2**31

# Version bytes of the serialised extended key. Only public versions appear
# here; the private ones (0x0488ADE4 / 0x04358394) are deliberately absent, so
# a private extended key cannot round-trip through this module even by
# accident.
MAINNET_PUBLIC_VERSION = 0x0488B21E
TESTNET_PUBLIC_VERSION = 0x043587CF
PUBLIC_VERSIONS = (MAINNET_PUBLIC_VERSION, TESTNET_PUBLIC_VERSION)

# The private version bytes, named only so that a private key offered to
# :func:`parse_extended_key` is refused with an accurate message rather than a
# generic "unknown version". Recognising them is what lets the refusal say why.
_PRIVATE_VERSIONS = (0x0488ADE4, 0x04358394)

_MAX_DEPTH = 0xFF

_B58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_B58_INDEX = {char: value for value, char in enumerate(_B58_ALPHABET)}


class Bip32Error(ValueError):
    """
    A derivation or parse that must not be allowed to return a value.

    A subclass of ValueError so a caller that only guards against bad input
    still catches it, and a distinct type so the security-relevant refusals --
    hardened derivation from public data, a private key offered to a watch-only
    parser -- can be asserted on specifically.
    """


# ---------------------------------------------------------------------------
# Hashing helpers
# ---------------------------------------------------------------------------


def _ripemd160(data: bytes) -> bytes:
    """
    RIPEMD-160, from hashlib when the linked OpenSSL provides it.

    Modern OpenSSL builds move RIPEMD-160 to the legacy provider and many
    distributions ship it disabled, so ``hashlib.new("ripemd160")`` raises on
    some hosts and works on others. A fingerprint is not worth an environment
    dependency, so :func:`_ripemd160_pure` is the fallback and the tests check
    both paths agree.
    """
    try:
        return hashlib.new("ripemd160", data).digest()
    except (ValueError, TypeError):  # pragma: no cover - host dependent
        return _ripemd160_pure(data)


# fmt: off
# RIPEMD-160 round schedule (ISO/IEC 10118-3), laid out 16 per row to match the
# tables in the standard. Tabulated rather than computed because the tables
# *are* the specification; a generated version would have to be checked against
# them anyway. `fmt: off` is load-bearing here: a formatter that puts one value
# per line turns these into 400 lines of digits that no reader will ever check
# against the standard, which is the only way they can be verified at all.
_R = (
    *range(16),
    7, 4, 13, 1, 10, 6, 15, 3, 12, 0, 9, 5, 2, 14, 11, 8,
    3, 10, 14, 4, 9, 15, 8, 1, 2, 7, 0, 6, 13, 11, 5, 12,
    1, 9, 11, 10, 0, 8, 12, 4, 13, 3, 7, 15, 14, 5, 6, 2,
    4, 0, 5, 9, 7, 12, 2, 10, 14, 1, 3, 8, 11, 6, 15, 13,
)
_RP = (
    5, 14, 7, 0, 9, 2, 11, 4, 13, 6, 15, 8, 1, 10, 3, 12,
    6, 11, 3, 7, 0, 13, 5, 10, 14, 15, 8, 12, 4, 9, 1, 2,
    15, 5, 1, 3, 7, 14, 6, 9, 11, 8, 12, 2, 10, 0, 4, 13,
    8, 6, 4, 1, 3, 11, 15, 0, 5, 12, 2, 13, 9, 7, 10, 14,
    12, 15, 10, 4, 1, 5, 8, 7, 6, 2, 13, 14, 0, 3, 9, 11,
)
_S = (
    11, 14, 15, 12, 5, 8, 7, 9, 11, 13, 14, 15, 6, 7, 9, 8,
    7, 6, 8, 13, 11, 9, 7, 15, 7, 12, 15, 9, 11, 7, 13, 12,
    11, 13, 6, 7, 14, 9, 13, 15, 14, 8, 13, 6, 5, 12, 7, 5,
    11, 12, 14, 15, 14, 15, 9, 8, 9, 14, 5, 6, 8, 6, 5, 12,
    9, 15, 5, 11, 6, 8, 13, 12, 5, 12, 13, 14, 11, 8, 5, 6,
)
_SP = (
    8, 9, 9, 11, 13, 15, 15, 5, 7, 7, 8, 11, 14, 14, 12, 6,
    9, 13, 15, 7, 12, 8, 9, 11, 7, 7, 12, 7, 6, 15, 13, 11,
    9, 7, 15, 11, 8, 6, 6, 14, 12, 13, 5, 14, 13, 13, 7, 5,
    15, 5, 8, 11, 14, 14, 6, 14, 6, 9, 12, 9, 12, 5, 15, 8,
    8, 5, 12, 9, 12, 5, 14, 6, 8, 13, 6, 5, 15, 13, 11, 11,
)
_K = (0x00000000, 0x5A827999, 0x6ED9EBA1, 0x8F1BBCDC, 0xA953FD4E)
_KP = (0x50A28BE6, 0x5C4DD124, 0x6D703EF3, 0x7A6D76E9, 0x00000000)
# fmt: on

_MASK32 = 0xFFFFFFFF


def _rotl(value: int, count: int) -> int:
    return ((value << count) | (value >> (32 - count))) & _MASK32


def _ripemd160_f(j: int, x: int, y: int, z: int) -> int:
    if j < 16:
        return x ^ y ^ z
    if j < 32:
        return (x & y) | (~x & z & _MASK32)
    if j < 48:
        return (x | (~y & _MASK32)) ^ z
    if j < 64:
        return (x & z) | (y & ~z & _MASK32)
    return x ^ (y | (~z & _MASK32))


def _ripemd160_pure(data: bytes) -> bytes:
    """
    RIPEMD-160 in pure Python, for hosts whose OpenSSL has dropped it.

    A direct transcription of ISO/IEC 10118-3. It exists so a BIP-32
    fingerprint never depends on how the host's OpenSSL was compiled; it is not
    a general-purpose hash implementation and is not constant time. The input
    is a public key, so there is nothing secret in it.
    """
    state = [0x67452301, 0xEFCDAB89, 0x98BADCFE, 0x10325476, 0xC3D2E1F0]

    padded = data + b"\x80"
    padded += b"\x00" * ((56 - len(padded) % 64) % 64)
    padded += (len(data) * 8 & 0xFFFFFFFFFFFFFFFF).to_bytes(8, "little")

    for offset in range(0, len(padded), 64):
        block = padded[offset : offset + 64]
        words = [int.from_bytes(block[i : i + 4], "little") for i in range(0, 64, 4)]

        a, b, c, d, e = state
        ap, bp, cp, dp, ep = state

        for j in range(80):
            rnd = j // 16
            t = _rotl(
                (a + _ripemd160_f(j, b, c, d) + words[_R[j]] + _K[rnd]) & _MASK32,
                _S[j],
            )
            a, b, c, d, e = e, (t + e) & _MASK32, b, _rotl(c, 10), d

            tp = _rotl(
                (ap + _ripemd160_f(79 - j, bp, cp, dp) + words[_RP[j]] + _KP[rnd]) & _MASK32,
                _SP[j],
            )
            ap, bp, cp, dp, ep = ep, (tp + ep) & _MASK32, bp, _rotl(cp, 10), dp

        state = [
            (state[1] + c + dp) & _MASK32,
            (state[2] + d + ep) & _MASK32,
            (state[3] + e + ap) & _MASK32,
            (state[4] + a + bp) & _MASK32,
            (state[0] + b + cp) & _MASK32,
        ]

    return b"".join(word.to_bytes(4, "little") for word in state)


def hash160(data: bytes) -> bytes:
    """RIPEMD160(SHA256(data)), the identifier hash BIP-32 fingerprints use."""
    return _ripemd160(hashlib.sha256(data).digest())


# ---------------------------------------------------------------------------
# Base58Check
# ---------------------------------------------------------------------------


def b58check_encode(payload: bytes) -> str:
    """Base58Check: payload plus the first four bytes of its double SHA-256."""
    checksum = hashlib.sha256(hashlib.sha256(payload).digest()).digest()[:4]
    raw = payload + checksum

    number = int.from_bytes(raw, "big")
    encoded = ""
    while number > 0:
        number, remainder = divmod(number, 58)
        encoded = _B58_ALPHABET[remainder] + encoded

    # Leading zero bytes carry no value in the integer but are significant in
    # the encoding, so each becomes one '1'. An xpub's 4-byte version never
    # starts with zero, but b58check is used on payloads that do.
    leading_zeros = len(raw) - len(raw.lstrip(b"\x00"))
    return "1" * leading_zeros + encoded


def b58check_decode(text: str) -> bytes:
    """
    Decode Base58Check, raising :class:`Bip32Error` on a bad checksum.

    The checksum is the whole point of the format: an xpub is transcribed by
    hand and pasted between systems, and a single flipped character silently
    yields a different valid-looking key -- which means watching an address
    tree that is not yours and reporting deposits that never arrive. Returning
    something for a corrupted string would be worse than failing.
    """
    if not text:
        raise Bip32Error("empty base58 string")

    number = 0
    for char in text:
        value = _B58_INDEX.get(char)
        if value is None:
            raise Bip32Error(f"invalid base58 character {char!r}")
        number = number * 58 + value

    leading_zeros = len(text) - len(text.lstrip("1"))
    body = number.to_bytes((number.bit_length() + 7) // 8, "big") if number else b""
    raw = b"\x00" * leading_zeros + body

    if len(raw) < 5:
        raise Bip32Error("base58 payload too short to carry a checksum")

    payload, checksum = raw[:-4], raw[-4:]
    expected = hashlib.sha256(hashlib.sha256(payload).digest()).digest()[:4]
    if not hmac.compare_digest(checksum, expected):
        raise Bip32Error("base58 checksum mismatch: the key is corrupted or mistyped")
    return payload


# ---------------------------------------------------------------------------
# Extended public keys
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ExtendedPublicKey:
    """
    A BIP-32 extended public key: a point plus the chain code that extends it.

    Frozen for the same reason :class:`~mathcore.curves.secp256k1.Point` is: a
    derivation walk holds several of these at once, and a caller mutating one
    in place would silently change the parent of an already-derived child.

    The dataclass carries no private field and no method that could produce
    one. There is nothing to audit for accidental secret handling, which is
    cheaper than auditing it.
    """

    version: int
    depth: int
    parent_fingerprint: bytes
    child_number: int
    chain_code: bytes
    public_key: Point

    def __post_init__(self) -> None:
        if self.version not in PUBLIC_VERSIONS:
            raise Bip32Error(f"not a public extended key version: {self.version:#010x}")
        if not 0 <= self.depth <= _MAX_DEPTH:
            raise Bip32Error(f"depth out of range: {self.depth}")
        if len(self.parent_fingerprint) != 4:
            raise Bip32Error("parent_fingerprint must be 4 bytes")
        if len(self.chain_code) != 32:
            raise Bip32Error("chain_code must be 32 bytes")
        if not 0 <= self.child_number <= 0xFFFFFFFF:
            raise Bip32Error(f"child_number out of range: {self.child_number}")
        if not validate_public_key(self.public_key):
            raise Bip32Error("public_key is not a valid secp256k1 public key")
        # A master key has depth 0, and BIP-32 requires its parent fingerprint
        # and child number to be zero. A non-zero one means the key was
        # re-parented or hand-edited, and its derivation paths would not
        # reproduce anywhere else.
        if self.depth == 0 and (self.parent_fingerprint != b"\x00" * 4 or self.child_number != 0):
            raise Bip32Error("depth 0 requires a zero parent_fingerprint and child_number")

    @property
    def identifier(self) -> bytes:
        """HASH160 of the compressed public key: this key's own identifier."""
        return hash160(serialize_point(self.public_key, compressed=True))

    @property
    def fingerprint(self) -> bytes:
        """The first four bytes of :attr:`identifier`, as children record it."""
        return self.identifier[:4]

    def serialize(self) -> bytes:
        """The 78-byte BIP-32 wire form."""
        return b"".join(
            (
                self.version.to_bytes(4, "big"),
                bytes([self.depth]),
                self.parent_fingerprint,
                self.child_number.to_bytes(4, "big"),
                self.chain_code,
                serialize_point(self.public_key, compressed=True),
            )
        )

    def to_base58(self) -> str:
        """The ``xpub...`` string form."""
        return b58check_encode(self.serialize())


def parse_extended_key(data: bytes | str) -> ExtendedPublicKey:
    """
    Parse a 78-byte extended key, or its Base58Check string.

    Refuses a private extended key by name rather than as an unknown version:
    an operator who pastes an ``xprv`` into a watch-only system has made a
    mistake worth telling them about precisely, and the accurate message is
    what stops them retrying with the same string.
    """
    raw = b58check_decode(data) if isinstance(data, str) else data

    if len(raw) != 78:
        raise Bip32Error(f"extended key must be 78 bytes, got {len(raw)}")

    version = int.from_bytes(raw[:4], "big")
    if version in _PRIVATE_VERSIONS:
        raise Bip32Error(
            "this is an extended PRIVATE key. This module is watch-only by "
            "design and will not parse one; export the xpub from the system "
            "that holds the seed and pass that instead."
        )

    key_bytes = raw[45:78]
    if key_bytes[0] not in (2, 3):
        raise Bip32Error(
            "public extended key must carry a compressed point (0x02/0x03 "
            f"prefix), got {key_bytes[0]:#04x}"
        )
    point = parse_point(key_bytes)
    if point is None:
        raise Bip32Error("extended key does not contain a valid curve point")

    return ExtendedPublicKey(
        version=version,
        depth=raw[4],
        parent_fingerprint=raw[5:9],
        child_number=int.from_bytes(raw[9:13], "big"),
        chain_code=raw[13:45],
        public_key=point,
    )


# ---------------------------------------------------------------------------
# Derivation
# ---------------------------------------------------------------------------


def derive_child(parent: ExtendedPublicKey, index: int) -> ExtendedPublicKey:
    """
    Derive one non-hardened child of ``parent``.

    Raises :class:`Bip32Error` when ``index`` is hardened. That is not a
    limitation to work around: a hardened child is defined as
    HMAC(chain_code, 0x00 || parent_private_key || index), so the parent
    private key is an input, and this module never has one. Any implementation
    that returned a value here would be returning a key from a different tree.

    Also raises when the derived offset is invalid -- IL >= n, or the resulting
    point is the identity. BIP-32 says to skip such an index rather than
    substitute anything, so this reports it and leaves the choice of the next
    index to the caller. The probability is about 2**-127; a caller that hits
    it has found a bug or a crafted chain code, and either deserves an
    exception rather than a silently different key.
    """
    if not 0 <= index <= 0xFFFFFFFF:
        raise Bip32Error(f"child index out of range: {index}")
    if index >= HARDENED_OFFSET:
        raise Bip32Error(
            f"index {index} is hardened (>= 2**31). Hardened derivation is "
            "defined over the parent private key and is impossible from an "
            "extended public key. Derive it in the system holding the seed."
        )
    if parent.depth >= _MAX_DEPTH:
        raise Bip32Error(
            f"cannot derive past depth {_MAX_DEPTH}: the serialised form has a "
            "single depth byte, so a deeper key could not be written out"
        )

    digest = hmac.new(
        parent.chain_code,
        serialize_point(parent.public_key, compressed=True) + index.to_bytes(4, "big"),
        hashlib.sha512,
    ).digest()
    offset = int.from_bytes(digest[:32], "big")
    child_chain_code = digest[32:]

    if offset >= CURVE_ORDER:
        raise Bip32Error(
            f"index {index} is invalid: the HMAC offset is not a valid scalar. "
            "Per BIP-32, skip to the next index."
        )

    child_point = add(scalar_multiply(offset, GENERATOR), parent.public_key)
    if child_point.x is None:
        raise Bip32Error(
            f"index {index} is invalid: derivation reached the point at "
            "infinity. Per BIP-32, skip to the next index."
        )

    return ExtendedPublicKey(
        version=parent.version,
        depth=parent.depth + 1,
        parent_fingerprint=parent.fingerprint,
        child_number=index,
        chain_code=child_chain_code,
        public_key=child_point,
    )


def parse_path(path: str) -> tuple[int, ...]:
    """
    Parse a BIP-32 path such as ``m/44/0/0/0`` into indices.

    Hardened components (``44'`` or ``44h``) parse to their hardened index
    rather than being rejected here, so :func:`derive_path` can refuse them
    with a message naming the component. Parsing and policy are separate
    because a caller may legitimately want to *read* a hardened path -- to
    display it, or to check that an xpub was exported from the right
    account -- without deriving it.
    """
    text = path.strip()
    if not text:
        raise Bip32Error("empty derivation path")

    parts = text.split("/")
    if parts[0] in ("m", "M"):
        parts = parts[1:]
    if not parts:
        return ()

    indices: list[int] = []
    for part in parts:
        if not part:
            raise Bip32Error(f"empty component in path {path!r}")
        hardened = part[-1] in ("'", "h", "H")
        number_text = part[:-1] if hardened else part
        if not number_text.isdigit():
            raise Bip32Error(f"invalid path component {part!r} in {path!r}")
        number = int(number_text)
        if number >= HARDENED_OFFSET:
            raise Bip32Error(
                f"path component {part!r} is out of range: indices below "
                "2**31 only, with ' or h marking hardened"
            )
        indices.append(number + HARDENED_OFFSET if hardened else number)
    return tuple(indices)


def derive_path(parent: ExtendedPublicKey, path: str) -> ExtendedPublicKey:
    """
    Walk a derivation path from ``parent``.

    The path is relative to ``parent``, not to the master key: an xpub exported
    at ``m/44'/0'/0'`` is the natural boundary for a watch-only system, and
    ``derive_path(account_xpub, "m/0/5")`` is the sixth receive address under
    it. The leading ``m`` is accepted and ignored for that reason; it does not
    assert that ``parent`` is a master key.
    """
    key = parent
    for position, index in enumerate(parse_path(path)):
        try:
            key = derive_child(key, index)
        except Bip32Error as exc:
            raise Bip32Error(f"{path!r} component {position}: {exc}") from exc
    return key


def derive_addresses(parent: ExtendedPublicKey, start: int, count: int) -> list[ExtendedPublicKey]:
    """
    Derive ``count`` consecutive children from ``start``, skipping invalid indices.

    This is the deposit-address-generation path, and the one place where
    skipping an invalid index is the right behaviour rather than the caller's
    decision: a gap-limit scan wants ``count`` usable keys, and an index whose
    offset happens to be invalid is not a usable key. Every other entry point
    reports the invalid index instead, because there the caller asked for that
    specific child and silently returning a different one would be wrong.
    """
    if count < 0:
        raise Bip32Error(f"count must not be negative: {count}")
    if start < 0:
        raise Bip32Error(f"start must not be negative: {start}")

    keys: list[ExtendedPublicKey] = []
    index = start
    while len(keys) < count:
        if index >= HARDENED_OFFSET:
            raise Bip32Error(
                f"ran out of non-hardened indices before {count} keys were "
                f"derived (reached {index}); the request is too large for one level"
            )
        # An invalid offset at this index is not an error here: BIP-32 says to
        # skip such an index, and a gap-limit scan wants `count` usable keys.
        with contextlib.suppress(Bip32Error):
            keys.append(derive_child(parent, index))
        index += 1
    return keys
