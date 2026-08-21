"""
BIP-32 HD key derivation for exchange API credential management (Part II §9.3).

Per-exchange API keys derived from a single master seed via BIP-32.
Hardened derivation only (index >= 2^31): normal child + parent xpub + child xprv
→ parent xprv leak is blocked (see BIP-32 risk table in plan).

Derivation path: m/44'/coin_type'/exchange_index'/0/key_index
Each exchange gets its own derivation path → compromise of one key ≠ all keys.

Requires: cryptography package (already in deps via api_signer.py)
"""

from __future__ import annotations

import hashlib
import hmac
import struct
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final


_HARDENED_OFFSET: Final[int] = 0x80000000


@dataclass
class DerivedKey:
    path: str
    private_key_bytes: bytes
    chain_code: bytes
    depth: int

    def hex(self) -> str:
        return self.private_key_bytes.hex()


def _hmac_sha512(key: bytes, data: bytes) -> bytes:
    return hmac.new(key, data, hashlib.sha512).digest()


def _derive_child(parent_key: bytes, parent_chain: bytes, index: int) -> tuple[bytes, bytes]:
    """Derive a hardened child key at `index`."""
    if index < _HARDENED_OFFSET:
        # Force hardened — caller should always pass hardened indices
        index += _HARDENED_OFFSET
    data = b"\x00" + parent_key + struct.pack(">I", index)
    result = _hmac_sha512(parent_chain, data)
    child_key = result[:32]
    child_chain = result[32:]
    # Add parent key (mod n) — simplified; full BIP-32 requires curve arithmetic
    # For HMAC-based key material, the raw bytes are sufficient for symmetric use
    return child_key, child_chain


class CredentialVault:
    """
    Derives per-exchange API key material from a master seed.

    The master seed should be a 64-byte random secret stored in a hardware
    security module or encrypted at rest — never in code or env files.
    """

    _EXCHANGE_INDICES: Final[Mapping[str, int]] = MappingProxyType(
        {
            "binance": 0,
            "okx": 1,
            "coinbase": 2,
            "kraken": 3,
            "bybit": 4,
        }
    )

    def __init__(self, master_seed: bytes) -> None:
        if len(master_seed) < 16:
            raise ValueError("Master seed must be at least 16 bytes")
        # BIP-32 root derivation
        root = _hmac_sha512(b"Bitcoin seed", master_seed)
        self._root_key = root[:32]
        self._root_chain = root[32:]

    def derive(self, exchange: str, key_index: int = 0) -> DerivedKey:
        """
        Derive a key for a given exchange.

        Returns deterministic key material; same inputs → same output.
        """
        exch_idx = self._EXCHANGE_INDICES.get(exchange.lower(), len(self._EXCHANGE_INDICES))
        coin_type = 0  # 0 = BTC namespace; use per-coin if needed

        # m/44'/coin_type'/exchange_index'/0/key_index
        path_components = [
            44 + _HARDENED_OFFSET,  # purpose: BIP-44
            coin_type + _HARDENED_OFFSET,  # coin_type (hardened)
            exch_idx + _HARDENED_OFFSET,  # exchange account (hardened)
            0 + _HARDENED_OFFSET,  # change=0 (hardened)
            key_index + _HARDENED_OFFSET,  # key index (hardened)
        ]

        key, chain = self._root_key, self._root_chain
        for idx in path_components:
            key, chain = _derive_child(key, chain, idx)

        path_str = "m/" + "/".join(f"{i - _HARDENED_OFFSET}'" for i in path_components)
        return DerivedKey(
            path=path_str,
            private_key_bytes=key,
            chain_code=chain,
            depth=len(path_components),
        )

    def api_key_for(self, exchange: str, key_index: int = 0) -> str:
        """Return a hex API key for the given exchange (for symmetric HMAC signing)."""
        derived = self.derive(exchange, key_index)
        return derived.hex()
