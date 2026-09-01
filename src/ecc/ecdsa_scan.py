"""
ECDSA r/s nonce-reuse detection and weak-key mining.

If two signatures share the same nonce r, the private key can be algebraically
extracted. This is a known vulnerability in ECDSA when a CSPRNG is broken or
the signing software is faulty (e.g. PlayStation 3 breach, 2010 Bitcoin wallet
incidents).

Detecting nonce reuse in on-chain transactions signals compromised addresses
associated with known exchanges or custodians → short signal.

Output: ecdsa_weakness_score ∈ [0, 1]
  0.0 → no weaknesses detected
  0.95 → near-certain private key extractable (nonce reuse confirmed)
"""

from __future__ import annotations

import hashlib
import struct
from collections import OrderedDict, deque
from dataclasses import dataclass, field

import structlog

log: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

# Distinct r values held for nonce-reuse detection. Each entry is an int
# plus a short tuple list, so 250k is on the order of tens of MB — large
# enough to span a long scanning session, bounded enough that a worker fed
# a transaction stream indefinitely cannot exhaust memory.
_MAX_TRACKED_R: int = 250_000

# Signatures retained per r value. Reuse is reported on the second sighting,
# so beyond a handful this stores nothing that changes a detection.
_MAX_SIGS_PER_R: int = 8

# Detected weaknesses retained. Findings are logged at warning as they
# occur, so this window is for the risk-score accessor rather than the
# record of what was found.
_MAX_RETAINED_WEAKNESSES: int = 10_000


@dataclass
class ECDSAWeakness:
    pubkey_hex: str
    r_value: int
    risk_score: float
    txids: list[str] = field(default_factory=list)
    privkey_extracted: bool = False


def _parse_der_signature(der: bytes) -> tuple[int, int] | None:
    """
    Parse a DER-encoded ECDSA signature and return (r, s).

    DER format: 0x30 [total_len] 0x02 [r_len] [r] 0x02 [s_len] [s]
    """
    if len(der) < 8 or der[0] != 0x30:
        return None
    try:
        i = 2
        if der[i] != 0x02:
            return None
        r_len = der[i + 1]
        r = int.from_bytes(der[i + 2 : i + 2 + r_len], "big")
        i += 2 + r_len
        if der[i] != 0x02:
            return None
        s_len = der[i + 1]
        s = int.from_bytes(der[i + 2 : i + 2 + s_len], "big")
        return r, s
    except (IndexError, struct.error):
        return None


def extract_ecdsa_signatures(raw_tx_hex: str) -> list[tuple[int, int, bytes, str]]:
    """
    Extract (r, s, pubkey_bytes, txid) tuples from a raw hex transaction.

    Simplified parser that handles P2PKH and P2WPKH script types.
    For full coverage a proper script interpreter is needed.
    Returns list of (r, s, pubkey, txid) tuples.
    """
    results = []
    try:
        raw = bytes.fromhex(raw_tx_hex)
        txid = hashlib.sha256(hashlib.sha256(raw).digest()).digest()[::-1].hex()

        # Walk inputs looking for scriptSig / witness data
        # This is a simplified DER scanner — finds DER sequences in the raw bytes
        i = 0
        while i < len(raw) - 8:
            if raw[i] == 0x30 and raw[i + 1] < 0x50:
                total_len = raw[i + 1]
                if i + 2 + total_len <= len(raw):
                    der_candidate = raw[i : i + 2 + total_len]
                    parsed = _parse_der_signature(der_candidate)
                    if parsed is not None:
                        r, s = parsed
                        # Try to find 33-byte compressed pubkey after the sig
                        pubkey_offset = i + 2 + total_len + 1  # skip sighash byte
                        if pubkey_offset + 33 <= len(raw) and raw[pubkey_offset] in (0x02, 0x03):
                            pubkey = raw[pubkey_offset : pubkey_offset + 33]
                            results.append((r, s, pubkey, txid))
                            i = pubkey_offset + 33
                            continue
            i += 1
    except Exception as exc:
        log.debug("ecdsa_parse_error", exc=str(exc))
    return results


_SECP256K1_N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141


def _extract_privkey_from_nonce_reuse(
    r: int,
    s1: int,
    s2: int,
    z1: int,
    z2: int,
) -> int | None:
    """
    Given two signatures (r, s1, z1) and (r, s2, z2) sharing the same r (nonce k),
    recover the private key d.

    From the ECDSA signing equation:
        s = k^-1 (z + r*d) mod n
        s1 - s2 = k^-1 (z1 - z2) mod n
        k = (z1 - z2) * modinv(s1 - s2, n) mod n
        d = (s1*k - z1) * modinv(r, n) mod n
    """
    n = _SECP256K1_N
    s_diff = (s1 - s2) % n
    if s_diff == 0:
        return None
    z_diff = (z1 - z2) % n
    try:
        k = z_diff * pow(s_diff, -1, n) % n
        if k == 0:
            return None
        d = (s1 * k - z1) * pow(r, -1, n) % n
        return d if d != 0 else None
    except Exception:
        return None


class ECDSAScanner:
    """
    Streaming scanner that tracks r-values across transactions and flags reuse.

    The scanner accumulates r → [(s, pubkey, txid, z)] entries.  When a
    second entry with the same r arrives, it attempts private key extraction
    and emits a weakness alert.
    """

    def __init__(self, max_tracked_r: int = _MAX_TRACKED_R) -> None:
        # r → list of (s, pubkey_hex, txid, z_hash)
        #
        # Bounded LRU rather than an unbounded map. Detecting nonce reuse
        # means remembering r values, so this structure grows by design —
        # but the scanner is constructed once in a long-lived worker
        # (src/workers/orchestrator.py) and fed a transaction stream, so
        # unbounded it accumulates every r from every transaction ever seen
        # and exhausts memory long before it detects anything on a real run.
        #
        # Evicting the least-recently-seen r trades completeness for
        # survivability, and does so in the right direction: reused nonces
        # come from one faulty signer and cluster in time, so the pairs this
        # can still catch are the ones it was ever realistically going to.
        # An eviction is not a clean loss, though — a later reuse of an
        # evicted r goes undetected — so the count is tracked rather than
        # dropped silently.
        self._r_registry: OrderedDict[int, list[tuple[int, str, str, int]]] = OrderedDict()
        self._max_tracked_r = max_tracked_r
        self._evicted_r = 0
        self._weaknesses: deque[ECDSAWeakness] = deque(maxlen=_MAX_RETAINED_WEAKNESSES)

    def scan_transaction(self, raw_tx_hex: str, tx_hash_z: int = 0) -> list[ECDSAWeakness]:
        """
        Scan a raw transaction for ECDSA weaknesses.

        tx_hash_z: the transaction hash used as z in signing (simplified: use 0 when unknown).
        Returns any newly detected weaknesses.
        """
        sigs = extract_ecdsa_signatures(raw_tx_hex)
        found: list[ECDSAWeakness] = []

        for r, s, pubkey, txid in sigs:
            pubkey_hex = pubkey.hex()
            z = tx_hash_z

            registry_entry = self._r_registry.get(r)
            if registry_entry is not None:
                # Touch: this r is live again, so it should not be the next
                # thing evicted.
                self._r_registry.move_to_end(r)
            else:
                registry_entry = []
                self._r_registry[r] = registry_entry
                while len(self._r_registry) > self._max_tracked_r:
                    self._r_registry.popitem(last=False)
                    self._evicted_r += 1
            if registry_entry:
                for s_prev, _pk_prev, txid_prev, z_prev in registry_entry:
                    # Same r value = same nonce k used → potential private key recovery
                    privkey = _extract_privkey_from_nonce_reuse(r, s, s_prev, z, z_prev)
                    weakness = ECDSAWeakness(
                        pubkey_hex=pubkey_hex,
                        r_value=r,
                        risk_score=0.95 if privkey is not None else 0.7,
                        txids=[txid, txid_prev],
                        privkey_extracted=privkey is not None,
                    )
                    found.append(weakness)
                    self._weaknesses.append(weakness)
                    log.warning(
                        "ecdsa_nonce_reuse_detected",
                        pubkey=pubkey_hex[:16],
                        risk=weakness.risk_score,
                        privkey_extracted=weakness.privkey_extracted,
                    )

            # Capped per r as well as across r values. A single r recurring
            # thousands of times is itself the weakness and is reported on
            # the second sighting; retaining every later one adds no
            # detection and is the shape a spam stream would exploit.
            if len(registry_entry) < _MAX_SIGS_PER_R:
                registry_entry.append((s, pubkey_hex, txid, z))

        return found

    @property
    def weakness_score(self) -> float:
        """Max risk score across all detected weaknesses (0 if none)."""
        if not self._weaknesses:
            return 0.0
        return float(max(w.risk_score for w in self._weaknesses))

    def clear_old_entries(self, max_registry_size: int = 100_000) -> None:
        """Prune r-registry to prevent unbounded memory growth."""
        if len(self._r_registry) > max_registry_size:
            # Remove oldest 10% of entries
            keys = list(self._r_registry.keys())
            for k in keys[: len(keys) // 10]:
                del self._r_registry[k]
