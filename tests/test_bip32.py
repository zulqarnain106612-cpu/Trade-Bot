"""
Tests for :mod:`src.mathcore.derivation.bip32` -- watch-only BIP-32 derivation.

Two things decide this module, and they are different in kind.

**Correctness** is decided against an independent oracle, not against recalled
strings. Every non-hardened child is computed a second way inside the test --
privately, from the published seed, by ``child_priv = parent_priv + IL`` -- and
the module's public-only derivation must land on the same point. Two
independent computations agreeing is evidence; a hardcoded expected string
typed from memory is not, and this project has shipped a fabricated constant
three times. The published master xpubs used here are held to the same
standard: each is checked to be what its published seed actually derives,
rather than trusted for being written down.

**Safety** is decided by the refusals, which carry most of the weight. A
watch-only module whose value is that it *cannot* produce a private key is
verified by showing what it refuses: hardened derivation, an extended private
key, a mistyped xpub, a child index past the serialisable depth.

Decides:
  - SECR-011 — Deposit-address derivation cannot produce or accept a private key
  - SECR-012 — Derived addresses match what a BIP-32 wallet derives, checked against an indepen
"""

from __future__ import annotations

import hashlib
import hmac
import inspect

import pytest

from src.mathcore.curves.secp256k1 import (
    CURVE_ORDER,
    GENERATOR,
    Point,
    scalar_multiply,
    serialize_point,
)
from src.mathcore.derivation import bip32
from src.mathcore.derivation.bip32 import (
    HARDENED_OFFSET,
    Bip32Error,
    ExtendedPublicKey,
    b58check_decode,
    b58check_encode,
    derive_addresses,
    derive_child,
    derive_path,
    hash160,
    parse_extended_key,
    parse_path,
)

# BIP-32 published test vectors 1 and 2: the seed, and the master extended
# PUBLIC key it produces. Only these two pairs are taken from the document --
# every child below is derived, never transcribed. `test_master_xpub_is_what_
# the_seed_derives` proves each pair is self-consistent, so a typo in either
# half fails loudly instead of silently redefining what the module must match.
VECTOR_1_SEED = bytes.fromhex("000102030405060708090a0b0c0d0e0f")
VECTOR_1_MASTER = (
    "xpub661MyMwAqRbcFtXgS5sYJABqqG9YLmC4Q1Rdap9gSE8NqtwybGhePY2gZ29ESFjqJoCu1"
    "Rupje8YtGqsefD265TMg7usUDFdp6W1EGMcet8"
)
VECTOR_2_SEED = bytes.fromhex(
    "fffcf9f6f3f0edeae7e4e1dedbd8d5d2cfccc9c6c3c0bdbab7b4b1aeaba8a5a2"
    "9f9c999693908d8a8784817e7b7875726f6c696663605d5a5754514e4b484542"
)
VECTOR_2_MASTER = (
    "xpub661MyMwAqRbcFW31YEwpkMuc5THy2PSt5bDMsktWQcFF8syAmRUapSCGu8ED9W6oDMSgv"
    "6Zz8idoc4a6mr8BDzTJY47LJhkJ8UB7WEGuduB"
)

VECTORS = [
    ("vector-1", VECTOR_1_SEED, VECTOR_1_MASTER),
    ("vector-2", VECTOR_2_SEED, VECTOR_2_MASTER),
]


# ---------------------------------------------------------------------------
# The oracle: private derivation, implemented only here
# ---------------------------------------------------------------------------


def _master_private(seed: bytes) -> tuple[int, bytes]:
    """The BIP-32 master key from a seed: (private scalar, chain code)."""
    digest = hmac.new(b"Bitcoin seed", seed, hashlib.sha512).digest()
    return int.from_bytes(digest[:32], "big"), digest[32:]


def _private_child(parent_key: int, parent_chain: bytes, index: int) -> tuple[int, bytes]:
    """
    One non-hardened private child, the way a seed holder would compute it.

    Deliberately a separate implementation from the module under test. The
    module adds IL*G to the parent *point*; this adds IL to the parent
    *scalar*. They agree only if the module is right, which is the property
    every correctness test here rests on.
    """
    parent_pub = serialize_point(scalar_multiply(parent_key, GENERATOR), compressed=True)
    digest = hmac.new(parent_chain, parent_pub + index.to_bytes(4, "big"), hashlib.sha512).digest()
    child_key = (int.from_bytes(digest[:32], "big") + parent_key) % CURVE_ORDER
    return child_key, digest[32:]


def _private_path(seed: bytes, indices: tuple[int, ...]) -> tuple[int, bytes]:
    key, chain = _master_private(seed)
    for index in indices:
        key, chain = _private_child(key, chain, index)
    return key, chain


# ---------------------------------------------------------------------------
# The published vectors are self-consistent
# ---------------------------------------------------------------------------


class TestPublishedVectors:
    @pytest.mark.parametrize("label,seed,master", VECTORS, ids=[v[0] for v in VECTORS])
    def test_master_xpub_is_what_the_seed_derives(
        self, label: str, seed: bytes, master: str
    ) -> None:
        """
        Pins the two constants this file takes on faith, so neither can be
        wrong without failing here.

        A transcription error in `master` would otherwise become the definition
        of correct for every other test in the file -- the module would be
        "verified" against a typo.
        """
        private_key, chain_code = _master_private(seed)
        parsed = parse_extended_key(master)
        assert parsed.chain_code == chain_code
        assert parsed.public_key == scalar_multiply(private_key, GENERATOR)
        assert parsed.depth == 0
        assert parsed.child_number == 0
        assert parsed.parent_fingerprint == b"\x00" * 4

    @pytest.mark.parametrize("label,seed,master", VECTORS, ids=[v[0] for v in VECTORS])
    def test_serialisation_round_trips(self, label: str, seed: bytes, master: str) -> None:
        parsed = parse_extended_key(master)
        assert parsed.to_base58() == master
        assert parse_extended_key(parsed.serialize()) == parsed
        assert len(parsed.serialize()) == 78


# ---------------------------------------------------------------------------
# Correctness: public derivation tracks private derivation
# ---------------------------------------------------------------------------

# Non-hardened paths only -- a hardened component cannot be derived from public
# data at all, which is its own test below. Depth 4 is past the point where a
# fencepost error in the chain-code hand-off would still agree by luck.
PATHS: list[tuple[int, ...]] = [
    (0,),
    (1,),
    (2,),
    (0, 0),
    (0, 1),
    (1, 0),
    (0, 0, 0),
    (1, 2, 3, 4),
    (0, 2_147_483_646),
    (2_147_483_646, 0, 1),
]


class TestPublicDerivationMatchesPrivate:
    @pytest.mark.parametrize("indices", PATHS, ids=["/".join(map(str, p)) for p in PATHS])
    @pytest.mark.parametrize("label,seed,master", VECTORS, ids=[v[0] for v in VECTORS])
    def test_point_and_chain_code_agree(
        self, label: str, seed: bytes, master: str, indices: tuple[int, ...]
    ) -> None:
        """
        The module's whole premise: adding the offset to the public point gives
        the same key as adding it to the private scalar.

        Catches a wrong HMAC input (BIP-32 feeds the *compressed* parent point,
        and an uncompressed or x-only variant produces a self-consistent tree
        that matches no other wallet), a chain code taken from the wrong half
        of the digest, and a missing reduction mod n.
        """
        expected_key, expected_chain = _private_path(seed, indices)

        derived = parse_extended_key(master)
        for index in indices:
            derived = derive_child(derived, index)

        assert derived.public_key == scalar_multiply(expected_key, GENERATOR)
        assert derived.chain_code == expected_chain
        assert derived.depth == len(indices)
        assert derived.child_number == indices[-1]

    @pytest.mark.parametrize("indices", PATHS, ids=["/".join(map(str, p)) for p in PATHS])
    def test_derive_path_equals_stepwise_derivation(self, indices: tuple[int, ...]) -> None:
        """`derive_path` is a convenience; it must not be a second algorithm."""
        master = parse_extended_key(VECTOR_1_MASTER)
        stepwise = master
        for index in indices:
            stepwise = derive_child(stepwise, index)
        assert derive_path(master, "m/" + "/".join(map(str, indices))) == stepwise

    def test_parent_fingerprint_chains_correctly(self) -> None:
        """
        A child records its parent's fingerprint, not its own and not the
        master's. Getting this wrong still derives correct keys, so nothing
        else here would catch it -- but the serialised xpub would not match
        any other wallet's for the same path.
        """
        master = parse_extended_key(VECTOR_1_MASTER)
        child = derive_child(master, 0)
        grandchild = derive_child(child, 1)
        assert child.parent_fingerprint == master.fingerprint
        assert grandchild.parent_fingerprint == child.fingerprint
        assert grandchild.parent_fingerprint != master.fingerprint

    def test_identifier_is_hash160_of_the_compressed_point(self) -> None:
        key = parse_extended_key(VECTOR_1_MASTER)
        assert key.identifier == hash160(serialize_point(key.public_key, compressed=True))
        assert key.fingerprint == key.identifier[:4]
        assert len(key.fingerprint) == 4

    def test_derivation_is_deterministic(self) -> None:
        master = parse_extended_key(VECTOR_1_MASTER)
        assert derive_child(master, 7) == derive_child(master, 7)

    def test_sibling_indices_give_different_keys(self) -> None:
        """A chain code fed without the index would make all siblings equal."""
        master = parse_extended_key(VECTOR_1_MASTER)
        keys = {derive_child(master, i).public_key for i in range(8)}
        assert len(keys) == 8


# ---------------------------------------------------------------------------
# Safety: what the module refuses
# ---------------------------------------------------------------------------


class TestHardenedDerivationIsRefused:
    """
    The property that makes this module safe to run on a trading host.

    Hardened derivation is defined over the parent private key. An
    implementation that returned *anything* here would be returning a key from
    a different tree -- and a caller would treat it as a deposit address.
    """

    @pytest.mark.parametrize(
        "index",
        [HARDENED_OFFSET, HARDENED_OFFSET + 1, HARDENED_OFFSET + 44, 0xFFFFFFFF],
    )
    def test_hardened_index_raises(self, index: int) -> None:
        master = parse_extended_key(VECTOR_1_MASTER)
        with pytest.raises(Bip32Error, match="hardened"):
            derive_child(master, index)

    @pytest.mark.parametrize("path", ["m/0'", "m/44'/0'/0'", "m/0/1'", "m/0h", "m/0/1H"])
    def test_hardened_path_raises_and_names_the_component(self, path: str) -> None:
        master = parse_extended_key(VECTOR_1_MASTER)
        with pytest.raises(Bip32Error, match="hardened"):
            derive_path(master, path)

    def test_the_last_non_hardened_index_still_works(self) -> None:
        """The boundary is exclusive: 2**31 - 1 is the largest normal index."""
        master = parse_extended_key(VECTOR_1_MASTER)
        derive_child(master, HARDENED_OFFSET - 1)

    def test_parse_path_reads_hardened_without_deriving_it(self) -> None:
        """
        Reading a hardened path is legitimate -- displaying it, or checking an
        xpub came from the right account. Deriving it is not. The split is
        deliberate, so it is pinned.
        """
        assert parse_path("m/44'/0'/0'") == (
            44 + HARDENED_OFFSET,
            HARDENED_OFFSET,
            HARDENED_OFFSET,
        )


class TestPrivateKeysCannotEnter:
    def test_extended_private_key_is_refused_by_name(self) -> None:
        """
        An operator pasting an xprv into a watch-only system needs to be told
        what they did, or they retry with the same string. A generic "unknown
        version" would not stop them.
        """
        payload = (
            (0x0488ADE4).to_bytes(4, "big")
            + b"\x00"
            + b"\x00" * 4
            + b"\x00" * 4
            + b"\x11" * 32
            + b"\x00"
            + b"\x22" * 32
        )
        with pytest.raises(Bip32Error, match="PRIVATE"):
            parse_extended_key(payload)
        with pytest.raises(Bip32Error, match="PRIVATE"):
            parse_extended_key(b58check_encode(payload))

    def test_testnet_private_version_is_refused_too(self) -> None:
        payload = (
            (0x04358394).to_bytes(4, "big")
            + b"\x00"
            + b"\x00" * 4
            + b"\x00" * 4
            + b"\x11" * 32
            + b"\x00"
            + b"\x22" * 32
        )
        with pytest.raises(Bip32Error, match="PRIVATE"):
            parse_extended_key(payload)

    def test_no_public_function_accepts_a_private_key(self) -> None:
        """
        Structural, not behavioural: the safety claim is that private material
        has nowhere to enter, so no public entry point may have a parameter
        that would hold one.

        This is the test that would notice a well-meaning future commit adding
        `derive_child(parent, index, private_key=None)`. Every other test here
        would still pass.
        """
        forbidden = ("priv", "secret", "seed", "xprv", "mnemonic", "passphrase")
        for name, obj in vars(bip32).items():
            if name.startswith("_") or not callable(obj) or not inspect.isfunction(obj):
                continue
            params = inspect.signature(obj).parameters
            offenders = [p for p in params if any(f in p.lower() for f in forbidden)]
            assert not offenders, f"{name}() takes private-key-shaped parameter(s) {offenders}"

    def test_extended_public_key_declares_no_private_field(self) -> None:
        forbidden = ("priv", "secret", "seed", "key_scalar")
        fields = ExtendedPublicKey.__dataclass_fields__
        offenders = [f for f in fields if any(x in f.lower() for x in forbidden)]
        assert not offenders, f"ExtendedPublicKey carries private-shaped field(s) {offenders}"


class TestCorruptedInputIsRefused:
    def test_a_mistyped_xpub_is_refused_not_silently_accepted(self) -> None:
        """
        The real case this guards: an xpub was transcribed with one wrong
        character while writing these very tests. Without the checksum it
        decodes to a different, structurally valid key -- so the system would
        watch someone else's address tree and report no deposits, with nothing
        anywhere reading as an error.
        """
        mistyped = (
            "xpub69H7F5d8KSRgmmdJg2KhpAK8SR3DjMwAdkxj3ZuxV27CprR9LgpeyGmXUbC6wb7ER"
            "fvrnKZjXoUhmM7XnWdfnKdPJhBHmDbgmzGTk2xCQAA"
        )
        with pytest.raises(Bip32Error, match="checksum"):
            parse_extended_key(mistyped)

    def test_single_character_change_fails_the_checksum(self) -> None:
        good = VECTOR_1_MASTER
        broken = good[:-1] + ("2" if good[-1] != "2" else "3")
        with pytest.raises(Bip32Error, match="checksum"):
            parse_extended_key(broken)

    @pytest.mark.parametrize("text", ["", "0", "l", "xpub0O", "not base58 at all!"])
    def test_malformed_base58_is_refused(self, text: str) -> None:
        with pytest.raises(Bip32Error):
            parse_extended_key(text)

    def test_wrong_length_payload_is_refused(self) -> None:
        with pytest.raises(Bip32Error, match="78 bytes"):
            parse_extended_key(b"\x04\x88\xb2\x1e" + b"\x00" * 40)

    def test_uncompressed_point_in_an_extended_key_is_refused(self) -> None:
        """
        BIP-32 fixes the compressed encoding. Accepting an uncompressed point
        would serialise back to a different 78 bytes and a different
        fingerprint, so every child below it would diverge from every other
        wallet.
        """
        payload = (
            (0x0488B21E).to_bytes(4, "big")
            + b"\x00"
            + b"\x00" * 4
            + b"\x00" * 4
            + b"\x11" * 32
            + b"\x04"
            + b"\x00" * 32
        )
        with pytest.raises(Bip32Error, match="compressed"):
            parse_extended_key(payload)

    def test_off_curve_point_is_refused(self) -> None:
        payload = (
            (0x0488B21E).to_bytes(4, "big")
            + b"\x00"
            + b"\x00" * 4
            + b"\x00" * 4
            + b"\x11" * 32
            + b"\x02"
            + b"\xff" * 32
        )
        with pytest.raises(Bip32Error, match="valid curve point"):
            parse_extended_key(payload)

    @pytest.mark.parametrize(
        "path", ["m/", "m//0", "m/x", "m/-1", "m/0/", "m/1.5", "m/999999999999", "m/2147483648"]
    )
    def test_malformed_paths_are_refused(self, path: str) -> None:
        with pytest.raises(Bip32Error):
            parse_path(path)

    def test_empty_path_derives_nothing(self) -> None:
        master = parse_extended_key(VECTOR_1_MASTER)
        assert derive_path(master, "m") == master
        assert derive_path(master, "M") == master

    @pytest.mark.parametrize("path", ["", "   ", "\t\n"])
    def test_a_blank_path_is_refused_not_treated_as_the_root(self, path: str) -> None:
        """
        A blank path almost always means an unset config value, and silently
        returning the parent would report the account xpub itself as a deposit
        address.
        """
        with pytest.raises(Bip32Error, match="empty derivation path"):
            parse_path(path)

    @pytest.mark.parametrize("path", ["0/1", "5", "0/0/0"])
    def test_relative_paths_without_a_leading_m_are_accepted(self, path: str) -> None:
        """
        Paths here are relative to the key passed in, not to a master key, so
        the leading `m` is optional punctuation rather than an assertion. A
        caller holding an account-level xpub writing "0/5" means the same thing
        as "m/0/5", and refusing one of the two spellings would be arbitrary.
        """
        master = parse_extended_key(VECTOR_1_MASTER)
        assert parse_path(path) == parse_path("m/" + path)
        assert derive_path(master, path) == derive_path(master, "m/" + path)

    @pytest.mark.parametrize("index", [-1, 0x100000000])
    def test_out_of_range_index_is_refused(self, index: int) -> None:
        master = parse_extended_key(VECTOR_1_MASTER)
        with pytest.raises(Bip32Error, match="out of range"):
            derive_child(master, index)


class TestConstructorInvariants:
    """
    A hand-built key that BIP-32 forbids must not be constructible.

    These are the fields a caller sets directly rather than deriving, so the
    constructor is the only thing standing between a typo and a key whose
    derivation paths reproduce nowhere.
    """

    @staticmethod
    def _valid_kwargs() -> dict:
        return {
            "version": bip32.MAINNET_PUBLIC_VERSION,
            "depth": 1,
            "parent_fingerprint": b"\x01\x02\x03\x04",
            "child_number": 0,
            "chain_code": b"\x11" * 32,
            "public_key": GENERATOR,
        }

    def test_valid_kwargs_construct(self) -> None:
        ExtendedPublicKey(**self._valid_kwargs())

    @pytest.mark.parametrize(
        "field,value,match",
        [
            ("version", 0x0488ADE4, "not a public extended key version"),
            ("version", 0xDEADBEEF, "not a public extended key version"),
            ("depth", 256, "depth out of range"),
            ("depth", -1, "depth out of range"),
            ("parent_fingerprint", b"\x01\x02\x03", "4 bytes"),
            ("chain_code", b"\x11" * 31, "32 bytes"),
            ("child_number", 0x100000000, "child_number out of range"),
            ("public_key", Point(None, None), "not a valid secp256k1 public key"),
        ],
    )
    def test_invalid_field_is_refused(self, field: str, value: object, match: str) -> None:
        kwargs = self._valid_kwargs()
        kwargs[field] = value
        with pytest.raises(Bip32Error, match=match):
            ExtendedPublicKey(**kwargs)

    def test_master_key_must_have_zero_parent(self) -> None:
        """
        Depth 0 with a non-zero parent means the key was re-parented or
        hand-edited. It derives fine, and reproduces nowhere.
        """
        kwargs = self._valid_kwargs() | {"depth": 0}
        with pytest.raises(Bip32Error, match="depth 0"):
            ExtendedPublicKey(**kwargs)
        ExtendedPublicKey(**(kwargs | {"parent_fingerprint": b"\x00" * 4}))

    def test_cannot_derive_past_the_serialisable_depth(self) -> None:
        """
        The wire form has one depth byte. A key at depth 255 has no
        representable child, and deriving one would produce a key that cannot
        be written out -- a value that looks fine until it is exported.
        """
        deep = ExtendedPublicKey(**(self._valid_kwargs() | {"depth": 255}))
        with pytest.raises(Bip32Error, match="depth"):
            derive_child(deep, 0)


class TestInvalidOffsetIsReported:
    """
    The 2**-127 branch, driven rather than waited for.

    BIP-32 says an index whose offset is out of range must be skipped, not
    substituted. Untested, this branch is where a future "just reduce it mod n"
    would go unnoticed -- and that silently returns a key from a different tree.
    """

    def test_offset_at_or_above_the_curve_order_is_refused(self, monkeypatch) -> None:
        master = parse_extended_key(VECTOR_1_MASTER)
        forced = CURVE_ORDER.to_bytes(32, "big") + b"\x33" * 32

        class _FakeHmac:
            def digest(self) -> bytes:
                return forced

        monkeypatch.setattr(bip32.hmac, "new", lambda *a, **k: _FakeHmac())
        with pytest.raises(Bip32Error, match="not a valid scalar"):
            derive_child(master, 0)

    def test_point_at_infinity_is_refused(self, monkeypatch) -> None:
        """
        IL*G == -parent_point, so the sum is the identity. Reachable only with
        a crafted chain code, and the identity is a public key that verifies
        every signature -- returning it would be the worst possible answer.
        """
        master = parse_extended_key(VECTOR_1_MASTER)
        monkeypatch.setattr(bip32, "add", lambda *a: Point(None, None))
        with pytest.raises(Bip32Error, match="infinity"):
            derive_child(master, 0)


# ---------------------------------------------------------------------------
# Address generation
# ---------------------------------------------------------------------------


class TestDeriveAddresses:
    def test_returns_consecutive_children(self) -> None:
        master = parse_extended_key(VECTOR_1_MASTER)
        keys = derive_addresses(master, 0, 5)
        assert [k.child_number for k in keys] == [0, 1, 2, 3, 4]
        assert keys == [derive_child(master, i) for i in range(5)]

    def test_honours_a_non_zero_start(self) -> None:
        master = parse_extended_key(VECTOR_1_MASTER)
        assert [k.child_number for k in derive_addresses(master, 100, 3)] == [100, 101, 102]

    def test_count_zero_returns_nothing(self) -> None:
        master = parse_extended_key(VECTOR_1_MASTER)
        assert derive_addresses(master, 0, 0) == []

    def test_skips_an_invalid_index_and_still_returns_count(self, monkeypatch) -> None:
        """
        The one place skipping is right: a gap-limit scan wants N usable keys.
        Every other entry point reports the invalid index, because there the
        caller named the child they wanted.
        """
        master = parse_extended_key(VECTOR_1_MASTER)
        real = bip32.derive_child

        def flaky(parent: ExtendedPublicKey, index: int) -> ExtendedPublicKey:
            if index == 2:
                raise Bip32Error("forced invalid offset")
            return real(parent, index)

        monkeypatch.setattr(bip32, "derive_child", flaky)
        keys = derive_addresses(master, 0, 4)
        assert [k.child_number for k in keys] == [0, 1, 3, 4]

    @pytest.mark.parametrize("start,count", [(-1, 1), (0, -1)])
    def test_negative_arguments_are_refused(self, start: int, count: int) -> None:
        master = parse_extended_key(VECTOR_1_MASTER)
        with pytest.raises(Bip32Error, match="must not be negative"):
            derive_addresses(master, start, count)

    def test_running_past_the_hardened_boundary_is_an_error(self) -> None:
        """
        Silently crossing into hardened indices would mean returning keys from
        a tree the caller cannot reproduce. Better to say the request does not
        fit at one level.
        """
        master = parse_extended_key(VECTOR_1_MASTER)
        with pytest.raises(Bip32Error, match="ran out of non-hardened indices"):
            derive_addresses(master, HARDENED_OFFSET - 2, 5)


# ---------------------------------------------------------------------------
# Primitives
# ---------------------------------------------------------------------------


class TestRipemd160:
    """
    RIPEMD-160 is bundled because OpenSSL 3 moved it to the legacy provider and
    many distributions ship it disabled. A fingerprint must not depend on how
    the host's OpenSSL was compiled, so both paths are checked to agree.
    """

    # ISO/IEC 10118-3 published vectors. Short enough to be checked by eye
    # against the standard, which is why they are safe to hardcode where an
    # xpub was not.
    VECTORS = [
        (b"", "9c1185a5c5e9fc54612808977ee8f548b2258d31"),
        (b"a", "0bdc9d2d256b3ee9daae347be6f4dc835a467ffe"),
        (b"abc", "8eb208f7e05d987a9b044a8e98c6b087f15a0bfc"),
        (b"message digest", "5d0689ef49d2fae572b881b123a85ffa21595f36"),
        (b"abcdefghijklmnopqrstuvwxyz", "f71c27109c692c1b56bbdceb5b9d2865b3708dbc"),
    ]

    @pytest.mark.parametrize(
        "data,expected", VECTORS, ids=[v[0].decode() or "empty" for v in VECTORS]
    )
    def test_pure_implementation_matches_the_standard(self, data: bytes, expected: str) -> None:
        assert bip32._ripemd160_pure(data).hex() == expected

    def test_multi_block_input_is_padded_correctly(self) -> None:
        """
        A length that crosses the 64-byte block boundary and leaves too little
        room for the length field is where a padding bug lives. The two
        implementations agreeing on it is the check.
        """
        for length in (0, 1, 54, 55, 56, 63, 64, 65, 119, 120, 128, 200):
            data = bytes(range(256))[:length] * 1
            assert bip32._ripemd160_pure(data) == bip32._ripemd160(data)

    def test_hash160_is_ripemd160_of_sha256(self) -> None:
        data = b"a public key would go here"
        assert hash160(data) == bip32._ripemd160(hashlib.sha256(data).digest())
        assert len(hash160(data)) == 20


class TestBase58Check:
    def test_round_trips(self) -> None:
        for payload in (b"\x00", b"\x00\x00\x01", b"\xff" * 32, bytes(range(40))):
            assert b58check_decode(b58check_encode(payload)) == payload

    def test_leading_zero_bytes_survive(self) -> None:
        """
        Base58 is an integer encoding, so leading zeros carry no value and must
        be re-added as '1' characters. Dropping them changes the payload
        length, which for an extended key means a wrong-length refusal at best.
        """
        payload = b"\x00\x00\x00\x2a"
        assert b58check_encode(payload).startswith("111")
        assert b58check_decode(b58check_encode(payload)) == payload

    def test_checksum_is_verified(self) -> None:
        encoded = b58check_encode(b"payload")
        broken = encoded[:-1] + ("2" if encoded[-1] != "2" else "3")
        with pytest.raises(Bip32Error, match="checksum"):
            b58check_decode(broken)

    def test_too_short_to_hold_a_checksum(self) -> None:
        with pytest.raises(Bip32Error, match="too short"):
            b58check_decode("12")

    @pytest.mark.parametrize("char", ["0", "O", "I", "l", "+", "/"])
    def test_ambiguous_characters_are_not_in_the_alphabet(self, char: str) -> None:
        """
        Base58 excludes 0/O/I/l precisely because they are misread. Accepting
        them would defeat the reason the format exists.
        """
        with pytest.raises(Bip32Error, match="invalid base58 character"):
            b58check_decode(f"xpub{char}")
